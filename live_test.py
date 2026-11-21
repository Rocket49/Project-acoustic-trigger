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
import tkinter.font as tkfont
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

# --- 2. GLOBAL STATE, CONSTANTS & THEMES ---
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
BACKEND_EXE = os.path.join(BASE_DIR, "trigger_backend.exe")
PIPE_PATH = r'\\.\pipe\AcousticTriggerPipe'

DEFAULT_VOL_THRESHOLD = 0.35
NOISE_KEYS = ("__TYPING_NOISE__", "__NEGATIVE_NOISE__")

THEMES = {
    "Light": {
        "bg": "#f0f0f0",
        "card_bg": "#f7f7f7",
        "fg": "#1a1a1a",
        "muted_fg": "#555555",
        "accent_fg": "#0078D7",
        "ok_fg": "#0b8a3a",
        "warn_fg": "#c76c00",
        "input_bg": "#ffffff",
        "input_fg": "#111111",
        "insert_bg": "#000000",
        "btn_bg": "#e4e4e4",
        "btn_fg": "#111111",
        "btn_active": "#d4d4d4",
        "btn_green_bg": "#e8f5e9",
        "btn_orange_bg": "#fff3e0",
        "btn_blue_bg": "#eef6ff",
        "btn_accent_bg": "#0078D7",
        "btn_accent_fg": "#ffffff",
        "btn_danger_bg": "#d93838",
        "btn_warn_bg": "#e68a00",
        "del_fg": "#cc2222",
        "led_off_bg": "#2b2b2b",
        "led_off_fg": "#888888",
        "led_on_bg": "#00ff66",
        "led_on_fg": "#000000",
        "meter_bg": "#222222",
        "meter_sub_fill": "#4da6ff",
        "meter_hit_fill": "#00e676",
        "select_bg": "#ffffff",
        "trough": "#dcdcdc",
        "border": "#c8c8c8"
    },
    "Dark": {
        "bg": "#1e1e1e",
        "card_bg": "#252526",
        "fg": "#e6e6e6",
        "muted_fg": "#aaaaaa",
        "accent_fg": "#4da6ff",
        "ok_fg": "#2ecc71",
        "warn_fg": "#f39c12",
        "input_bg": "#2d2d30",
        "input_fg": "#f0f0f0",
        "insert_bg": "#ffffff",
        "btn_bg": "#333337",
        "btn_fg": "#f0f0f0",
        "btn_active": "#45454a",
        "btn_green_bg": "#1f3a29",
        "btn_orange_bg": "#3d2e1a",
        "btn_blue_bg": "#1d2f44",
        "btn_accent_bg": "#0078D7",
        "btn_accent_fg": "#ffffff",
        "btn_danger_bg": "#c9302c",
        "btn_warn_bg": "#cc7a00",
        "del_fg": "#ff6b6b",
        "led_off_bg": "#141414",
        "led_off_fg": "#666666",
        "led_on_bg": "#00e676",
        "led_on_fg": "#000000",
        "meter_bg": "#121212",
        "meter_sub_fill": "#4da6ff",
        "meter_hit_fill": "#00e676",
        "select_bg": "#2d2d30",
        "trough": "#2a2a2d",
        "border": "#3e3e42"
    }
}

SCALE_OPTIONS = ["85%", "100%", "115%", "125%", "140%"]

trigger_names = []
active_configs = {}      # { "Left": "TEXT:git status", "Right": "COMBO:17,67" }
trigger_panels = {}      # UI widget references per trigger
led_widgets = {}         # Telemetry LED labels per trigger

clf = None
model_classes = []       # Ordered list of calibrated trigger names in current model
profile_samples = {}     # Stored feature vectors per trigger + noise keys
profile_amplitudes = {}  # Per-profile amplitude thresholds { "Default": 0.35, ... }
is_legacy_model = False
model_loaded = False
is_paused = False

# Window visibility state: True when visible on screen (Telemetry-Only mode)
is_window_open = True

# Thread-safe plain Python mirrors for audio callback
cfg_ignore_typing = True
cfg_bypass_enhancements = True
cfg_vol_threshold = DEFAULT_VOL_THRESHOLD
cfg_conf_threshold = 0.62
cfg_cooldown = 0.45
cfg_theme = "Light"
cfg_scale = 1.0

# Live microphone amplitude telemetry for the visual level bar
latest_peak_vol = 0.0
displayed_peak_vol = 0.0

wasapi_raw_status_text = "Initializing..."

# Typing suppression state
last_physical_keypress_time = 0.0
last_macro_execution_time = 0.0

backend_proc = None
pipe = None
stream = None
current_device_idx = None
current_sr = 44100
current_channels = 2

# Calibration State & Thread Lock
calib_lock = threading.Lock()
calib_session_id = 0
is_calibrating = False
calib_queue = []
calib_idx = 0
taps_collected = 0
calib_append_mode = False
TARGET_TAPS = 10
NEGATIVE_NOISE_TAPS = 8
target_profile_name = "Default"
temp_recorded_features = []
backup_profile_samples = {}

# Cache synthetic noise per sample rate so training is near-instant
synthetic_noise_cache = {}

# More Settings window reference
settings_window = None
raw_status_lbl = None

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
        time.sleep(0.02)
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

# --- 6. AUDIO DEVICE MANAGER WITH PER-APP WASAPI RAW MODE ---
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

def update_raw_status_label():
    if raw_status_lbl is not None and raw_status_lbl.winfo_exists():
        raw_status_lbl.config(text=wasapi_raw_status_text)

def start_audio_stream(device_idx=None):
    global stream, current_device_idx, current_sr, current_channels, wasapi_raw_status_text
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

        current_device_idx = device_idx
        dev_info = sd.query_devices(device_idx, 'input')
        hostapis = sd.query_hostapis()
        api_name = hostapis[dev_info['hostapi']]['name']
        current_sr = int(dev_info['default_samplerate'])
        current_channels = max(1, int(dev_info['max_input_channels']))

        extra_settings = None
        attempted_raw = False
        if cfg_bypass_enhancements:
            if "WASAPI" in api_name.upper():
                try:
                    ws = sd.WasapiSettings(exclusive=False)
                    if hasattr(ws, "_streaminfo") and hasattr(ws._streaminfo, "streamOption"):
                        ws._streaminfo.streamOption = getattr(sd._lib, "eStreamOptionRaw", 1)
                        if hasattr(ws._streaminfo, "streamCategory"):
                            ws._streaminfo.streamCategory = getattr(sd._lib, "eAudioCategoryOther", 0)
                        extra_settings = ws
                        attempted_raw = True
                except Exception:
                    extra_settings = None
            else:
                wasapi_raw_status_text = "🟡 Select a [Windows WASAPI] mic to bypass OS enhancements"
        else:
            wasapi_raw_status_text = "⚪ Off (Using Windows default audio processing)"

        try:
            stream = sd.InputStream(
                device=device_idx,
                samplerate=current_sr,
                channels=current_channels,
                blocksize=int(0.35 * current_sr),
                callback=audio_callback,
                extra_settings=extra_settings
            )
            stream.start()
            if attempted_raw:
                wasapi_raw_status_text = "🟢 Active (WASAPI Raw Mode — OS enhancements bypassed for this app only)"
        except Exception:
            if attempted_raw:
                stream = sd.InputStream(
                    device=device_idx,
                    samplerate=current_sr,
                    channels=current_channels,
                    blocksize=int(0.35 * current_sr),
                    callback=audio_callback
                )
                stream.start()
                wasapi_raw_status_text = "🟡 Fallback (This mic driver rejected Raw Mode; running standard WASAPI)"
            else:
                raise

        update_raw_status_label()
        threading.Thread(target=get_cached_synthetic_noise, args=(current_sr,), daemon=True).start()
        return True
    except Exception as e:
        wasapi_raw_status_text = f"🔴 Audio stream error: {e}"
        update_raw_status_label()
        status_label.config(text=f"🔴 Audio Device Error: {e}", fg="#ff4d4d")
        return False

def on_audio_device_changed(event=None):
    sel = device_combo.get()
    for idx, label, _ in available_devices:
        if label == sel:
            if start_audio_stream(idx):
                status_label.config(text=f"🟢 Switched Mic: {sel[:42]}...", fg="#00aa00")
            break

# --- 7. DSP FEATURE EXTRACTION, KEYCAP FILTER & SYNTHETIC NOISE ---
def is_valid_impulse(audio, check_zcr=True, min_crest=5.0):
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
def get_profile_filename(prof_name):
    return "tap_model.pkl" if prof_name.lower() == "default" else f"model_{prof_name}.pkl"

def get_available_profiles():
    profiles = {}
    if os.path.exists("tap_model.pkl"):
        profiles["Default"] = "tap_model.pkl"
    for f in sorted(glob.glob("model_*.pkl")):
        name = f[6:-4]
        profiles[name] = f
    return profiles

def is_trigger_calibrated(name):
    if name in profile_samples and len(profile_samples[name]) > 0:
        return True
    if is_legacy_model and name in model_classes:
        return True
    return False

def update_card_calibration_badge(name):
    if name not in trigger_panels:
        return
    w = trigger_panels[name]
    t = THEMES[cfg_theme]
    if is_trigger_calibrated(name):
        w["calib_badge"].config(text="✅ Calibrated", fg=t["ok_fg"])
        w["rec_btn"].config(text="🎙️ Re-calibrate (10x)")
    else:
        w["calib_badge"].config(text="⚪ Uncalibrated", fg=t["warn_fg"])
        w["rec_btn"].config(text="🎙️ Calibrate (10x)")

def update_all_calibration_badges():
    for name in trigger_panels:
        update_card_calibration_badge(name)

def update_status_bar_for_window_state():
    if is_calibrating:
        return
    t = THEMES[cfg_theme]
    prof_name = profile_combo.get()
    if not prof_name:
        status_label.config(text="🟡 No Profile Selected — Click '➕ New Profile'", fg=t["warn_fg"])
    elif not model_loaded or len(model_classes) == 0:
        status_label.config(
            text=f"🟡 Profile '{prof_name}' Uncalibrated — Click '🎙️ Calibrate (10x)' on 1 or more triggers",
            fg=t["warn_fg"]
        )
    elif is_paused:
        status_label.config(text="⏸️ Detection Paused", fg=t["warn_fg"])
    elif is_window_open:
        neg_count = len(profile_samples.get("__NEGATIVE_NOISE__", []))
        neg_tag = f" | Neg: {neg_count}" if neg_count > 0 else ""
        cal_count = len(model_classes)
        status_label.config(
            text=f"🟢 '{prof_name}' ({cal_count} calibrated{neg_tag}) | TEST MODE (Minimize to Tray to fire)",
            fg=t["accent_fg"]
        )
    else:
        status_label.config(text=f"🟢 Active Profile: '{prof_name}' (Macros Live)", fg=t["ok_fg"])

def apply_profile_amplitude(prof_name, pkl_fallback=None):
    """Loads the saved amplitude threshold for the given profile without changing other profiles."""
    if prof_name in profile_amplitudes:
        val = float(profile_amplitudes[prof_name])
    elif pkl_fallback is not None:
        val = float(pkl_fallback)
        profile_amplitudes[prof_name] = round(val, 2)
    else:
        val = DEFAULT_VOL_THRESHOLD
        if prof_name:
            profile_amplitudes[prof_name] = DEFAULT_VOL_THRESHOLD

    val = max(0.02, min(0.85, round(val, 2)))
    vol_threshold_var.set(val)
    sync_settings_mirrors(save_to_profile=False)

def load_selected_profile(event=None):
    global clf, model_classes, profile_samples, model_loaded, is_legacy_model
    prof_name = profile_combo.get()
    profiles = get_available_profiles()

    if prof_name in profiles:
        try:
            loaded_obj = joblib.load(profiles[prof_name])
            pkl_vol = None
            if isinstance(loaded_obj, dict) and "samples" in loaded_obj:
                clf = loaded_obj.get("model", None)
                model_classes = list(loaded_obj.get("classes", []))
                profile_samples = {k: list(v) for k, v in loaded_obj.get("samples", {}).items()}
                pkl_vol = loaded_obj.get("vol_threshold", None)
                is_legacy_model = False
                model_loaded = (clf is not None and len(model_classes) > 0)
            else:
                # Legacy raw RandomForestClassifier .pkl
                clf = loaded_obj
                model_classes = ["Left", "Right"]
                profile_samples = {}
                is_legacy_model = True
                model_loaded = (clf is not None)

            apply_profile_amplitude(prof_name, pkl_fallback=pkl_vol)

            # Ensure calibrated classes from this profile exist in the UI
            for c_name in model_classes:
                if c_name not in trigger_panels:
                    add_trigger_ui_panel(c_name)

            update_all_calibration_badges()
            update_status_bar_for_window_state()
        except Exception as e:
            clf = None
            model_classes = []
            profile_samples = {}
            is_legacy_model = False
            model_loaded = False
            update_all_calibration_badges()
            status_label.config(text=f"🔴 Failed to load '{prof_name}': {e}", fg="#ff4d4d")
    else:
        clf = None
        model_classes = []
        profile_samples = {}
        is_legacy_model = False
        model_loaded = False
        apply_profile_amplitude("", pkl_fallback=DEFAULT_VOL_THRESHOLD)
        update_all_calibration_badges()
        update_status_bar_for_window_state()

def save_current_profile_payload(prof_name, model_obj, classes_list, samples_dict):
    filename = get_profile_filename(prof_name)
    amp = profile_amplitudes.get(prof_name, cfg_vol_threshold)
    payload = {
        "model": model_obj,
        "classes": list(classes_list),
        "samples": samples_dict,
        "vol_threshold": round(float(amp), 2)
    }
    joblib.dump(payload, filename)

def train_and_save_profile(prof_name, samples_dict):
    """Trains a Random Forest for 1 or more calibrated triggers + synthetic/negative noise."""
    global clf, model_classes, profile_samples, model_loaded, is_legacy_model
    X, y = [], []
    classes = [k for k in samples_dict.keys() if k not in NOISE_KEYS and len(samples_dict[k]) > 0]

    if len(classes) == 0:
        save_current_profile_payload(prof_name, None, [], samples_dict)
        clf = None
        model_classes = []
        profile_samples = samples_dict
        is_legacy_model = False
        model_loaded = False
        return

    for idx, t_name in enumerate(classes):
        for feat_vec in samples_dict[t_name]:
            X.append(feat_vec)
            y.append(idx)

    # Last class index = Noise (Synthetic Noise + User Negative Audio)
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

    save_current_profile_payload(prof_name, new_clf, classes, samples_dict)

    clf = new_clf
    model_classes = classes
    profile_samples = samples_dict
    is_legacy_model = False
    model_loaded = True

def create_new_profile():
    """Creates a brand-new blank profile with all triggers starting Uncalibrated."""
    name = simpledialog.askstring(
        "New Acoustic Profile",
        "Enter a name for the new profile (e.g., 'Hostel Desk', 'Lap', 'External Mic'):",
        parent=root
    )
    if not name or not name.strip():
        return
    clean_name = "".join(c for c in name.strip() if c.isalnum() or c in (' ', '_', '-')).strip()
    if not clean_name:
        messagebox.showerror("Invalid Name", "Please use standard letters and numbers.")
        return

    display_name = "Default" if clean_name.lower() == "default" else clean_name
    existing = get_available_profiles()
    if display_name in existing:
        if not messagebox.askyesno(
            "Overwrite Profile?",
            f"Profile '{display_name}' already exists.\n\nReset it to a fresh uncalibrated profile?"
        ):
            return

    profile_amplitudes[display_name] = DEFAULT_VOL_THRESHOLD
    save_current_profile_payload(display_name, None, [], {})

    profs = list(get_available_profiles().keys())
    profile_combo.config(values=profs)
    profile_combo.set(display_name)
    load_selected_profile()

    try:
        persist_config_to_disk()
    except Exception:
        pass

def delete_selected_profile():
    """Deletes the currently selected profile .pkl from disk and updates the UI."""
    prof_name = profile_combo.get()
    profiles = get_available_profiles()
    if not prof_name or prof_name not in profiles:
        messagebox.showinfo("Delete Profile", "No profile is currently selected to delete.")
        return

    if not messagebox.askyesno(
        "Delete Profile",
        f"Permanently delete profile '{prof_name}'?\n\nThis removes its calibrated sound model from disk."
    ):
        return

    try:
        os.remove(profiles[prof_name])
    except Exception as e:
        messagebox.showerror("Delete Error", f"Could not delete profile file: {e}")
        return

    profile_amplitudes.pop(prof_name, None)

    remaining = list(get_available_profiles().keys())
    profile_combo.config(values=remaining)
    if remaining:
        profile_combo.set(remaining[0])
    else:
        profile_combo.set("")

    load_selected_profile()
    try:
        persist_config_to_disk()
    except Exception:
        pass

# --- 9. CALIBRATION, RE-RECORD, NEGATIVE AUDIO & CANCEL CONTROLS ---
def get_target_count_for_step(step_name):
    if step_name == "__NEGATIVE_NOISE__":
        return NEGATIVE_NOISE_TAPS
    return TARGET_TAPS

def get_prompt_for_step(prof_name, step_name, count):
    target = get_target_count_for_step(step_name)
    if step_name == "__NEGATIVE_NOISE__":
        return f"🚫 NEGATIVE AUDIO [{prof_name}]: Make the false-trigger sound ({count}/{target})"
    return f"🔵 RECORDING [{prof_name}]: Perform '{step_name}' sound ({count}/{target})"

def set_recording_controls_active(recording):
    state = "disabled" if recording else "normal"
    new_prof_btn.config(state=state)
    del_prof_btn.config(state=state)
    neg_audio_btn.config(state=state)
    settings_btn.config(state=state)
    add_custom_btn.config(state=state)
    save_btn.config(state=state)
    profile_combo.config(state="disabled" if recording else "readonly")
    for w in trigger_panels.values():
        w["rec_btn"].config(state=state)
        w["rename_btn"].config(state=state)
        w["del_btn"].config(state=state)
    # Min Amplitude slider & Reset button stay ENABLED so users can tune sensitivity mid-recording
    if recording:
        cancel_calib_btn.pack(side="right", padx=(6, 0))
        rerecord_calib_btn.pack(side="right", padx=(6, 0))
    else:
        rerecord_calib_btn.pack_forget()
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
    status_label.config(text=get_prompt_for_step(prof_name, first_step, 0), fg=THEMES[cfg_theme]["accent_fg"])

def rerecord_current_step():
    """Resets ONLY the current trigger/step back to 0/N while preserving already-saved steps and profile name."""
    global taps_collected
    with calib_lock:
        if not is_calibrating or calib_idx >= len(calib_queue):
            return
        curr_step = calib_queue[calib_idx]
        temp_recorded_features.clear()
        taps_collected = 0
        prof = target_profile_name

    status_label.config(text=get_prompt_for_step(prof, curr_step, 0), fg="#e68a00")

def cancel_recording():
    """Aborts the entire recording session and restores the previous profile untouched."""
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
    status_label.config(text="🚫 Recording cancelled. Previous profile preserved.", fg="#e68a00")
    root.after(2200, update_status_bar_for_window_state)

def record_single_trigger_sound(trig_name):
    """Calibrates or re-calibrates a single trigger independently (supports 1-trigger setups)."""
    prof_name = profile_combo.get()
    if not prof_name:
        prof_name = "Default"
        profile_amplitudes.setdefault(prof_name, cfg_vol_threshold)
        save_current_profile_payload(prof_name, None, [], {})
        profs = list(get_available_profiles().keys())
        profile_combo.config(values=profs)
        profile_combo.set(prof_name)

    if is_legacy_model:
        ans = messagebox.askyesno(
            "Upgrade Legacy Profile",
            f"'{prof_name}' is a legacy model without stored raw samples for other triggers.\n\n"
            f"Calibrating '{trig_name}' will upgrade this profile so '{trig_name}' works immediately "
            f"(other triggers will become Uncalibrated until you calibrate them).\n\nContinue?"
        )
        if not ans:
            return
        begin_recording_queue([trig_name], prof_name, clear_existing=True, append_mode=False)
        return

    begin_recording_queue([trig_name], prof_name, clear_existing=False, append_mode=False)

def record_negative_audio():
    prof_name = profile_combo.get()
    if not prof_name:
        messagebox.showinfo("No Profile", "Create or select a profile and calibrate at least 1 trigger first.")
        return

    valid_triggers = [k for k in profile_samples.keys() if k not in NOISE_KEYS and len(profile_samples[k]) > 0]
    if len(valid_triggers) < 1:
        messagebox.showinfo(
            "Calibrate a Trigger First",
            f"Profile '{prof_name}' does not have any calibrated triggers with raw samples yet.\n\n"
            f"Click '🎙️ Calibrate (10x)' on at least one trigger first, then record negative audio if needed."
        )
        return

    begin_recording_queue(["__NEGATIVE_NOISE__"], prof_name, clear_existing=False, append_mode=True)

def finish_training_async(prof_name, samples_snapshot, was_negative_only):
    try:
        train_and_save_profile(prof_name, samples_snapshot)
        root.after(0, lambda: on_training_complete_ui(prof_name, was_negative_only))
    except Exception as e:
        root.after(0, lambda err=e: status_label.config(text=f"🔴 Training Error: {err}", fg="#ff4d4d"))
        root.after(0, lambda: set_recording_controls_active(False))

def on_training_complete_ui(prof_name, was_negative_only):
    profs = list(get_available_profiles().keys())
    profile_combo.config(values=profs)
    profile_combo.set("Default" if prof_name.lower() == "default" else prof_name)
    set_recording_controls_active(False)
    update_all_calibration_badges()
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
            f"Saved to profile '{prof_name}'!\nActive calibrated triggers: {', '.join(model_classes)}"
        )

def on_calibration_step_ui(session_id, step_name, count_now, next_step_name, all_done, was_negative_only):
    if session_id != calib_session_id:
        return
    if all_done:
        rerecord_calib_btn.pack_forget()
        cancel_calib_btn.pack_forget()
        status_label.config(text="⏳ Training AI Model...", fg="#e68a00")
        snapshot = {k: list(v) for k, v in profile_samples.items()}
        threading.Thread(
            target=finish_training_async,
            args=(target_profile_name, snapshot, was_negative_only),
            daemon=True
        ).start()
    elif next_step_name is not None:
        status_label.config(text=get_prompt_for_step(target_profile_name, next_step_name, 0), fg=THEMES[cfg_theme]["accent_fg"])
    else:
        status_label.config(text=get_prompt_for_step(target_profile_name, step_name, count_now), fg=THEMES[cfg_theme]["accent_fg"])

# --- 10. REAL-TIME AUDIO CALLBACK ---
last_trigger_time = 0.0
def audio_callback(indata, frames, time_info, status):
    global last_trigger_time, is_calibrating, calib_idx, taps_collected, latest_peak_vol
    if is_paused:
        return

    peak_vol = float(np.max(np.abs(indata)))
    if peak_vol > latest_peak_vol:
        latest_peak_vol = peak_vol

    now = time.time()
    if now - last_trigger_time < cfg_cooldown:
        return

    # 1. Thread-Safe Calibration / Negative Audio Recording Mode
    if is_calibrating:
        with calib_lock:
            if not is_calibrating or calib_idx >= len(calib_queue):
                return
            curr_step = calib_queue[calib_idx]
            active_session = calib_session_id

        is_noise_step = curr_step in NOISE_KEYS
        vol_thresh = max(0.015, cfg_vol_threshold * 0.75) if is_noise_step else cfg_vol_threshold
        if peak_vol <= vol_thresh:
            return

        audio_chunk = indata.copy()
        min_crest = 3.2 if is_noise_step else 5.0
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
    if peak_vol <= cfg_vol_threshold:
        return

    if cfg_ignore_typing and (now - last_physical_keypress_time < 0.45):
        return

    audio_chunk = indata.copy()
    if not is_valid_impulse(audio_chunk, check_zcr=True, min_crest=5.0):
        return

    features = extract_features(audio_chunk, current_sr)

    if model_loaded and clf is not None and len(model_classes) > 0:
        probs = clf.predict_proba(features.reshape(1, -1))[0]
        pred_idx = int(np.argmax(probs))

        if pred_idx < len(model_classes) and probs[pred_idx] >= cfg_conf_threshold:
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
    t = THEMES[cfg_theme]
    has_text = bool(get_text_box_value(w["text"]).strip())
    shortcut_state = "disabled" if has_text else "normal"
    for widget in w["shortcut_widgets"]:
        widget.config(state=shortcut_state)
    w["mode_badge"].config(
        text="Mode: Type String" if has_text else "Mode: Shortcut",
        fg=t["accent_fg"] if has_text else t["muted_fg"]
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

def sync_settings_mirrors(*_, save_to_profile=True):
    """Copies Tk variables to plain Python variables for thread-safe audio callback access."""
    global cfg_ignore_typing, cfg_bypass_enhancements, cfg_vol_threshold, cfg_conf_threshold, cfg_cooldown
    cfg_ignore_typing = bool(ignore_typing_var.get())
    cfg_bypass_enhancements = bool(bypass_enhancements_var.get())
    cfg_vol_threshold = round(float(vol_threshold_var.get()), 2)
    cfg_conf_threshold = round(float(conf_threshold_var.get()), 2)
    cfg_cooldown = round(float(cooldown_var.get()), 2)

    if save_to_profile and "profile_combo" in globals():
        prof = profile_combo.get()
        if prof:
            profile_amplitudes[prof] = cfg_vol_threshold

    if "amp_value_lbl" in globals() and amp_value_lbl.winfo_exists():
        amp_value_lbl.config(text=f"{cfg_vol_threshold:.2f}")

def on_amplitude_release(event=None):
    """Persists per-profile amplitude when user finishes dragging the slider."""
    sync_settings_mirrors(save_to_profile=True)
    try:
        persist_config_to_disk()
    except Exception:
        pass

def reset_amplitude_to_default():
    """Resets the active profile's amplitude threshold back to the original default (0.35)."""
    vol_threshold_var.set(DEFAULT_VOL_THRESHOLD)
    sync_settings_mirrors(save_to_profile=True)
    try:
        persist_config_to_disk()
    except Exception:
        pass

def snapshot_all_trigger_inputs():
    snap = {}
    for name in trigger_names:
        if name in trigger_panels:
            w = trigger_panels[name]
            snap[name] = {
                "text": get_text_box_value(w["text"]),
                "ctrl": w["ctrl"].get(),
                "shift": w["shift"].get(),
                "alt": w["alt"].get(),
                "win": w["win"].get(),
                "key": w["key"].get(),
                "expanded": w["expanded"]
            }
    return snap

def persist_config_to_disk():
    sync_settings_mirrors(save_to_profile=True)
    triggers_block = {}
    for name in trigger_names:
        if name in trigger_panels:
            w = trigger_panels[name]
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

    config_data = {
        "profile": profile_combo.get(),
        "device_index": sel_dev_idx,
        "startup": startup_var.get(),
        "ignore_typing": cfg_ignore_typing,
        "bypass_enhancements": cfg_bypass_enhancements,
        "profile_amplitudes": profile_amplitudes,
        "conf_threshold": cfg_conf_threshold,
        "cooldown": cfg_cooldown,
        "theme": cfg_theme,
        "ui_scale": cfg_scale,
        "trigger_order": trigger_names,
        "triggers": triggers_block
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)

def save_and_apply_config():
    global active_configs
    active_configs.clear()

    for name in trigger_names:
        w = trigger_panels[name]
        update_card_mode_visuals(name)
        cmd = format_command(w)
        if cmd:
            active_configs[name] = cmd

    set_windows_startup(startup_var.get())

    try:
        persist_config_to_disk()
        status_label.config(
            text="💾 Saved! (Minimize window to System Tray to activate macros)",
            fg="#00aa00"
        )
    except Exception as e:
        status_label.config(text=f"🔴 Error saving config: {e}", fg="#ff4d4d")

def load_saved_config():
    """Loads config.json, builds saved triggers, and returns (saved_profile, saved_dev_idx)."""
    global active_configs, cfg_theme, cfg_scale
    startup_var.set(check_windows_startup())

    if not os.path.exists(CONFIG_FILE):
        for default_trig in ["Left", "Right"]:
            add_trigger_ui_panel(default_trig)
        sync_settings_mirrors(save_to_profile=False)
        return None, None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        saved_order = data.get("trigger_order", [])
        if not saved_order:
            saved_order = ["Left", "Right"]
        for t_name in saved_order:
            if t_name not in trigger_panels:
                add_trigger_ui_panel(t_name)

        # Load per-profile amplitudes (with migration from older single vol_threshold)
        saved_amps = data.get("profile_amplitudes", {})
        if isinstance(saved_amps, dict):
            for k, v in saved_amps.items():
                try:
                    profile_amplitudes[k] = round(float(v), 2)
                except Exception:
                    pass
        if not profile_amplitudes and "vol_threshold" in data and data.get("profile"):
            profile_amplitudes[data["profile"]] = round(float(data["vol_threshold"]), 2)

        ignore_typing_var.set(data.get("ignore_typing", True))
        bypass_enhancements_var.set(data.get("bypass_enhancements", True))
        conf_threshold_var.set(float(data.get("conf_threshold", 0.62)))
        cooldown_var.set(float(data.get("cooldown", 0.45)))

        saved_theme = data.get("theme", "Light")
        if saved_theme in THEMES:
            cfg_theme = saved_theme
            theme_var.set(saved_theme)

        saved_scale = float(data.get("ui_scale", 1.0))
        cfg_scale = saved_scale
        scale_str = f"{int(round(saved_scale * 100))}%"
        scale_var.set(scale_str if scale_str in SCALE_OPTIONS else "100%")

        sync_settings_mirrors(save_to_profile=False)

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

        return data.get("profile", None), saved_dev_idx
    except Exception as e:
        print(f"Error loading config: {e}")
        if not trigger_panels:
            for default_trig in ["Left", "Right"]:
                add_trigger_ui_panel(default_trig)
        sync_settings_mirrors(save_to_profile=False)
        return None, None

# --- 12. LIVE MIC AMPLITUDE METER ---
def update_mic_level_meter():
    """Renders the live mic peak bar and current amplitude threshold marker."""
    global latest_peak_vol, displayed_peak_vol
    try:
        if is_window_open and mic_meter_canvas.winfo_exists():
            peak = latest_peak_vol
            latest_peak_vol = 0.0

            if peak >= displayed_peak_vol:
                displayed_peak_vol = peak
            else:
                displayed_peak_vol = max(0.0, displayed_peak_vol * 0.78)

            w = mic_meter_canvas.winfo_width()
            h = mic_meter_canvas.winfo_height()
            if w > 10 and h > 4:
                t = THEMES[cfg_theme]
                mic_meter_canvas.delete("all")
                mic_meter_canvas.create_rectangle(0, 0, w, h, fill=t["meter_bg"], outline=t["border"])

                fill_ratio = min(1.0, displayed_peak_vol)
                bar_w = int(fill_ratio * w)
                crossed = displayed_peak_vol >= cfg_vol_threshold
                bar_color = t["meter_hit_fill"] if crossed else t["meter_sub_fill"]
                if bar_w > 0:
                    mic_meter_canvas.create_rectangle(1, 1, bar_w, h - 1, fill=bar_color, outline="")

                thresh_x = max(2, min(w - 2, int(cfg_vol_threshold * w)))
                mic_meter_canvas.create_line(thresh_x, 0, thresh_x, h, fill="#ff4d4d", width=2)
    except Exception:
        pass
    root.after(75, update_mic_level_meter)

# --- 13. THEME & UI SCALE ENGINE ---
def apply_ui_scale(scale_value, resize_window=True):
    global cfg_scale
    cfg_scale = scale_value

    FONT_BODY.configure(size=max(7, int(round(9 * scale_value))))
    FONT_BOLD.configure(size=max(8, int(round(10 * scale_value))))
    FONT_SMALL.configure(size=max(7, int(round(8 * scale_value))))
    FONT_SMALL_BOLD.configure(size=max(7, int(round(8 * scale_value))))
    FONT_TINY.configure(size=max(6, int(round(7 * scale_value))))
    FONT_ITALIC.configure(size=max(7, int(round(8 * scale_value))))
    FONT_CODE.configure(size=max(7, int(round(9 * scale_value))))
    FONT_BIG_BTN.configure(size=max(9, int(round(11 * scale_value))))

    if resize_window:
        max_w = root.winfo_screenwidth() - 60
        max_h = root.winfo_screenheight() - 90
        w = min(max_w, int(655 * scale_value))
        h = min(max_h, int(765 * scale_value))
        root.geometry(f"{w}x{h}")

    if settings_window is not None and settings_window.winfo_exists():
        sw = min(root.winfo_screenwidth() - 80, int(510 * scale_value))
        sh = min(root.winfo_screenheight() - 100, int(565 * scale_value))
        settings_window.geometry(f"{sw}x{sh}")

    root.update_idletasks()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))

def style_widget_tree(widget, t, is_card=False):
    cls = widget.winfo_class()
    bg_color = t["card_bg"] if is_card else t["bg"]

    if cls in ("Tk", "Toplevel"):
        widget.configure(bg=t["bg"])
    elif cls == "Frame":
        widget.configure(bg=bg_color)
    elif cls == "Labelframe":
        widget.configure(bg=t["card_bg"], fg=t["fg"])
        is_card = True
    elif cls == "Canvas":
        if widget != mic_meter_canvas:
            widget.configure(bg=t["bg"])
    elif cls == "Label":
        if widget not in (status_label, raw_status_lbl) and widget not in led_widgets.values():
            widget.configure(bg=bg_color, fg=t["fg"])
        elif widget in (status_label, raw_status_lbl):
            widget.configure(bg=bg_color)
    elif cls == "Checkbutton":
        widget.configure(
            bg=bg_color, fg=t["fg"],
            activebackground=bg_color, activeforeground=t["fg"],
            selectcolor=t["select_bg"]
        )
    elif cls == "Radiobutton":
        widget.configure(
            bg=bg_color, fg=t["fg"],
            activebackground=bg_color, activeforeground=t["fg"],
            selectcolor=t["select_bg"]
        )
    elif cls == "Entry":
        widget.configure(
            bg=t["input_bg"], fg=t["input_fg"],
            insertbackground=t["insert_bg"],
            disabledbackground=t["bg"], disabledforeground=t["muted_fg"]
        )
    elif cls == "Text":
        widget.configure(
            bg=t["input_bg"], fg=t["input_fg"],
            insertbackground=t["insert_bg"]
        )
    elif cls == "Scale":
        widget.configure(
            bg=bg_color, fg=t["fg"],
            troughcolor=t["trough"],
            activebackground=t["accent_fg"],
            highlightthickness=0
        )
    elif cls == "Button":
        role = getattr(widget, "_theme_role", "default")
        if role == "accent":
            widget.configure(bg=t["btn_accent_bg"], fg=t["btn_accent_fg"], activebackground="#005fa3", activeforeground="#ffffff")
        elif role == "danger":
            widget.configure(bg=t["btn_danger_bg"], fg="#ffffff", activebackground="#a82424", activeforeground="#ffffff")
        elif role == "warn":
            widget.configure(bg=t["btn_warn_bg"], fg="#ffffff", activebackground="#b36b00", activeforeground="#ffffff")
        elif role == "green":
            widget.configure(bg=t["btn_green_bg"], fg=t["fg"], activebackground=t["btn_active"], activeforeground=t["fg"])
        elif role == "orange":
            widget.configure(bg=t["btn_orange_bg"], fg=t["fg"], activebackground=t["btn_active"], activeforeground=t["fg"])
        elif role == "blue":
            widget.configure(bg=t["btn_blue_bg"], fg=t["fg"], activebackground=t["btn_active"], activeforeground=t["fg"])
        elif role == "delete":
            widget.configure(bg=t["btn_bg"], fg=t["del_fg"], activebackground=t["btn_active"], activeforeground=t["del_fg"])
        else:
            widget.configure(bg=t["btn_bg"], fg=t["btn_fg"], activebackground=t["btn_active"], activeforeground=t["btn_fg"])

    for child in widget.winfo_children():
        style_widget_tree(child, t, is_card=is_card)

def apply_theme(theme_name):
    global cfg_theme
    if theme_name not in THEMES:
        return
    cfg_theme = theme_name
    t = THEMES[theme_name]

    ttk_style.configure(
        "TCombobox",
        fieldbackground=t["input_bg"],
        background=t["btn_bg"],
        foreground=t["input_fg"],
        arrowcolor=t["fg"],
        bordercolor=t["border"]
    )
    ttk_style.map(
        "TCombobox",
        fieldbackground=[("readonly", t["input_bg"]), ("disabled", t["bg"])],
        foreground=[("readonly", t["input_fg"]), ("disabled", t["muted_fg"])],
        selectbackground=[("readonly", t["input_bg"])],
        selectforeground=[("readonly", t["input_fg"])]
    )
    ttk_style.configure(
        "Vertical.TScrollbar",
        background=t["btn_bg"],
        troughcolor=t["trough"],
        bordercolor=t["border"],
        arrowcolor=t["fg"]
    )

    root.option_add("*TCombobox*Listbox.background", t["input_bg"])
    root.option_add("*TCombobox*Listbox.foreground", t["input_fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["btn_accent_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    style_widget_tree(root, t, is_card=False)

    for lbl in led_widgets.values():
        lbl.config(bg=t["led_off_bg"], fg=t["led_off_fg"])
    for name in trigger_panels:
        update_card_mode_visuals(name)
        update_card_calibration_badge(name)
    update_status_bar_for_window_state()

    if settings_window is not None and settings_window.winfo_exists():
        style_widget_tree(settings_window, t, is_card=False)

# --- 14. MORE SETTINGS WINDOW ---
def open_more_settings():
    global settings_window, raw_status_lbl
    if settings_window is not None and settings_window.winfo_exists():
        settings_window.deiconify()
        settings_window.lift()
        settings_window.focus_force()
        return

    settings_window = tk.Toplevel(root)
    settings_window.title("More Settings - Acoustic Trigger")
    sw = min(root.winfo_screenwidth() - 80, int(510 * cfg_scale))
    sh = min(root.winfo_screenheight() - 100, int(565 * cfg_scale))
    settings_window.geometry(f"{sw}x{sh}")
    settings_window.configure(padx=14, pady=12)
    settings_window.transient(root)

    def on_close_settings():
        global settings_window, raw_status_lbl
        raw_status_lbl = None
        if settings_window is not None:
            settings_window.destroy()
            settings_window = None

    settings_window.protocol("WM_DELETE_WINDOW", on_close_settings)

    # --- Group 1: Appearance & Scale ---
    app_frame = tk.LabelFrame(settings_window, text="Appearance & Display", font=FONT_BOLD, padx=10, pady=8)
    app_frame.pack(fill="x", pady=(0, 8))
    app_frame.grid_columnconfigure(1, weight=1)

    tk.Label(app_frame, text="Theme:", font=FONT_BODY).grid(row=0, column=0, sticky="w", pady=4)
    theme_combo = ttk.Combobox(app_frame, textvariable=theme_var, values=list(THEMES.keys()), state="readonly", width=16, font=FONT_BODY)
    theme_combo.grid(row=0, column=1, sticky="w", padx=8, pady=4)

    def on_theme_change(event=None):
        apply_theme(theme_var.get())
        try:
            persist_config_to_disk()
        except Exception:
            pass
    theme_combo.bind("<<ComboboxSelected>>", on_theme_change)

    tk.Label(app_frame, text="UI Scale:", font=FONT_BODY).grid(row=1, column=0, sticky="w", pady=4)
    scale_combo = ttk.Combobox(app_frame, textvariable=scale_var, values=SCALE_OPTIONS, state="readonly", width=16, font=FONT_BODY)
    scale_combo.grid(row=1, column=1, sticky="w", padx=8, pady=4)

    def on_scale_change(event=None):
        val = int(scale_var.get().replace("%", "")) / 100.0
        apply_ui_scale(val, resize_window=True)
        try:
            persist_config_to_disk()
        except Exception:
            pass
    scale_combo.bind("<<ComboboxSelected>>", on_scale_change)

    # --- Group 2: Audio Engine & Per-App Enhancement Bypass ---
    audio_frame = tk.LabelFrame(settings_window, text="Audio Engine & Sensitivity", font=FONT_BOLD, padx=10, pady=8)
    audio_frame.pack(fill="x", pady=(0, 8))
    audio_frame.grid_columnconfigure(1, weight=1)

    def on_bypass_toggle():
        sync_settings_mirrors(save_to_profile=False)
        start_audio_stream(current_device_idx)
        try:
            persist_config_to_disk()
        except Exception:
            pass

    bypass_chk = tk.Checkbutton(
        audio_frame,
        text="Bypass Windows Audio Enhancements (App-Only WASAPI Raw Mode)",
        variable=bypass_enhancements_var,
        font=FONT_SMALL_BOLD,
        command=on_bypass_toggle
    )
    bypass_chk.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))

    raw_status_lbl = tk.Label(audio_frame, text=wasapi_raw_status_text, font=FONT_SMALL, fg=THEMES[cfg_theme]["muted_fg"], anchor="w", justify="left", wraplength=440)
    raw_status_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=(0, 8))

    tk.Label(audio_frame, text="Profile Min Amplitude:", font=FONT_BODY).grid(row=2, column=0, sticky="w", pady=2)
    vol_slider = tk.Scale(
        audio_frame, from_=0.02, to=0.85, resolution=0.01, orient="horizontal",
        variable=vol_threshold_var, font=FONT_TINY, command=lambda _: sync_settings_mirrors(save_to_profile=True)
    )
    vol_slider.grid(row=2, column=1, sticky="ew", padx=6)
    vol_slider.bind("<ButtonRelease-1>", on_amplitude_release)

    vol_reset_btn = tk.Button(audio_frame, text="↺ Reset", font=FONT_TINY, padx=6, command=reset_amplitude_to_default)
    vol_reset_btn.grid(row=2, column=2, padx=(2, 0))

    tk.Label(audio_frame, text="AI Confidence Strictness:", font=FONT_BODY).grid(row=3, column=0, sticky="w", pady=2)
    conf_slider = tk.Scale(
        audio_frame, from_=0.45, to=0.90, resolution=0.02, orient="horizontal",
        variable=conf_threshold_var, font=FONT_TINY, command=lambda _: sync_settings_mirrors(save_to_profile=False)
    )
    conf_slider.grid(row=3, column=1, columnspan=2, sticky="ew", padx=6)

    tk.Label(audio_frame, text="Trigger Cooldown (sec):", font=FONT_BODY).grid(row=4, column=0, sticky="w", pady=2)
    cool_slider = tk.Scale(
        audio_frame, from_=0.15, to=1.50, resolution=0.05, orient="horizontal",
        variable=cooldown_var, font=FONT_TINY, command=lambda _: sync_settings_mirrors(save_to_profile=False)
    )
    cool_slider.grid(row=4, column=1, columnspan=2, sticky="ew", padx=6)

    # --- Group 3: System & Protection ---
    beh_frame = tk.LabelFrame(settings_window, text="Behavior & System", font=FONT_BOLD, padx=10, pady=8)
    beh_frame.pack(fill="x", pady=(0, 10))

    typing_chk = tk.Checkbutton(
        beh_frame,
        text="🛡️ Suppress triggers while physically typing on keyboard",
        variable=ignore_typing_var,
        font=FONT_BODY,
        command=lambda: sync_settings_mirrors(save_to_profile=False)
    )
    typing_chk.pack(anchor="w", pady=2)

    startup_chk = tk.Checkbutton(
        beh_frame,
        text="Run automatically on Windows Startup (starts minimized in System Tray)",
        variable=startup_var,
        font=FONT_BODY,
        command=lambda: set_windows_startup(startup_var.get())
    )
    startup_chk.pack(anchor="w", pady=2)

    # --- Bottom Action Bar ---
    btn_row = tk.Frame(settings_window)
    btn_row.pack(fill="x", pady=(4, 0))

    def reset_defaults():
        theme_var.set("Light")
        scale_var.set("100%")
        bypass_enhancements_var.set(True)
        vol_threshold_var.set(DEFAULT_VOL_THRESHOLD)
        conf_threshold_var.set(0.62)
        cooldown_var.set(0.45)
        ignore_typing_var.set(True)
        sync_settings_mirrors(save_to_profile=True)
        apply_theme("Light")
        apply_ui_scale(1.0, resize_window=True)
        start_audio_stream(current_device_idx)
        try:
            persist_config_to_disk()
        except Exception:
            pass

    reset_btn = tk.Button(btn_row, text="Reset Defaults", font=FONT_SMALL_BOLD, padx=10, pady=4, command=reset_defaults)
    reset_btn.pack(side="left")

    def save_and_close():
        sync_settings_mirrors(save_to_profile=True)
        set_windows_startup(startup_var.get())
        try:
            persist_config_to_disk()
        except Exception:
            pass
        on_close_settings()

    done_btn = tk.Button(btn_row, text="💾 Save & Close", font=FONT_SMALL_BOLD, padx=14, pady=4, command=save_and_close)
    done_btn._theme_role = "accent"
    done_btn.pack(side="right")

    apply_theme(cfg_theme)

# --- 15. DYNAMIC UI PANELS, RENAME, DELETE & TELEMETRY ---
def flash_indicator(trig_name):
    if trig_name in led_widgets:
        t = THEMES[cfg_theme]
        lbl = led_widgets[trig_name]
        lbl.config(bg=t["led_on_bg"], fg=t["led_on_fg"])
        root.after(300, lambda l=lbl: l.config(bg=THEMES[cfg_theme]["led_off_bg"], fg=THEMES[cfg_theme]["led_off_fg"]))

def rebuild_led_telemetry():
    for child in led_badges_container.winfo_children():
        child.destroy()
    led_widgets.clear()

    t = THEMES[cfg_theme]
    cols = 3
    for i, name in enumerate(trigger_names):
        r, c = divmod(i, cols)
        lbl = tk.Label(
            led_badges_container, text=f"{name.upper()}", font=FONT_SMALL_BOLD,
            bg=t["led_off_bg"], fg=t["led_off_fg"], width=16, pady=5, relief="sunken"
        )
        lbl.grid(row=r, column=c, padx=5, pady=3, sticky="ew")
        led_badges_container.grid_columnconfigure(c, weight=1)
        led_widgets[name] = lbl

def rebuild_trigger_cards_from_snapshot(snap):
    """Cleanly rebuilds all trigger cards in trigger_names order so renamed callbacks are never stale."""
    for child in scrollable_triggers_frame.winfo_children():
        child.destroy()
    trigger_panels.clear()

    ordered_names = list(trigger_names)
    trigger_names.clear()
    for name in ordered_names:
        add_trigger_ui_panel(name, rebuild_leds=False)
        if name in snap:
            s = snap[name]
            w = trigger_panels[name]
            w["text"].delete("1.0", tk.END)
            w["text"].insert("1.0", s["text"])
            w["ctrl"].set(s["ctrl"])
            w["shift"].set(s["shift"])
            w["alt"].set(s["alt"])
            w["win"].set(s["win"])
            w["key"].delete(0, tk.END)
            w["key"].insert(0, s["key"])
            if s.get("expanded", False):
                toggle_text_box_expand(name)
            update_card_mode_visuals(name)

    rebuild_led_telemetry()
    update_all_calibration_badges()
    root.update_idletasks()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))

def rename_trigger(old_name):
    """Renames any trigger (including default Left and Right) across UI, config, and active model."""
    global is_legacy_model
    raw_new = simpledialog.askstring(
        "Rename Trigger",
        f"Enter a new name for '{old_name}':",
        initialvalue=old_name,
        parent=root
    )
    if not raw_new or not raw_new.strip():
        return

    new_name = "".join(c for c in raw_new.strip() if c.isalnum() or c in (' ', '_', '-')).strip()
    if not new_name or new_name in NOISE_KEYS:
        messagebox.showerror("Invalid Name", "Please use standard letters and numbers.")
        return
    if new_name == old_name:
        return
    if new_name in trigger_panels:
        messagebox.showwarning("Duplicate Name", f"A trigger named '{new_name}' already exists.")
        return

    snap = snapshot_all_trigger_inputs()
    if old_name in snap:
        snap[new_name] = snap.pop(old_name)

    idx = trigger_names.index(old_name)
    trigger_names[idx] = new_name

    if old_name in active_configs:
        active_configs[new_name] = active_configs.pop(old_name)

    model_changed = False
    if old_name in profile_samples:
        profile_samples[new_name] = profile_samples.pop(old_name)
        model_changed = True
    if old_name in model_classes:
        model_classes[model_classes.index(old_name)] = new_name
        model_changed = True

    prof_name = profile_combo.get()
    if model_changed and prof_name:
        try:
            save_current_profile_payload(prof_name, clf, model_classes, profile_samples)
            if not is_legacy_model:
                pass
            else:
                # Keeping legacy flag true if no raw samples exist so badge stays Calibrated
                is_legacy_model = (len([k for k in profile_samples if k not in NOISE_KEYS]) == 0)
        except Exception as e:
            print(f"Error saving renamed profile: {e}")

    rebuild_trigger_cards_from_snapshot(snap)
    update_status_bar_for_window_state()
    try:
        persist_config_to_disk()
    except Exception:
        pass

def delete_trigger(name):
    if len(trigger_names) <= 1:
        messagebox.showwarning("Cannot Delete", "You must keep at least 1 trigger.")
        return
    if not messagebox.askyesno("Delete Trigger", f"Remove trigger '{name}'?"):
        return

    trigger_names.remove(name)
    active_configs.pop(name, None)
    panel = trigger_panels.pop(name, None)
    if panel:
        panel["frame"].destroy()

    prof_name = profile_combo.get()
    if name in profile_samples:
        profile_samples.pop(name, None)
        if prof_name:
            train_and_save_profile(prof_name, profile_samples)

    rebuild_led_telemetry()
    update_all_calibration_badges()
    update_status_bar_for_window_state()
    triggers_canvas.configure(scrollregion=triggers_canvas.bbox("all"))
    try:
        persist_config_to_disk()
    except Exception:
        pass

def add_trigger_ui_panel(name, rebuild_leds=True):
    if name not in trigger_names:
        trigger_names.append(name)

    frame = tk.LabelFrame(
        scrollable_triggers_frame, text=f"⚡ {name} Trigger",
        font=FONT_BOLD, padx=10, pady=6
    )
    frame.pack(fill="x", pady=4, padx=4)
    frame.grid_columnconfigure(1, weight=1)

    card_top = tk.Frame(frame)
    card_top.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 4))

    rec_btn = tk.Button(
        card_top, text="🎙️ Calibrate (10x)", font=FONT_SMALL_BOLD,
        command=lambda n=name: record_single_trigger_sound(n)
    )
    rec_btn._theme_role = "blue"
    rec_btn.pack(side="left")

    calib_badge = tk.Label(card_top, text="⚪ Uncalibrated", font=FONT_SMALL_BOLD)
    calib_badge.pack(side="left", padx=(8, 4))

    mode_badge = tk.Label(card_top, text="Mode: Shortcut", font=FONT_ITALIC)
    mode_badge.pack(side="left", padx=(6, 4))

    del_btn = tk.Button(
        card_top, text="🗑️", font=FONT_SMALL, padx=4,
        command=lambda n=name: delete_trigger(n)
    )
    del_btn._theme_role = "delete"
    del_btn.pack(side="right", padx=(4, 0))

    rename_btn = tk.Button(
        card_top, text="✏️ Rename", font=FONT_SMALL, padx=5,
        command=lambda n=name: rename_trigger(n)
    )
    rename_btn.pack(side="right")

    # Expandable Multi-Line Type String Row
    label_col = tk.Frame(frame)
    label_col.grid(row=1, column=0, sticky="nw", pady=2)
    tk.Label(label_col, text="Type String:", font=FONT_BODY).pack(anchor="w")
    expand_btn = tk.Button(
        label_col, text="⤢ Expand", font=FONT_TINY, padx=4, pady=0,
        command=lambda n=name: toggle_text_box_expand(n)
    )
    expand_btn.pack(anchor="w", pady=(2, 0))

    text_wrapper = tk.Frame(frame)
    text_wrapper.grid(row=1, column=1, columnspan=6, sticky="ew", padx=5, pady=2)
    text_wrapper.grid_columnconfigure(0, weight=1)

    text_box = tk.Text(text_wrapper, height=2, width=38, font=FONT_CODE, wrap="word", undo=True)
    text_scroll = ttk.Scrollbar(text_wrapper, orient="vertical", command=text_box.yview)
    text_box.configure(yscrollcommand=text_scroll.set)
    text_box.grid(row=0, column=0, sticky="ew")
    text_scroll.grid(row=0, column=1, sticky="ns")

    text_box.bind("<KeyRelease>", lambda e, n=name: update_card_mode_visuals(n))
    text_box.bind("<<Paste>>", lambda e, n=name: root.after(10, lambda: update_card_mode_visuals(n)))

    # Shortcut Row
    tk.Label(frame, text="Shortcut:", font=FONT_BODY).grid(row=2, column=0, sticky="w", pady=(4, 0))

    ctrl = tk.BooleanVar()
    shift = tk.BooleanVar()
    alt = tk.BooleanVar()
    win = tk.BooleanVar()

    shortcut_row = tk.Frame(frame)
    shortcut_row.grid(row=2, column=1, columnspan=6, sticky="w", padx=3, pady=(4, 0))

    cb_ctrl = tk.Checkbutton(shortcut_row, text="Ctrl", variable=ctrl, font=FONT_BODY)
    cb_shift = tk.Checkbutton(shortcut_row, text="Shift", variable=shift, font=FONT_BODY)
    cb_alt = tk.Checkbutton(shortcut_row, text="Alt", variable=alt, font=FONT_BODY)
    cb_win = tk.Checkbutton(shortcut_row, text="Win", variable=win, font=FONT_BODY)

    cb_ctrl.pack(side="left")
    cb_shift.pack(side="left")
    cb_alt.pack(side="left")
    cb_win.pack(side="left")

    tk.Label(shortcut_row, text="+", font=FONT_BODY).pack(side="left", padx=2)
    key_entry = tk.Entry(shortcut_row, width=6, font=FONT_BODY)
    key_entry.pack(side="left", padx=4)

    trigger_panels[name] = {
        "frame": frame,
        "rec_btn": rec_btn,
        "calib_badge": calib_badge,
        "rename_btn": rename_btn,
        "del_btn": del_btn,
        "text": text_box,
        "expand_btn": expand_btn,
        "expanded": False,
        "ctrl": ctrl, "shift": shift, "alt": alt, "win": win,
        "key": key_entry,
        "mode_badge": mode_badge,
        "shortcut_widgets": [cb_ctrl, cb_shift, cb_alt, cb_win, key_entry]
    }

    style_widget_tree(frame, THEMES[cfg_theme], is_card=True)
    update_card_mode_visuals(name)
    update_card_calibration_badge(name)
    if rebuild_leds:
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
    clean_name = "".join(c for c in raw_name.strip() if c.isalnum() or c in (' ', '_', '-')).strip()
    if not clean_name or clean_name in NOISE_KEYS:
        messagebox.showerror("Invalid Name", "Please use letters and numbers.")
        return
    if clean_name in trigger_panels:
        messagebox.showwarning("Duplicate", f"Trigger '{clean_name}' already exists.")
        return

    add_trigger_ui_panel(clean_name)
    if messagebox.askyesno("Calibrate Custom Trigger", f"Trigger '{clean_name}' added (Uncalibrated).\n\nCalibrate its sound (10x) right now?"):
        record_single_trigger_sound(clean_name)

# --- 16. WINDOW VISIBILITY (MINIMIZE -> TRAY, CLOSE -> EXIT) ---
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
    if settings_window is not None and settings_window.winfo_exists():
        settings_window.withdraw()
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

# --- 17. BUILD MAIN GUI ---
root = tk.Tk()
root.title("Acoustic Trigger Control Center")
root.geometry("655x765")
root.configure(padx=12, pady=10)
root.protocol("WM_DELETE_WINDOW", quit_application)

ttk_style = ttk.Style(root)
try:
    ttk_style.theme_use("clam")
except Exception:
    pass

# Scalable Named Fonts
FONT_BODY = tkfont.Font(family="Arial", size=9)
FONT_BOLD = tkfont.Font(family="Arial", size=10, weight="bold")
FONT_SMALL = tkfont.Font(family="Arial", size=8)
FONT_SMALL_BOLD = tkfont.Font(family="Arial", size=8, weight="bold")
FONT_TINY = tkfont.Font(family="Arial", size=7)
FONT_ITALIC = tkfont.Font(family="Arial", size=8, slant="italic")
FONT_CODE = tkfont.Font(family="Consolas", size=9)
FONT_BIG_BTN = tkfont.Font(family="Arial", size=11, weight="bold")

# Settings Tkinter Variables
theme_var = tk.StringVar(value="Light")
scale_var = tk.StringVar(value="100%")
bypass_enhancements_var = tk.BooleanVar(value=True)
ignore_typing_var = tk.BooleanVar(value=True)
startup_var = tk.BooleanVar(value=check_windows_startup())
vol_threshold_var = tk.DoubleVar(value=DEFAULT_VOL_THRESHOLD)
conf_threshold_var = tk.DoubleVar(value=0.62)
cooldown_var = tk.DoubleVar(value=0.45)

# Section 1: System, Profile, Audio Device & More Settings
sys_frame = tk.LabelFrame(root, text="Device Profile & Audio Hardware", font=FONT_BOLD, padx=10, pady=8)
sys_frame.pack(fill="x", pady=(0, 8))
sys_frame.grid_columnconfigure(1, weight=1)

tk.Label(sys_frame, text="Acoustic Profile:", font=FONT_BODY).grid(row=0, column=0, sticky="w", pady=3)
available_profs = list(get_available_profiles().keys())
profile_combo = ttk.Combobox(sys_frame, values=available_profs, state="readonly", width=18, font=FONT_BODY)
profile_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
profile_combo.bind("<<ComboboxSelected>>", load_selected_profile)

prof_btns = tk.Frame(sys_frame)
prof_btns.grid(row=0, column=2, sticky="e", pady=3)

new_prof_btn = tk.Button(prof_btns, text="➕ New Profile", font=FONT_SMALL_BOLD, command=create_new_profile)
new_prof_btn.pack(side="left", padx=(0, 4))

del_prof_btn = tk.Button(prof_btns, text="🗑️ Delete", font=FONT_SMALL_BOLD, command=delete_selected_profile)
del_prof_btn._theme_role = "delete"
del_prof_btn.pack(side="left", padx=(0, 4))

neg_audio_btn = tk.Button(
    prof_btns, text="🚫 Negative Audio", font=FONT_SMALL_BOLD,
    command=record_negative_audio
)
neg_audio_btn._theme_role = "orange"
neg_audio_btn.pack(side="left")

tk.Label(sys_frame, text="Audio Input Mic:", font=FONT_BODY).grid(row=1, column=0, sticky="w", pady=3)
available_devices = get_audio_input_devices()
device_labels = [label for _, label, _ in available_devices]
device_combo = ttk.Combobox(sys_frame, values=device_labels, state="readonly", font=FONT_BODY)
device_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
if device_labels:
    device_combo.set(device_labels[0])
device_combo.bind("<<ComboboxSelected>>", on_audio_device_changed)

settings_btn = tk.Button(sys_frame, text="⚙️ More Settings", font=FONT_SMALL_BOLD, command=open_more_settings)
settings_btn.grid(row=1, column=2, sticky="e", padx=(4, 0), pady=3)

# Section 2: Custom Trigger Header & Scrollable Trigger List
header_bar = tk.Frame(root)
header_bar.pack(fill="x", pady=(2, 4))
tk.Label(header_bar, text="Trigger Mappings (Empty String = Shortcut Mode)", font=FONT_BOLD).pack(side="left")
add_custom_btn = tk.Button(
    header_bar, text="➕ Add Custom Trigger", font=FONT_SMALL_BOLD,
    command=prompt_new_custom_trigger
)
add_custom_btn._theme_role = "green"
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
    if isinstance(event.widget, tk.Text):
        first, last = event.widget.yview()
        if not (first <= 0.0 and last >= 1.0):
            event.widget.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return
    triggers_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
triggers_canvas.bind_all("<MouseWheel>", _on_mousewheel)

# Section 3: Save Button
save_btn = tk.Button(
    root, text="💾 Save & Apply Configuration", font=FONT_BIG_BTN,
    pady=6, command=save_and_apply_config
)
save_btn._theme_role = "accent"
save_btn.pack(fill="x", pady=6)

# Section 4: Live Detection LEDs + Live Mic Level & Per-Profile Amplitude Threshold Slider
led_frame = tk.LabelFrame(root, text="Live Sensor Telemetry & Per-Profile Sensitivity", font=FONT_BOLD, padx=10, pady=8)
led_frame.pack(fill="x", pady=4)

led_badges_container = tk.Frame(led_frame)
led_badges_container.pack(fill="x", pady=(0, 6))

meter_row = tk.Frame(led_frame)
meter_row.pack(fill="x", pady=(0, 4))
tk.Label(meter_row, text="Mic Level:", font=FONT_SMALL_BOLD, width=14, anchor="w").pack(side="left")
mic_meter_canvas = tk.Canvas(meter_row, height=14, highlightthickness=0)
mic_meter_canvas.pack(side="left", fill="x", expand=True, padx=(4, 0))

amp_row = tk.Frame(led_frame)
amp_row.pack(fill="x")
tk.Label(amp_row, text="Min Amplitude:", font=FONT_SMALL_BOLD, width=14, anchor="w").pack(side="left")
main_amp_slider = tk.Scale(
    amp_row, from_=0.02, to=0.85, resolution=0.01, orient="horizontal",
    variable=vol_threshold_var, showvalue=False,
    command=lambda _: sync_settings_mirrors(save_to_profile=True)
)
main_amp_slider.pack(side="left", fill="x", expand=True, padx=4)
main_amp_slider.bind("<ButtonRelease-1>", on_amplitude_release)

amp_value_lbl = tk.Label(amp_row, text=f"{cfg_vol_threshold:.2f}", font=FONT_SMALL_BOLD, width=5, anchor="e")
amp_value_lbl.pack(side="left", padx=(0, 4))

amp_reset_btn = tk.Button(
    amp_row, text="↺ Reset", font=FONT_TINY, padx=6, pady=1,
    command=reset_amplitude_to_default
)
amp_reset_btn.pack(side="right")

# Section 5: Status Bar + Dynamic Re-record & Cancel Buttons
status_frame = tk.Frame(root)
status_frame.pack(fill="x", pady=(4, 0))

status_label = tk.Label(status_frame, text="Initializing...", font=FONT_SMALL_BOLD, anchor="w")
status_label.pack(side="left", fill="x", expand=True)

rerecord_calib_btn = tk.Button(
    status_frame, text="🔄 Re-record", font=FONT_SMALL_BOLD,
    padx=8, command=rerecord_current_step
)
rerecord_calib_btn._theme_role = "warn"

cancel_calib_btn = tk.Button(
    status_frame, text="❌ Cancel", font=FONT_SMALL_BOLD,
    padx=8, command=cancel_recording
)
cancel_calib_btn._theme_role = "danger"

# --- 18. INITIALIZE APP STATE ---
start_cpp_backend_and_connect()
threading.Thread(target=keyboard_activity_monitor, daemon=True).start()

saved_prof, saved_mic_idx = load_saved_config()

available_profs = list(get_available_profiles().keys())
profile_combo.config(values=available_profs)
if saved_prof and saved_prof in available_profs:
    profile_combo.set(saved_prof)
    load_selected_profile()
elif "Default" in available_profs:
    profile_combo.set("Default")
    load_selected_profile()
elif available_profs:
    profile_combo.set(available_profs[0])
    load_selected_profile()
else:
    update_all_calibration_badges()
    update_status_bar_for_window_state()

apply_theme(cfg_theme)
apply_ui_scale(cfg_scale, resize_window=(cfg_scale != 1.0))
start_audio_stream(saved_mic_idx)

threading.Thread(target=run_system_tray, daemon=True).start()

if "--minimized" in sys.argv:
    hide_to_system_tray()

poll_window_state()
update_mic_level_meter()
root.mainloop()