import sounddevice as sd
import numpy as np
import librosa
import joblib
import time
import tkinter as tk

try:
    clf = joblib.load("tap_model.pkl")
    pipe = open(r'\\.\pipe\AcousticTriggerPipe', 'w')
except:
    pipe = None

target_device = sd.default.device[0]
for i, dev in enumerate(sd.query_devices()):
    if dev['max_input_channels'] > 0 and sd.query_hostapis(dev['hostapi'])['name'] == 'Windows WASAPI':
        target_device = i
        break

sd.default.device = target_device
device_info = sd.query_devices(target_device, 'input')
SAMPLE_RATE = int(device_info['default_samplerate'])
CHANNELS = device_info['max_input_channels']

# Helper to build the command string sent to C++
def build_command(mode_var, text_entry, ctrl_var, shift_var, alt_var, win_var, key_entry):
    if mode_var.get() == "TEXT":
        return f"TEXT:{text_entry.get()}"
    else:
        vk_codes = []
        if ctrl_var.get(): vk_codes.append("17")  # VK_CONTROL
        if shift_var.get(): vk_codes.append("16") # VK_SHIFT
        if alt_var.get(): vk_codes.append("18")   # VK_MENU
        if win_var.get(): vk_codes.append("91")   # VK_LWIN
        
        base_key = key_entry.get().upper()
        if base_key:
            vk_codes.append(str(ord(base_key[0]))) # Convert character to ASCII/VK code
            
        if not vk_codes: return ""
        return f"COMBO:{','.join(vk_codes)}"

def process_and_predict(audio):
    if len(audio.shape) > 1: audio = audio.T
    else: audio = audio.reshape(2, -1)
        
    max_vol = np.max(np.abs(audio))
    if max_vol > 0: audio = audio / max_vol
        
    features = []
    expected_length = int(0.2 * SAMPLE_RATE)
    correlation = np.correlate(audio[0], audio[1], mode='full')
    features.append(np.argmax(correlation) - (len(audio[1]) - 1))
    
    for ch in range(audio.shape[0]):
        channel_data = audio[ch]
        peak_index = np.argmax(np.abs(channel_data))
        start = max(0, peak_index - int(0.05 * SAMPLE_RATE))
        end = min(len(channel_data), peak_index + int(0.15 * SAMPLE_RATE))
        cropped = channel_data[start:end]
        if len(cropped) < expected_length:
            cropped = np.pad(cropped, (0, expected_length - len(cropped)))
        mfccs = librosa.feature.mfcc(y=cropped, sr=SAMPLE_RATE, n_mfcc=13)
        features.extend(np.mean(mfccs.T, axis=0))
            
    prediction = clf.predict(np.array(features).reshape(1, -1))[0]
    confidence = clf.predict_proba(np.array(features).reshape(1, -1))[0]
    
    if prediction == 0 and confidence[0] >= 0.55:
        cmd = build_command(l_mode, l_text, l_ctrl, l_shift, l_alt, l_win, l_key)
        if pipe and cmd:
            pipe.write(f"LEFT:{cmd}\n")
            pipe.flush()
            
    elif prediction == 1 and confidence[1] >= 0.55:
        cmd = build_command(r_mode, r_text, r_ctrl, r_shift, r_alt, r_win, r_key)
        if pipe and cmd:
            pipe.write(f"RIGHT:{cmd}\n")
            pipe.flush()

last_trigger_time = 0
def audio_callback(indata, frames, time_info, status):
    global last_trigger_time
    if time.time() - last_trigger_time < 0.5: return
    if np.max(np.abs(indata)) > 0.4:
        process_and_predict(indata.copy())
        last_trigger_time = time.time()

# --- BUILD THE NEW GUI ---
root = tk.Tk()
root.title("Acoustic Trigger Settings")
root.geometry("450x300")
root.configure(padx=10, pady=10)

def create_tap_panel(parent, title):
    frame = tk.LabelFrame(parent, text=title, font=("Arial", 10, "bold"), padx=10, pady=10)
    frame.pack(fill="x", pady=5)
    
    mode = tk.StringVar(value="TEXT")
    
    tk.Radiobutton(frame, text="Type String:", variable=mode, value="TEXT").grid(row=0, column=0, sticky="w")
    text_entry = tk.Entry(frame, width=30)
    text_entry.grid(row=0, column=1, columnspan=5, sticky="w", padx=5)
    
    tk.Radiobutton(frame, text="Shortcut:", variable=mode, value="COMBO").grid(row=1, column=0, sticky="w")
    
    ctrl = tk.BooleanVar()
    shift = tk.BooleanVar()
    alt = tk.BooleanVar()
    win = tk.BooleanVar()
    
    tk.Checkbutton(frame, text="Ctrl", variable=ctrl).grid(row=1, column=1)
    tk.Checkbutton(frame, text="Shift", variable=shift).grid(row=1, column=2)
    tk.Checkbutton(frame, text="Alt", variable=alt).grid(row=1, column=3)
    tk.Checkbutton(frame, text="Win", variable=win).grid(row=1, column=4)
    
    tk.Label(frame, text="+").grid(row=1, column=5)
    key_entry = tk.Entry(frame, width=5)
    key_entry.grid(row=1, column=6, padx=5)
    
    return mode, text_entry, ctrl, shift, alt, win, key_entry

l_mode, l_text, l_ctrl, l_shift, l_alt, l_win, l_key = create_tap_panel(root, "Left Palm Tap")
l_text.insert(0, "git status")

r_mode, r_text, r_ctrl, r_shift, r_alt, r_win, r_key = create_tap_panel(root, "Right Palm Tap")
r_mode.set("COMBO") # Default right tap to shortcut mode
r_ctrl.set(True)    # Default to Ctrl + C
r_key.insert(0, "C")

tk.Label(root, text="🟢 Sensor Active" if pipe else "🟡 No C++ Backend", fg="green" if pipe else "orange").pack(pady=5)

try:
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=int(0.5 * SAMPLE_RATE), callback=audio_callback)
    stream.start()
    root.mainloop() 
finally:
    stream.stop()
    if pipe: pipe.close()