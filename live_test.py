import os
import sys
import glob
import json
import time
import subprocess
import threading
import winreg as reg
import ctypes
import sounddevice as sd
import numpy as np
import librosa
import joblib
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from sklearn.ensemble import RandomForestClassifier
from PIL import Image, ImageDraw
import pystray

# --- 1. LOCK WORKING DIRECTORY (CRITICAL FOR WINDOWS STARTUP) ---
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# Prevent duplicate background instances
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\AcousticTriggerAppMutex")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

# --- 2. GLOBAL STATE & CONSTANTS ---
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BACKEND_EXE = os.path.join(BASE_DIR, "trigger_backend.exe")
PIPE_PATH = r'\\.\pipe\AcousticTriggerPipe'

NOISE_KEYS = ("__TYPING_NOISE__", "__NEGATIVE_NOISE__")

trigger_names = ["Left", "Right"]
active_configs = {}      # { "Left": "TEXT:git status", "Right": "COMBO:17,67" }
trigger_panels = {}      # UI widget references per trigger
led_widgets = {}         # Telemetry LED labels per trigger

clf = None
model_classes = []       # Trigger names corresponding to class indices 0..N-1
profile_samples = {}     # Stored feature vectors per trigger + noise keys
model_loaded = False
is_paused = False

# Window visibility state: True when visible on screen (Telemetry-Only mode)
is_window_open = True

# Typing suppression state
last_physical_keypress_time = 0.0
last_macro_execution_time = 0.0

backend_proc = None
pipe = None
stream = None
current_sr = 44100
current_channels = 2

# Calibration State & Thread Lock
calib_lock = threading.Lock()
calib_session_id = 0
is_calibrating = False
calib_queue = []         # e.g. ["Left", "Right", "__TYPING_NOISE__"]
calib_idx = 0
taps_collected = 0
calib_append_mode = False
TARGET_TAPS = 10
TYPING_NOISE_TAPS = 5
NEGATIVE_NOISE_TAPS = 8
target_profile_name = "Default"
temp_recorded_features = []
backup_profile_samples = {}

# Cache synthetic noise per sample rate so training is near-instant
synthetic_noise_cache = {}

# --- 3. HARDWARE KEYBOARD TYPING MONITOR (STOPS TYPING FALSE POSITIVES) ---
TYPING_VK_CODES = (
    [0x08, 0x09, 0x0D, 0x20] +
    list(range(0x30, 0x5B)) +
    list(range(0x60, 0x70)) +
    list(range(0xBA, 0xE0))
)

def keyboard_activity_monitor():
    """Polls Win32 key states to detect physical keyboard typing and suppress false triggers."""
    global last_physical_keypress_time
    user32 = ctypes.windll.user32
    prev_down = set()

    while True:
        time.sleep(0.02)  # 20ms polling
        now = time.time()
        if now - last_macro_execution_time < 0.35:
            continue

        currently_down = set()
        for vk in TYPING_VK_CODES:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                currently_down.add(vk)

        new_presses = currently_down - prev_down
        if new_presses:
            last_physical_keypress_time = now
        prev_down = currently_down

# --- 4. C++ BACKEND PROCESS & NAMED PIPE MANAGER ---
def start_cpp_backend_and_connect():
    global backend_proc, pipe
    if pipe is not None:
        return True

    subprocess.run(["taskkill", "/F", "/IM", "trigger_backend.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=0x08000000)

    if os.path.exists(BACKEND_EXE):
        try:
            backend_proc = subprocess.Popen(
                [BACKEND_EXE],
                cwd=BASE_DIR,
                creationflags=0x08000000  # CREATE_NO_WINDOW
            )
        except Exception as e:
            print(f"Failed to spawn trigger_backend.exe: {e}")

    for _ in range(25):
        try:
            pipe = open(PIPE_PATH, 'w', encoding='utf-8')
            return True
        except OSError:
            time.sleep(0.1)

    pipe = None
    return False

def stop_cpp_backend():
    global backend_proc, pipe
    if pipe:
        try:
            pipe.close()
        except Exception:
            pass
        pipe = None
    if backend_proc:
        try:
            backend_proc.terminate()
        except Exception:
            pass
        backend_proc = None
    subprocess.run(["taskkill", "/F", "/IM", "trigger_backend.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=0x08000000)

# --- 5. WINDOWS STARTUP REGISTRY ---
def set_windows_startup(enable):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "AcousticTrigger"
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, key_path, 0, reg.KEY_ALL_ACCESS)
        if enable:
            if getattr(sys, 'frozen', False):
                cmd = f'"{sys.executable}" --minimized'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}" --minimized'
            reg.SetValueEx(key, app_name, 0, reg.REG_SZ, cmd)
        else:
            try:
                reg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        reg.CloseKey(key)
    except Exception as e:
        print(f"Registry update error: {e}")

def check_windows_startup():
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, key_path, 0, reg.KEY_READ)
        reg.QueryValueEx(key, "AcousticTrigger")
        reg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False

# --- 6. AUDIO DEVICE MANAGER ---
def get_audio_input_devices():
    devices = []
    hostapis = sd.query_hostapis()
    for i, dev in enumerate(sd.query_devices()):
        if dev['max_input_channels'] > 0:
            api_name = hostapis[dev['hostapi']]['name']
            label = f"[{api_name}] {dev['name']} (ID:{i})"
            devices.append((i, label, api_name))
    devices.sort(key=lambda x: (0 if "WASAPI" in x[2] else 1, x[0]))
    return devices

def start_audio_stream(device_idx=None):
    global stream, current_sr, current_channels
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        stream = None

    try:
        if device_idx is None:
            devs = get_audio_input_devices()
            device_idx = devs[0][0] if devs else sd.default.device[0]

        dev_info = sd.query_devices(device_idx, 'input')
        current_sr = int(dev_info['default_samplerate'])
        current_channels = max(1, int(dev_info['max_input_channels']))

        stream = sd.InputStream(
            device=device_idx,
            samplerate=current_sr,
            channels=current_channels,
            blocksize=int(0.35 * current_sr),
            callback=audio_callback
        )
        stream.start()

        threading.Thread(target=get_cached_synthetic_noise, args=(current_sr,), daemon=True).start()
        return True
    except Exception as e:
        status_label.config(text=f"🔴 Audio Device Error: {e}", fg="red")
        return False

def on_audio_device_changed(event=None):
    sel = device_combo.get()
    for idx, label, _ in available_devices:
        if label == sel:
            if start_audio_stream(idx):
                status_label.config(text=f"🟢 Switched Mic: {sel[:38]}...", fg="green")
            break

# --- 7. DSP FEATURE EXTRACTION, KEYCAP FILTER & SYNTHETIC NOISE ---
def is_valid_impulse(audio, check_zcr=True, min_crest=5.5):
    """Filters out continuous voice/music (low crest factor) and high-pitched keycap clacks."""
    peak = np.max(np.abs(audio))
    rms = np.sqrt(np.mean(audio**2)) + 1e-8
    if (peak / rms) <= min_crest:
        return False

    if check_zcr:
        mono = np.mean(audio, axis=1) if len(audio.shape) > 1 else audio
        pk = np.argmax(np.abs(mono))
        win = mono[max(0, pk - 400):min(len(mono), pk + 1200)]
        if len(win) > 100:
            zcr = np.mean(np.diff(np.sign(win)) != 0)
            if zcr > 0.38:
                return False
    return True

def extract_features(audio, sr):
    if len(audio.shape) > 1:
        audio = audio.T
    else:
        audio = audio.reshape(1, -1)

    if audio.shape[0] == 1:
        audio = np.vstack([audio[0], audio[0]])
    elif audio.shape[0] > 2:
        audio = audio[:2, :]

    max_vol = np.max(np.abs(audio))
    if max_vol > 0:
        audio = audio / max_vol

    features = []
    expected_length = int(0.2 * sr)

    correlation = np.correlate(audio[0], audio[1], mode='full')
    delay = np.argmax(correlation) - (len(audio[1]) - 1)
    features.append(delay)

    for ch in range(2):
        channel_data = audio[ch]
        peak_index = np.argmax(np.abs(channel_data))
        start = max(0, peak_index - int(0.05 * sr))
        end = min(len(channel_data), peak_index + int(0.15 * sr))
        cropped = channel_data[start:end]
        if len(cropped) < expected_length:
            cropped = np.pad(cropped, (0, expected_length - len(cropped)))
        mfccs = librosa.feature.mfcc(y=cropped, sr=sr, n_mfcc=13)
        features.extend(np.mean(mfccs.T, axis=0))

    return np.array(features)

def get_cached_synthetic_noise(sr, num_samples=30):
    if sr in synthetic_noise_cache:
        return synthetic_noise_cache[sr]

    noise_features = []
    n_frames = int(0.35 * sr)
    t = np.linspace(0, 0.35, n_frames)

    for i in range(num_samples):
        kind = i % 5
        if kind == 0:
            sig = np.random.normal(0, 1, (n_frames, 2))
        elif kind == 1:
            f0 = np.random.uniform(100, 350)
            wave = (np.sin(2 * np.pi * f0 * t) +
                    0.5 * np.sin(2 * np.pi * f0 * 2 * t) +
                    0.25 * np.sin(2 * np.pi * f0 * 3 * t))
            envelope = np.sin(np.pi * t / 0.35)
            sig = np.column_stack([wave * envelope, wave * envelope])
            sig += np.random.normal(0, 0.1, sig.shape)
        elif kind == 2:
            freq = np.random.uniform(20, 70)
            decay = np.exp(-t * np.random.uniform(8, 20))
            wave = np.sin(2 * np.pi * freq * t) * decay
            sig = np.column_stack([wave, wave])
            sig += np.random.normal(0, 0.05, sig.shape)
        elif kind == 3:
            freq = np.random.uniform(1500, 4200)
            decay = np.exp(-t * np.random.uniform(60, 120))
            burst = np.sin(2 * np.pi * freq * t) * decay
            sig = np.column_stack([burst, burst * np.random.uniform(0.6, 1.4)])
            sig += np.random.normal(0, 0.08, sig.shape)
        else:
            sig = np.random.normal(0, 0.2, (n_frames, 2))
            idx = np.random.randint(500, max(501, n_frames - 600))
            shift = np.random.randint(-120, 120)
            sig[idx:idx+300, 0] += np.random.normal(0, 2.0, min(300, n_frames - idx))
            idx2 = max(0, min(n_frames - 300, idx + shift))
            sig[idx2:idx2+300, 1] += np.random.normal(0, 2.0, min(300, n_frames - idx2))

        noise_features.append(extract_features(sig, sr))

    synthetic_noise_cache[sr] = noise_features
    return noise_features

# --- 8. MODEL & PROFILE MANAGEMENT ---
def get_available_profiles():
    profiles = {}
    if os.path.exists("tap_model.pkl"):
        profiles["Default"] = "tap_model.pkl"
    for f in glob.glob("model_*.pkl"):
        name = f[6:-4]
        profiles[name] = f
    return profiles

def update_status_bar_for_window_state():
    if is_calibrating:
        return
    prof_name = profile_combo.get() or "None"
    if not model_loaded:
        status_label.config(text="🟡 No Model Loaded - Click '➕ Calibrate New Profile'", fg="orange")
    elif is_paused:
        status_label.config(text="⏸️ Detection Paused", fg="orange")
    elif is_window_open:
        neg_count = len(profile_samples.get("__NEGATIVE_NOISE__", []))
        neg_tag = f" | Neg Samples: {neg_count}" if neg_count > 0 else ""
        status_label.config(
            text=f"🟢 Profile '{prof_name}'{neg_tag} | TEST MODE (Minimize to Tray to fire actions)",
            fg="#0078D7"
        )
    else:
        status_label.config(text=f"🟢 Active Profile: '{prof_name}' (Macros Live)", fg="green")

def load_selected_profile(event=None):
    global clf, model_classes, profile_samples, model_loaded
    prof_name = profile_combo.get()
    profiles = get_available_profiles()
    if prof_name in profiles:
        try:
            loaded_obj = joblib.load(profiles[prof_name])
            if isinstance(loaded_obj, dict) and "model" in loaded_obj:
                clf = loaded_obj["model"]
                model_classes = loaded_obj.get("classes", ["Left", "Right"])
                profile_samples = loaded_obj.get("samples", {})
            else:
                clf = loaded_obj
                model_classes = ["Left", "Right"]
                profile_samples = {}

            model_loaded = True
            for c_name in model_classes:
                if c_name not in trigger_panels:
                    add_trigger_ui_panel(c_name)

            update_status_bar_for_window_state()
        except Exception as e:
            clf = None
            model_loaded = False
            status_label.config(text=f"🔴 Failed to load '{prof_name}': {e}", fg="red")
    else:
        clf = None
        model_loaded = False
        update_status_bar_for_window_state()

def train_and_save_profile(prof_name, samples_dict):
    global clf, model_classes, profile_samples, model_loaded
    X, y = [], []
    classes = [k for k in samples_dict.keys() if k not in NOISE_KEYS]

    for idx, t_name in enumerate(classes):
        for feat_vec in samples_dict[t_name]:
            X.append(feat_vec)
            y.append(idx)

    noise_class_idx = len(classes)
    for noise_key in NOISE_KEYS:
        for feat_vec in samples_dict.get(noise_key, []):
            X.append(feat_vec)
            y.append(noise_class_idx)

    noise_X = get_cached_synthetic_noise(current_sr, num_samples=30)
    for nf in noise_X:
        X.append(nf)
        y.append(noise_class_idx)

    new_clf = RandomForestClassifier(n_estimators=120, random_state=42)
    new_clf.fit(X, y)

    filename = "tap_model.pkl" if prof_name.lower() == "default" else f"model_{prof_name}.pkl"
    payload = {
        "model": new_clf,
        "classes": classes,
        "samples": samples_dict
    }
    joblib.dump(payload, filename)

    clf = new_clf
    model_classes = classes
    profile_samples = samples_dict
    model_loaded = True

# --- 9. CALIBRATION, NEGATIVE AUDIO & CANCEL CONTROLS ---
def get_target_count_for_step(step_name):
    if step_name == "__TYPING_NOISE__":
        return TYPING_NOISE_TAPS
    if step_name == "__NEGATIVE_NOISE__":
        return NEGATIVE_NOISE_TAPS
    return TARGET_TAPS

def get_prompt_for_step(prof_name, step_name, count):
    target = get_target_count_for_step(step_name)
    if step_name == "__TYPING_NOISE__":
        return f"⌨️ ANTI-TYPING [{prof_name}]: Press random keyboard keys firmly ({count}/{target})"
    if step_name == "__NEGATIVE_NOISE__":
        return f"🚫 NEGATIVE AUDIO [{prof_name}]: Make the false-trigger sound ({count}/{target})"
    return f"🔵 RECORDING [{prof_name}]: Perform '{step_name}' sound ({count}/{target})"

def set_recording_controls_active(recording):
    state = "disabled" if recording else "normal"
    calib_btn.config(state=state)
    neg_audio_btn.config(state=state)
    add_custom_btn.config(state=state)
    save_btn.config(state=state)
    profile_combo.config(state="disabled" if recording else "readonly")
    for w in trigger_panels.values():
        w["rec_btn"].config(state=state)
    if recording:
        cancel_calib_btn.pack(side="right", padx=(8, 0))
    else:
        cancel_calib_btn.pack_forget()

def begin_recording_queue(queue_names, prof_name, clear_existing=False, append_mode=False):
    global calib_session_id, is_calibrating, calib_queue, calib_idx, taps_collected
    global target_profile_name, temp_recorded_features, backup_profile_samples, calib_append_mode
    if not queue_names:
        return

    with calib_lock:
        calib_session_id += 1
        backup_profile_samples = {k: list(v) for k, v in profile_samples.items()}
        if clear_existing:
            profile_samples.clear()
        target_profile_name = prof_name
        calib_queue = list(queue_names)
        calib_idx = 0
        taps_collected = 0
        calib_append_mode = append_mode
        temp_recorded_features = []
        is_calibrating = True

    set_recording_controls_active(True)
    first_step = calib_queue[0]
    status_label.config(text=get_prompt_for_step(prof_name, first_step, 0), fg="#0055ff")

def cancel_recording():
    global calib_session_id, is_calibrating, calib_idx, taps_collected, calib_append_mode
    with calib_lock:
        if not is_calibrating:
            return
        calib_session_id += 1
        is_calibrating = False
        calib_queue.clear()
        calib_idx = 0
        taps_collected = 0
        calib_append_mode = False
        temp_recorded_features.clear()
        profile_samples.clear()
        profile_samples.update(backup_profile_samples)

    set_recording_controls_active(False)
    status_label.config(text="🚫 Recording cancelled. Previous profile preserved.", fg="#cc5500")
    root.after(2200, update_status_bar_for_window_state)

def start_full_profile_calibration():
    name = simpledialog.askstring(
        "Calibrate New Profile",
        "Enter a name for this device/surface profile (e.g., 'Hostel Desk', 'Lap', 'Default'):",
        parent=root
    )
    if not name or not name.strip():
        return
    clean_name = "".join(c for c in name.strip() if c.isalnum() or c in (' ', '_', '-'))
    if not clean_name:
        messagebox.showerror("Invalid Name", "Please use standard letters and numbers.")
        return
    begin_recording_queue(list(trigger_names) + ["__TYPING_NOISE__"], clean_name, clear_existing=True, append_mode=False)

def record_single_trigger_sound(trig_name):
    prof_name = profile_combo.get() or "Default"
    missing_base = [t for t in trigger_names if t != trig_name and t not in profile_samples]
    if missing_base:
        ans = messagebox.askyesno(
            "Full Profile Recording Required",
            f"The current profile '{prof_name}' does not have raw audio samples stored for {missing_base}.\n\n"
            f"Would you like to record all triggers + keyboard typing rejection now?"
        )
        if ans:
            begin_recording_queue(list(trigger_names) + ["__TYPING_NOISE__"], prof_name, clear_existing=False, append_mode=False)
        return
    begin_recording_queue([trig_name], prof_name, clear_existing=False, append_mode=False)

def record_negative_audio():
    prof_name = profile_combo.get() or "Default"
    valid_triggers = [k for k in profile_samples.keys() if k not in NOISE_KEYS]
    if len(valid_triggers) < 1:
        ans = messagebox.askyesno(
            "Calibration Required First",
            f"Profile '{prof_name}' doesn't contain stored raw trigger samples yet (legacy or uncalibrated model).\n\n"
            f"Calibrate this profile first so negative audio can be added to it?"
        )
        if ans:
            begin_recording_queue(list(trigger_names) + ["__TYPING_NOISE__", "__NEGATIVE_NOISE__"], prof_name, clear_existing=True, append_mode=False)
        return

    begin_recording_queue(["__NEGATIVE_NOISE__"], prof_name, clear_existing=False, append_mode=True)

def finish_training_async(prof_name, samples_snapshot, was_negative_only):
    try:
        train_and_save_profile(prof_name, samples_snapshot)
        root.after(0, lambda: on_training_complete_ui(prof_name, was_negative_only))
    except Exception as e:
        root.after(0, lambda err=e: status_label.config(text=f"🔴 Training Error: {err}", fg="red"))
        root.after(0, lambda: set_recording_controls_active(False))

def on_training_complete_ui(prof_name, was_negative_only):
    profs = list(get_available_profiles().keys())
    profile_combo.config(values=profs)
    profile_combo.set("Default" if prof_name.lower() == "default" else prof_name)
    set_recording_controls_active(False)
    update_status_bar_for_window_state()
    if was_negative_only:
        total_neg = len(profile_samples.get("__NEGATIVE_NOISE__", []))
        messagebox.showinfo(
            "Negative Audio Added",
            f"False-trigger samples added to '{prof_name}'!\n(Total negative audio samples stored: {total_neg})"
        )
    else:
        messagebox.showinfo(
            "Calibration Complete",
            f"Profile '{prof_name}' is calibrated and active!"
        )

def on_calibration_step_ui(session_id, step_name, count_now, next_step_name, all_done, was_negative_only):
    if session_id != calib_session_id:
        return
    if all_done:
        cancel_calib_btn.pack_forget()
        status_label.config(text="⏳ Training AI Model...", fg="orange")
        snapshot = {k: list(v) for k, v in profile_samples.items()}
        threading.Thread(
            target=finish_training_async,
            args=(target_profile_name, snapshot, was_negative_only),
            daemon=True
        ).start()
    elif next_step_name is not None:
        status_label.config(text=get_prompt_for_step(target_profile_name, next_step_name, 0))
    else:
        status_label.config(text=get_prompt_for_step(target_profile_name, step_name, count_now))

# --- 10. REAL-TIME AUDIO CALLBACK ---
last_trigger_time = 0.0
def audio_callback(indata, frames, time_info, status):
    global last_trigger_time, is_calibrating, calib_idx, taps_collected
    if is_paused:
        return

    now = time.time()
    if now - last_trigger_time < 0.45:
        return

    peak_vol = np.max(np.abs(indata))

    # 1. Thread-Safe Calibration / Negative Audio Recording Mode
    if is_calibrating:
        with calib_lock:
            if not is_calibrating or calib_idx >= len(calib_queue):
                return
            curr_step = calib_queue[calib_idx]
            active_session = calib_session_id

        is_noise_step = curr_step in NOISE_KEYS
        vol_thresh = 0.30 if is_noise_step else 0.40
        if peak_vol <= vol_thresh:
            return

        audio_chunk = indata.copy()
        min_crest = 3.2 if is_noise_step else 5.5
        if not is_valid_impulse(audio_chunk, check_zcr=(not is_noise_step), min_crest=min_crest):
            return

        features = extract_features(audio_chunk, current_sr)

        with calib_lock:
            if not is_calibrating or active_session != calib_session_id or calib_idx >= len(calib_queue):
                return
            curr_step = calib_queue[calib_idx]
            target_count = get_target_count_for_step(curr_step)

            temp_recorded_features.append(features)
            taps_collected = len(temp_recorded_features)
            last_trigger_time = now

            next_step_name = None
            all_done = False
            was_negative_only = (calib_queue == ["__NEGATIVE_NOISE__"])

            if taps_collected >= target_count:
                if calib_append_mode and curr_step in profile_samples:
                    profile_samples[curr_step].extend(temp_recorded_features)
                else:
                    profile_samples[curr_step] = list(temp_recorded_features)
                temp_recorded_features.clear()
                calib_idx += 1
                taps_collected = 0

                if calib_idx < len(calib_queue):
                    next_step_name = calib_queue[calib_idx]
                else:
                    is_calibrating = False
                    all_done = True

            count_snapshot = taps_collected

        if not is_noise_step:
            root.after(0, lambda t=curr_step: flash_indicator(t))
        root.after(
            0,
            lambda sid=active_session, s=curr_step, c=count_snapshot, ns=next_step_name, ad=all_done, neg=was_negative_only:
                on_calibration_step_ui(sid, s, c, ns, ad, neg)
        )
        return

    # 2. Live Inference Mode
    if peak_vol <= 0.40:
        return

    if ignore_typing_var.get() and (now - last_physical_keypress_time < 0.45):
        return

    audio_chunk = indata.copy()
    if not is_valid_impulse(audio_chunk, check_zcr=True, min_crest=5.5):
        return

    features = extract_features(audio_chunk, current_sr)

    if model_loaded and clf is not None:
        probs = clf.predict_proba(features.reshape(1, -1))[0]
        pred_idx = int(np.argmax(probs))

        if pred_idx < len(model_classes) and probs[pred_idx] >= 0.62:
            trig_name = model_classes[pred_idx]
            root.after(0, lambda t=trig_name: flash_indicator(t))

            # ONLY execute actions when minimized to System Tray
            if not is_window_open:
                cmd = active_configs.get(trig_name, "")
                if cmd:
                    send_to_backend(trig_name, cmd)

    last_trigger_time = now

def send_to_backend(trig_name, cmd):
    global pipe, last_macro_execution_time
    last_macro_execution_time = time.time()
    if pipe is None:
        start_cpp_backend_and_connect()
    if pipe is not None:
        try:
            pipe.write(f"{trig_name.upper()}:{cmd}\n")
            pipe.flush()
        except OSError:
            pipe = None
            if start_cpp_backend_and_connect():
                try:
                    pipe.write(f"{trig_name.upper()}:{cmd}\n")
                    pipe.flush()
                except OSError:
                    pass

# --- 11. AUTOMATIC COMMAND FORMATTING, SAVING & LOADING ---
def get_text_box_value(text_widget):
    return text_widget.get("1.0", "end-1c")

def escape_for_pipe(raw_text):
    # Escape backslashes, newlines, and tabs so multi-line snippets transmit in one pipe message
    return (
        raw_text.replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\t", "\\t")
    )

def format_command(w):
    raw_val = get_text_box_value(w["text"])
    if raw_val.strip():
        return f"TEXT:{escape_for_pipe(raw_val)}"

    vk_codes = []
    if w["ctrl"].get(): vk_codes.append("17")
    if w["shift"].get(): vk_codes.append("16")
    if w["alt"].get(): vk_codes.append("18")
    if w["win"].get(): vk_codes.append("91")

    base_key = w["key"].get().strip().upper()
    if base_key:
        vk_codes.append(str(ord(base_key[0])))
    if not vk_codes:
        return ""
    return f"COMBO:{','.join(vk_codes)}"

def update_card_mode_visuals(name):
    if name not in trigger_panels:
        return
    w = trigger_panels[name]
    has_text = bool(get_text_box_value(w["text"]).strip())
    shortcut_state = "disabled" if has_text else "normal"
    for widget in w["shortcut_widgets"]:
        widget.config(state=shortcut_state)
    w["mode_badge"].config(
        text="Mode: Type String" if has_text else "Mode: Shortcut",
        fg="#0078D7" if has_text else "#555555"
    )

def toggle_text_box_expand(name):
    if name not in trigger_panels:
        return
    w = trigger_panels[name]
    w["expanded"] = not w["expanded"]
    if w["expanded"]:
        w["text"].config(height=8)
        w["expand_btn"].config(text="⤡ Collapse")
    else:
        w["text"].config(height=2)
        w["expand_btn"].config(text="⤢ Expand")
    root.update_idletasks()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))

def save_and_apply_config():
    global active_configs
    active_configs.clear()
    triggers_block = {}

    for name in trigger_names:
        w = trigger_panels[name]
        update_card_mode_visuals(name)
        cmd = format_command(w)
        if cmd:
            active_configs[name] = cmd
        triggers_block[name] = {
            "text": get_text_box_value(w["text"]),
            "ctrl": w["ctrl"].get(),
            "shift": w["shift"].get(),
            "alt": w["alt"].get(),
            "win": w["win"].get(),
            "key": w["key"].get()
        }

    sel_dev_label = device_combo.get()
    sel_dev_idx = None
    for idx, label, _ in available_devices:
        if label == sel_dev_label:
            sel_dev_idx = idx
            break

    set_windows_startup(startup_var.get())

    config_data = {
        "profile": profile_combo.get(),
        "device_index": sel_dev_idx,
        "startup": startup_var.get(),
        "ignore_typing": ignore_typing_var.get(),
        "trigger_order": trigger_names,
        "triggers": triggers_block
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
        status_label.config(
            text="💾 Saved! (Minimize window to System Tray to activate macros)",
            fg="#00aa00"
        )
    except Exception as e:
        status_label.config(text=f"🔴 Error saving config: {e}", fg="red")

def load_saved_config():
    global active_configs
    startup_var.set(check_windows_startup())
    if not os.path.exists(CONFIG_FILE):
        return None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for t_name in data.get("trigger_order", []):
            if t_name not in trigger_panels:
                add_trigger_ui_panel(t_name)

        if "profile" in data and data["profile"] in get_available_profiles():
            profile_combo.set(data["profile"])
            load_selected_profile()

        ignore_typing_var.set(data.get("ignore_typing", True))

        saved_dev_idx = data.get("device_index", None)
        if saved_dev_idx is not None:
            for idx, label, _ in available_devices:
                if idx == saved_dev_idx:
                    device_combo.set(label)
                    break

        for name, trig in data.get("triggers", {}).items():
            if name in trigger_panels:
                w = trigger_panels[name]
                w["text"].delete("1.0", tk.END)
                w["text"].insert("1.0", trig.get("text", ""))
                w["ctrl"].set(trig.get("ctrl", False))
                w["shift"].set(trig.get("shift", False))
                w["alt"].set(trig.get("alt", False))
                w["win"].set(trig.get("win", False))
                w["key"].delete(0, tk.END)
                w["key"].insert(0, trig.get("key", ""))
                update_card_mode_visuals(name)

        active_configs.clear()
        for name, w in trigger_panels.items():
            cmd = format_command(w)
            if cmd:
                active_configs[name] = cmd

        return saved_dev_idx
    except Exception as e:
        print(f"Error loading config: {e}")
        return None

# --- 12. DYNAMIC UI PANELS & TELEMETRY ---
def flash_indicator(trig_name):
    if trig_name in led_widgets:
        lbl = led_widgets[trig_name]
        lbl.config(bg="#00ff66", fg="black")
        root.after(300, lambda l=lbl: l.config(bg="#2b2b2b", fg="#888888"))

def rebuild_led_telemetry():
    for child in led_frame.winfo_children():
        child.destroy()
    led_widgets.clear()

    cols = 3
    for i, name in enumerate(trigger_names):
        r, c = divmod(i, cols)
        lbl = tk.Label(
            led_frame, text=f"{name.upper()}", font=("Arial", 9, "bold"),
            bg="#2b2b2b", fg="#888888", width=16, pady=5, relief="sunken"
        )
        lbl.grid(row=r, column=c, padx=5, pady=3, sticky="ew")
        led_frame.grid_columnconfigure(c, weight=1)
        led_widgets[name] = lbl

def delete_custom_trigger(name):
    if name in ("Left", "Right"):
        messagebox.showwarning("Default Trigger", "Default Left and Right triggers cannot be deleted.")
        return
    if not messagebox.askyesno("Delete Trigger", f"Remove custom trigger '{name}'?"):
        return

    trigger_names.remove(name)
    active_configs.pop(name, None)
    panel = trigger_panels.pop(name, None)
    if panel:
        panel["frame"].destroy()

    if name in profile_samples:
        profile_samples.pop(name, None)
        valid_triggers = [k for k in profile_samples.keys() if k not in NOISE_KEYS]
        if len(valid_triggers) >= 2:
            train_and_save_profile(profile_combo.get() or "Default", profile_samples)

    rebuild_led_telemetry()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))

def add_trigger_ui_panel(name):
    if name not in trigger_names:
        trigger_names.append(name)

    frame = tk.LabelFrame(
        scrollable_triggers_frame, text=f"⚡ {name} Trigger",
        font=("Arial", 10, "bold"), padx=10, pady=6
    )
    frame.pack(fill="x", pady=4, padx=4)
    frame.grid_columnconfigure(1, weight=1)

    card_top = tk.Frame(frame)
    card_top.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 4))

    rec_btn = tk.Button(
        card_top, text=f"🎙️ Record '{name}' Sound (10x)", font=("Arial", 8, "bold"),
        bg="#eef6ff", command=lambda n=name: record_single_trigger_sound(n)
    )
    rec_btn.pack(side="left")

    mode_badge = tk.Label(card_top, text="Mode: Shortcut", font=("Arial", 8, "italic"), fg="#555555")
    mode_badge.pack(side="left", padx=10)

    if name not in ("Left", "Right"):
        del_btn = tk.Button(
            card_top, text="🗑️ Delete", font=("Arial", 8), fg="red",
            command=lambda n=name: delete_custom_trigger(n)
        )
        del_btn.pack(side="right")

    # Expandable Multi-Line Type String Row
    label_col = tk.Frame(frame)
    label_col.grid(row=1, column=0, sticky="nw", pady=2)
    tk.Label(label_col, text="Type String:", font=("Arial", 9)).pack(anchor="w")
    expand_btn = tk.Button(
        label_col, text="⤢ Expand", font=("Arial", 7), padx=4, pady=0,
        command=lambda n=name: toggle_text_box_expand(n)
    )
    expand_btn.pack(anchor="w", pady=(2, 0))

    text_wrapper = tk.Frame(frame)
    text_wrapper.grid(row=1, column=1, columnspan=6, sticky="ew", padx=5, pady=2)
    text_wrapper.grid_columnconfigure(0, weight=1)

    text_box = tk.Text(text_wrapper, height=2, width=38, font=("Consolas", 9), wrap="word", undo=True)
    text_scroll = ttk.Scrollbar(text_wrapper, orient="vertical", command=text_box.yview)
    text_box.configure(yscrollcommand=text_scroll.set)
    text_box.grid(row=0, column=0, sticky="ew")
    text_scroll.grid(row=0, column=1, sticky="ns")

    text_box.bind("<KeyRelease>", lambda e, n=name: update_card_mode_visuals(n))
    text_box.bind("<<Paste>>", lambda e, n=name: root.after(10, lambda: update_card_mode_visuals(n)))

    # Shortcut Row
    tk.Label(frame, text="Shortcut:", font=("Arial", 9)).grid(row=2, column=0, sticky="w", pady=(4, 0))

    ctrl = tk.BooleanVar()
    shift = tk.BooleanVar()
    alt = tk.BooleanVar()
    win = tk.BooleanVar()

    shortcut_row = tk.Frame(frame)
    shortcut_row.grid(row=2, column=1, columnspan=6, sticky="w", padx=3, pady=(4, 0))

    cb_ctrl = tk.Checkbutton(shortcut_row, text="Ctrl", variable=ctrl)
    cb_shift = tk.Checkbutton(shortcut_row, text="Shift", variable=shift)
    cb_alt = tk.Checkbutton(shortcut_row, text="Alt", variable=alt)
    cb_win = tk.Checkbutton(shortcut_row, text="Win", variable=win)

    cb_ctrl.pack(side="left")
    cb_shift.pack(side="left")
    cb_alt.pack(side="left")
    cb_win.pack(side="left")

    tk.Label(shortcut_row, text="+").pack(side="left", padx=2)
    key_entry = tk.Entry(shortcut_row, width=6)
    key_entry.pack(side="left", padx=4)

    trigger_panels[name] = {
        "frame": frame,
        "rec_btn": rec_btn,
        "text": text_box,
        "expand_btn": expand_btn,
        "expanded": False,
        "ctrl": ctrl, "shift": shift, "alt": alt, "win": win,
        "key": key_entry,
        "mode_badge": mode_badge,
        "shortcut_widgets": [cb_ctrl, cb_shift, cb_alt, cb_win, key_entry]
    }

    rebuild_led_telemetry()
    root.update_idletasks()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))

def prompt_new_custom_trigger():
    raw_name = simpledialog.askstring(
        "New Custom Trigger",
        "Enter a name for your custom trigger (e.g., 'Center Knock', 'Snap', 'Top Tap'):",
        parent=root
    )
    if not raw_name or not raw_name.strip():
        return
    clean_name = "".join(c for c in raw_name.strip() if c.isalnum() or c in (' ', '_', '-'))
    if not clean_name:
        messagebox.showerror("Invalid Name", "Please use letters and numbers.")
        return
    if clean_name in trigger_panels:
        messagebox.showwarning("Duplicate", f"Trigger '{clean_name}' already exists.")
        return

    add_trigger_ui_panel(clean_name)
    if messagebox.askyesno("Record Custom Sound", f"Trigger '{clean_name}' created!\n\nWould you like to record its custom sound (10 times) right now?"):
        record_single_trigger_sound(clean_name)

# --- 13. WINDOW VISIBILITY (MINIMIZE -> TRAY, CLOSE -> EXIT) ---
tray_icon = None
def create_tray_icon_image():
    img = Image.new("RGB", (64, 64), color=(0, 120, 215))
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, 60, 60], outline=(255, 255, 255), width=3)
    draw.text((16, 20), "AT", fill=(255, 255, 255))
    return img

def poll_window_state():
    """When user minimizes the window ('iconic'), hide it completely to the System Tray."""
    global is_window_open
    try:
        state = root.state()
        if state == "iconic":
            hide_to_system_tray()
        else:
            current_open = state != "withdrawn"
            if current_open != is_window_open:
                is_window_open = current_open
                update_status_bar_for_window_state()
    except Exception:
        pass
    root.after(100, poll_window_state)

def restore_window_from_tray():
    global is_window_open
    root.deiconify()
    root.state("normal")
    root.lift()
    root.focus_force()
    is_window_open = True
    update_status_bar_for_window_state()

def hide_to_system_tray():
    global is_window_open
    root.withdraw()
    is_window_open = False
    update_status_bar_for_window_state()

def toggle_pause_from_tray(icon, item):
    global is_paused
    is_paused = not is_paused
    root.after(0, update_status_bar_for_window_state)

def quit_application(icon=None, item=None):
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    if stream:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
    stop_cpp_backend()
    try:
        root.destroy()
    except Exception:
        pass
    os._exit(0)

def run_system_tray():
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Open Control Center", lambda icon, item: root.after(0, restore_window_from_tray), default=True),
        pystray.MenuItem(
            lambda item: "Resume Detection" if is_paused else "Pause Detection",
            toggle_pause_from_tray
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Acoustic Trigger", quit_application)
    )
    tray_icon = pystray.Icon("AcousticTrigger", create_tray_icon_image(), "Acoustic Trigger (Running)", menu)
    tray_icon.run()

# --- 14. BUILD MAIN GUI ---
root = tk.Tk()
root.title("Acoustic Trigger Control Center")
root.geometry("600x740")
root.configure(padx=12, pady=10)
# Clicking X closes the entire app; minimizing hides it to the System Tray
root.protocol("WM_DELETE_WINDOW", quit_application)

# Section 1: System, Profile & Audio Device Selector
sys_frame = tk.LabelFrame(root, text="Device Profile & Audio Hardware", font=("Arial", 10, "bold"), padx=10, pady=8)
sys_frame.pack(fill="x", pady=(0, 8))
sys_frame.grid_columnconfigure(1, weight=1)

tk.Label(sys_frame, text="Acoustic Profile:").grid(row=0, column=0, sticky="w", pady=3)
available_profs = list(get_available_profiles().keys())
profile_combo = ttk.Combobox(sys_frame, values=available_profs, state="readonly", width=20)
profile_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
profile_combo.bind("<<ComboboxSelected>>", load_selected_profile)

prof_btns = tk.Frame(sys_frame)
prof_btns.grid(row=0, column=2, sticky="e", pady=3)

calib_btn = tk.Button(prof_btns, text="➕ Calibrate New", font=("Arial", 8, "bold"), command=start_full_profile_calibration)
calib_btn.pack(side="left", padx=(0, 4))

neg_audio_btn = tk.Button(
    prof_btns, text="🚫 Record Negative Audio", font=("Arial", 8, "bold"),
    bg="#fff3e0", command=record_negative_audio
)
neg_audio_btn.pack(side="left")

tk.Label(sys_frame, text="Audio Input Mic:").grid(row=1, column=0, sticky="w", pady=3)
available_devices = get_audio_input_devices()
device_labels = [label for _, label, _ in available_devices]
device_combo = ttk.Combobox(sys_frame, values=device_labels, state="readonly")
device_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
if device_labels:
    device_combo.set(device_labels[0])
device_combo.bind("<<ComboboxSelected>>", on_audio_device_changed)

ignore_typing_var = tk.BooleanVar(value=True)
ignore_typing_chk = tk.Checkbutton(
    sys_frame, text="🛡️ Suppress triggers while physically typing on keyboard",
    variable=ignore_typing_var
)
ignore_typing_chk.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

startup_var = tk.BooleanVar(value=check_windows_startup())
startup_chk = tk.Checkbutton(
    sys_frame, text="Run automatically on Windows Startup (starts minimized in System Tray)",
    variable=startup_var, command=lambda: set_windows_startup(startup_var.get())
)
startup_chk.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

# Section 2: Custom Trigger Header & Scrollable Trigger List
header_bar = tk.Frame(root)
header_bar.pack(fill="x", pady=(2, 4))
tk.Label(header_bar, text="Trigger Mappings (Empty String = Shortcut Mode)", font=("Arial", 10, "bold")).pack(side="left")
add_custom_btn = tk.Button(
    header_bar, text="➕ Add Custom Trigger", font=("Arial", 9, "bold"),
    bg="#e8f5e9", command=prompt_new_custom_trigger
)
add_custom_btn.pack(side="right")

canvas_container = tk.Frame(root, bd=1, relief="sunken")
canvas_container.pack(fill="both", expand=True, pady=4)

triggers_canvas = tk.Canvas(canvas_container, highlightthickness=0)
scrollbar = ttk.Scrollbar(canvas_container, orient="vertical", command=triggers_canvas.yview)
scrollable_triggers_frame = tk.Frame(triggers_canvas)

scrollable_triggers_frame.bind(
    "<Configure>",
    lambda e: triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))
)
canvas_window = triggers_canvas.create_window((0, 0), window=scrollable_triggers_frame, anchor="nw")
triggers_canvas.bind(
    "<Configure>",
    lambda e: triggers_canvas.itemconfig(canvas_window, width=e.width)
)
triggers_canvas.configure(yscrollcommand=scrollbar.set)

triggers_canvas.pack(side="left", fill="both", expand=True)
scrollbar.pack(side="right", fill="y")

def _on_mousewheel(event):
    # If hovering over a scrollable Text box that has overflow, scroll the text box instead
    if isinstance(event.widget, tk.Text):
        first, last = event.widget.yview()
        if not (first <= 0.0 and last >= 1.0):
            event.widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return
    triggers_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
triggers_canvas.bind_all("<MouseWheel>", _on_mousewheel)

# Section 3: Save Button
save_btn = tk.Button(
    root, text="💾 Save & Apply Configuration", font=("Arial", 11, "bold"),
    bg="#0078D7", fg="white", pady=6, command=save_and_apply_config
)
save_btn.pack(fill="x", pady=6)

# Section 4: Live Detection LEDs
led_frame = tk.LabelFrame(root, text="Live Sensor Telemetry (Active while window is open)", font=("Arial", 10, "bold"), padx=10, pady=8)
led_frame.pack(fill="x", pady=4)

# Section 5: Status Bar + Dynamic Cancel Recording Button
status_frame = tk.Frame(root)
status_frame.pack(fill="x", pady=(4, 0))

status_label = tk.Label(status_frame, text="Initializing...", font=("Arial", 9, "bold"), anchor="w")
status_label.pack(side="left", fill="x", expand=True)

cancel_calib_btn = tk.Button(
    status_frame, text="❌ Cancel", font=("Arial", 8, "bold"),
    bg="#ff4d4d", fg="white", padx=8, command=cancel_recording
)
# Hidden until recording starts

# --- 15. INITIALIZE APP STATE ---
start_cpp_backend_and_connect()
threading.Thread(target=keyboard_activity_monitor, daemon=True).start()

for default_trig in ["Left", "Right"]:
    add_trigger_ui_panel(default_trig)

if "Default" in available_profs:
    profile_combo.set("Default")
    load_selected_profile()
elif available_profs:
    profile_combo.set(available_profs[0])
    load_selected_profile()
else:
    update_status_bar_for_window_state()

saved_mic_idx = load_saved_config()
start_audio_stream(saved_mic_idx)

threading.Thread(target=run_system_tray, daemon=True).start()

if "--minimized" in sys.argv:
    hide_to_system_tray()

poll_window_state()
root.mainloop()