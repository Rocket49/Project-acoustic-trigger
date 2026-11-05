import sounddevice as sd
import numpy as np
import librosa
import joblib
import time

try:
    clf = joblib.load("tap_model.pkl")
    print("Loaded tap_model.pkl successfully!")
except FileNotFoundError:
    print("Error: tap_model.pkl not found.")
    exit()

# --- NEW: IPC CONNECTION TO C++ ---
try:
    pipe = open(r'\\.\pipe\AcousticTriggerPipe', 'w')
    print("✅ Connected to C++ Named Pipe!")
except FileNotFoundError:
    print("⚠️ C++ Backend not found. Starting in Standalone Python Mode.")
    pipe = None
# ----------------------------------

target_device = sd.default.device[0]
for i, dev in enumerate(sd.query_devices()):
    if dev['max_input_channels'] > 0 and sd.query_hostapis(dev['hostapi'])['name'] == 'Windows WASAPI':
        target_device = i
        break

sd.default.device = target_device
device_info = sd.query_devices(target_device, 'input')
SAMPLE_RATE = int(device_info['default_samplerate'])
CHANNELS = device_info['max_input_channels']

def process_and_predict(audio):
    if len(audio.shape) > 1:
        audio = audio.T
    else:
        audio = audio.reshape(2, -1)
        
    max_vol = np.max(np.abs(audio))
    if max_vol > 0: 
        audio = audio / max_vol
        
    features = []
    expected_length = int(0.2 * SAMPLE_RATE)
    
    left_channel = audio[0]
    right_channel = audio[1]
    correlation = np.correlate(left_channel, right_channel, mode='full')
    delay = np.argmax(correlation) - (len(right_channel) - 1)
    features.append(delay)
    
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
            
    features = np.array(features).reshape(1, -1)
    
    prediction = clf.predict(features)[0]
    confidence = clf.predict_proba(features)[0]
    
    MIN_CONFIDENCE = 0.55 
    
    # --- UPDATED: SENDING SIGNALS TO C++ ---
    if prediction == 0 and confidence[0] >= MIN_CONFIDENCE:
        print(f"🎯 LEFT TAP  [{confidence[0]*100:.0f}%] (Delay: {delay})")
        if pipe:
            pipe.write("LEFT\n")
            pipe.flush() # Force the packet through the pipe instantly
            
    elif prediction == 1 and confidence[1] >= MIN_CONFIDENCE:
        print(f"🎯 RIGHT TAP [{confidence[1]*100:.0f}%] (Delay: {delay})")
        if pipe:
            pipe.write("RIGHT\n")
            pipe.flush()
            
    else:
        max_conf = np.max(confidence)
        if prediction == 2:
            print(f"🗣️ IGNORED (Recognized Noise) [{max_conf*100:.0f}%]")
        else:
            print(f"🗣️ IGNORED (Too Uncertain) [{max_conf*100:.0f}%]")
    # ---------------------------------------

last_trigger_time = 0

def audio_callback(indata, frames, time_info, status):
    global last_trigger_time
    current_time = time.time()
    
    if current_time - last_trigger_time < 0.5:
        return
        
    volume = np.max(np.abs(indata))
    threshold = 0.4 
    
    if volume > threshold:
        print(f"\n[Spike Detected: {volume:.4f}]")
        process_and_predict(indata.copy())
        last_trigger_time = current_time

print("\n--- Real-Time System Daemon Active ---")
print("Listening continuously. Just tap the palm rests! (Press Ctrl+C to stop)")

try:
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, 
                        blocksize=int(0.5 * SAMPLE_RATE), 
                        callback=audio_callback):
        while True:
            time.sleep(0.1) 
except KeyboardInterrupt:
    print("\nDaemon stopped.")
    if pipe:
        pipe.close()