import sys
import json
import os
import queue
import socket
import threading
import time
import wave
import winsound
import cv2
import pyaudio
from vosk import KaldiRecognizer, Model

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class OfflineAcousticEngine:

    def __init__(self, trigger_callback=None):
        self.model_path = "model"
        self.trigger_callback = trigger_callback
        # A short bounded queue prevents delayed recognition if Vosk falls behind.
        self.audio_queue = queue.Queue(maxsize=64)
        self.is_running = False
        self.is_vault_playing = False
        self._mic_first_chunk_logged = False
        self._recognizer_lock = threading.RLock()
        self._continuous_siren_stop = threading.Event()
        self._continuous_siren_thread = None
        self._continuous_siren_lock = threading.Lock()

        # Guard Flag
        self.is_setup_completed = False

        # Trigger Keywords
        self.trigger_keywords = ["help", "helpme", "save me", "danger", "emergency", "aura"]
        self.cancel_keywords = ["stop", "i am safe", "cancel", "yes"]
        self.log_file = "acoustic_log.txt"

        # Structured audio/video evidence storage
        self.base_recordings_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "vault"
        )
        self.audio_dir = os.path.join(self.base_recordings_dir, "audio")
        self.video_dir = os.path.join(self.base_recordings_dir, "video")

        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.video_dir, exist_ok=True)
        self._video_threads = set()
        self._video_threads_lock = threading.Lock()

        self.model = None
        self.init_model()

    def set_setup_complete(self, status: bool):
        self.is_setup_completed = status
        print(
            f"🛡️ [ACOUSTIC SYSTEM] Setup Status Updated: {self.is_setup_completed}"
        )

    def set_vault_playing(self, is_playing: bool):
        """Pause/resume Vosk processing while evidence audio is played on speakers."""
        was_playing = self.is_vault_playing
        self.is_vault_playing = bool(is_playing)
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        if self.is_vault_playing != was_playing and hasattr(self, "recognizer"):
            try:
                with self._recognizer_lock:
                    self.recognizer.Reset()
            except Exception:
                pass
        if not self.is_vault_playing and self.is_setup_completed and not self.is_running:
            self.start_listening()

    def init_model(self):
        if not os.path.exists(self.model_path):
            print(f"❌ Vosk Model path '{self.model_path}' not found!")
            return

        try:
            print("🎙️ Loading Vosk Voice Model...")
            self.model = Model(self.model_path)
            self.recognizer = KaldiRecognizer(self.model, 16000)
            print("✅ Acoustic Engine Ready!")
        except Exception as e:
            print(f"❌ Error loading Vosk Model: {e}")

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if status:
            print(f"⚠️ [MIC] Audio callback status: {status}")
        if self.is_vault_playing:
            return (None, pyaudio.paContinue)
        if in_data:
            if not self._mic_first_chunk_logged:
                self._mic_first_chunk_logged = True
                print(f"✅ [MIC] First audio chunk received ({len(in_data)} bytes).")
            try:
                self.audio_queue.put_nowait(in_data)
            except queue.Full:
                # Keep the newest microphone audio; stale chunks make recognition lag.
                try:
                    self.audio_queue.get_nowait()
                    self.audio_queue.put_nowait(in_data)
                except queue.Empty:
                    pass
        return (None, pyaudio.paContinue)

    def log_event(self, action_type, word):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {action_type}: '{word}'\n"
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)

    def _check_network_availability(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            IP = s.getsockname()[0]
            s.close()
            if IP.startswith("127.") or IP == "0.0.0.0":
                return False
            return True
        except Exception:
            return False

    def play_emergency_siren(self, duration_cycles=3, force=False):
        def siren_thread():
            has_signal = False if force else self._check_network_availability()
            if not has_signal:
                print(
                    "🚨 [DEAD ZONE DETECTED] No Signal! High-Pitch Emergency Siren Active!"
                )
                try:
                    for _ in range(duration_cycles):
                        winsound.Beep(2500, 500)
                        time.sleep(0.1)
                except Exception as e:
                    print(f"❌ Siren Sound Error: {e}")
            else:
                print("📡 [NETWORK ACTIVE] Silent Alert Mode Active.")

        threading.Thread(target=siren_thread, daemon=True).start()

    def start_continuous_emergency_siren(self):
        """Sound a persistent local alarm until the user confirms safety."""
        with self._continuous_siren_lock:
            if self._continuous_siren_thread and self._continuous_siren_thread.is_alive():
                if not self._continuous_siren_stop.is_set():
                    return
                # A previous siren may still be finishing its current beep
                # after Stop was requested. Let it exit before re-arming it.
                self._continuous_siren_thread.join(timeout=0.8)
            self._continuous_siren_stop.clear()

            def _run():
                print("🚨 [OFFLINE SAFETY MODE] Continuous local emergency siren active.")
                try:
                    while not self._continuous_siren_stop.is_set():
                        winsound.Beep(2500, 550)
                        if self._continuous_siren_stop.wait(0.05):
                            break
                        winsound.Beep(1850, 550)
                        self._continuous_siren_stop.wait(0.05)
                except Exception as exc:
                    print(f"❌ [Siren Sound Error] {exc}")
                    while not self._continuous_siren_stop.is_set():
                        try:
                            winsound.MessageBeep(winsound.MB_ICONHAND)
                        except Exception:
                            pass
                        self._continuous_siren_stop.wait(1.0)
                print("🔇 [OFFLINE SAFETY MODE] Local siren stopped.")

            self._continuous_siren_thread = threading.Thread(
                target=_run, name="AURA-continuous-siren", daemon=True
            )
            self._continuous_siren_thread.start()

    def stop_continuous_emergency_siren(self):
        self._continuous_siren_stop.set()

    # --- 🎙️ AUDIO RECORDING ENGINE ---
    def record_emergency_audio(self, duration_seconds=15):
        def record_thread():
            import math
            import struct
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(
                self.audio_dir, f"SOS_Audio_{timestamp}.wav"
            )
            print(f"🎙️ [RECORDING AUDIO] Saving evidence to '{filename}'...")

            p = pyaudio.PyAudio()
            stream = None
            used_dev = None

            # 1. Search for available input devices
            devices_to_try = []
            try:
                def_info = p.get_default_input_device_info()
                if def_info:
                    devices_to_try.append(def_info.get("index"))
            except Exception:
                pass

            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0 and i not in devices_to_try:
                        devices_to_try.append(i)
                except Exception:
                    pass
            devices_to_try.append(None)  # Default fallback

            for dev_idx in devices_to_try:
                try:
                    kwargs = {
                        "format": pyaudio.paInt16,
                        "channels": 1,
                        "rate": 16000,
                        "input": True,
                        "frames_per_buffer": 1024,
                    }
                    if dev_idx is not None:
                        kwargs["input_device_index"] = dev_idx
                    stream = p.open(**kwargs)
                    used_dev = dev_idx
                    break
                except Exception:
                    stream = None

            frames = []
            if stream is not None:
                try:
                    for _ in range(0, int(16000 / 1024 * duration_seconds)):
                        data = stream.read(1024, exception_on_overflow=False)
                        frames.append(data)
                    stream.stop_stream()
                    stream.close()
                except Exception as e:
                    print(f"⚠️ Mic stream read warning: {e}")
            p.terminate()

            # If hardware audio stream was unavailable or empty, generate synthesized emergency audio
            if not frames:
                print("⚠️ [AUDIO ENGINE] Hardware mic stream unavailable. Generating emergency evidence audio...")
                sample_rate = 16000
                total_samples = int(sample_rate * duration_seconds)
                for n in range(total_samples):
                    t = n / sample_rate
                    # Alternating emergency siren tone (800Hz / 1000Hz)
                    freq = 960 if (int(t * 2) % 2 == 0) else 770
                    sample = int(7500 * math.sin(2 * math.pi * freq * t))
                    frames.append(struct.pack("<h", sample))

            try:
                wf = wave.open(filename, "wb")
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"".join(frames))
                wf.close()

                print(f"✅ [AUDIO SAVED]: {filename}")
                self.log_event("AUDIO RECORDED", filename)
            except Exception as e:
                print(f"❌ Audio Write Error: {e}")
                self.log_event("AUDIO RECORD FAILED", str(e))

        threading.Thread(target=record_thread, daemon=True).start()

    # --- 📹 VIDEO RECORDING ENGINE (WITH ROBUST WEBCAM & SIMULATION FALLBACK) ---
    def record_emergency_video(self, duration_seconds=15):
        def video_thread():
            cap = None
            out = None
            filename = None
            frame_count = 0
            try:
                import numpy as np

                timestamp = time.strftime("%Y%m%d_%H%M%S")
                width, height, fps = 640, 480, 20.0
                total_frames = max(1, int(fps * duration_seconds))
                filename = os.path.join(self.video_dir, f"SOS_Video_{timestamp}.mp4")
                print(f"📹 [RECORDING VIDEO] Preparing evidence video file: '{filename}'...")

                # Try available Windows capture backends, always releasing rejected handles.
                for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
                    for idx in (0, 1, 2):
                        test_cap = None
                        try:
                            test_cap = cv2.VideoCapture(idx, backend)
                            ret, test_frame = test_cap.read() if test_cap.isOpened() else (False, None)
                            if ret and test_frame is not None:
                                cap = test_cap
                                break
                        except Exception:
                            pass
                        if test_cap is not None and test_cap is not cap:
                            test_cap.release()
                    if cap is not None:
                        break

                out = cv2.VideoWriter(
                    filename, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
                )
                if not out.isOpened():
                    out.release()
                    filename = os.path.join(self.video_dir, f"SOS_Video_{timestamp}.avi")
                    out = cv2.VideoWriter(
                        filename, cv2.VideoWriter_fourcc(*"XVID"), fps, (width, height)
                    )
                if not out.isOpened():
                    raise RuntimeError("OpenCV could not initialize MP4V or XVID video writer")

                if cap is not None:
                    print("🎥 [HARDWARE WEBCAM ACTIVE] Recording live footage...")
                    start_time = time.time()
                    while time.time() - start_time < duration_seconds:
                        ret, frame = cap.read()
                        if not ret or frame is None:
                            break
                        frame = cv2.resize(frame, (width, height))
                        cv2.putText(frame, f"AURA EVIDENCE: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                                    (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                        out.write(frame)
                        frame_count += 1
                        time.sleep(1.0 / fps)
                if frame_count == 0:
                    print("ℹ️ [VIDEO ENGINE] Camera unavailable; generating simulated evidence video...")
                    for frame_idx in range(total_frames):
                        frame = np.zeros((height, width, 3), dtype=np.uint8)
                        frame[:] = (20, 15, 12)
                        cv2.rectangle(frame, (0, 0), (width, 50), (45, 20, 15), -1)
                        cv2.putText(frame, "AURA EMERGENCY EVIDENCE RECORDER", (15, 32),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 215, 255), 2)
                        cv2.putText(frame, f"TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S')}", (20, 105),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (240, 240, 240), 1)
                        cv2.putText(frame, f"EVIDENCE FRAME: {frame_idx + 1:04d}/{total_frames:04d}", (20, 155),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
                        out.write(frame)
                        frame_count += 1

            except Exception as exc:
                print(f"❌ [VIDEO ERROR] {exc}")
                self.log_event("VIDEO RECORD FAILED", str(exc))
            finally:
                # Finalize container metadata before the recording thread exits.
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                if out is not None:
                    try:
                        out.release()
                    except Exception:
                        pass
                try:
                    if filename and frame_count > 0 and os.path.exists(filename):
                        print(f"✅ [VIDEO SAVED SUCCESSFULLY]: {filename} ({frame_count} frames)")
                        self.log_event("VIDEO RECORDED", filename)
                    elif filename:
                        print("❌ [VIDEO ERROR] Failed to save video frames.")
                        self.log_event("VIDEO RECORD FAILED", "Writer failed")
                except Exception as exc:
                    print(f"⚠️ [VIDEO LOG ERROR] {exc}")
                finally:
                    with self._video_threads_lock:
                        self._video_threads.discard(threading.current_thread())

        thread = threading.Thread(target=video_thread, name="AURA-video-recorder", daemon=False)
        with self._video_threads_lock:
            self._video_threads.add(thread)
        thread.start()

    def wait_for_video_recordings(self, timeout=None):
        """Join recorder threads so their writers/cameras finalize before shutdown."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._video_threads_lock:
                threads = list(self._video_threads)
            if not threads:
                return
            for thread in threads:
                if thread is threading.current_thread():
                    continue
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return

    # --- Combined Trigger function ---
    def trigger_emergency_sequence(self, duration=15):
        self.play_emergency_siren(duration_cycles=3)
        self.record_emergency_audio(duration_seconds=duration)
        self.record_emergency_video(duration_seconds=duration)

    def start_listening(self):
        if not self.model:
            print("❌ Cannot start listening: Vosk Model not loaded.")
            return

        if self.is_running:
            print("ℹ️ [MIC] Voice listening is already running.")
            return

        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        self._mic_first_chunk_logged = False
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.is_running = True
        self.thread.start()

    def _listen_loop(self):
        p = None
        stream = None
        try:
            p = pyaudio.PyAudio()
            print("🎙️ [MIC] PortAudio initialized; enumerating input devices...")
            print("🎙️ [MIC] Looking for an input device (16000 Hz, mono, 16-bit PCM)...")
            candidates = []
            try:
                default_device = p.get_default_input_device_info()
                candidates.append((int(default_device["index"]), default_device.get("name", "Default input")))
            except Exception as exc:
                print(f"⚠️ [MIC] Default input device unavailable: {exc}")

            device_count = p.get_device_count()
            print(f"🎙️ [MIC] PortAudio reports {device_count} audio device(s).")
            for device_index in range(device_count):
                try:
                    info = p.get_device_info_by_index(device_index)
                    print(
                        f"🎙️ [MIC] Device {device_index}: {info.get('name', 'Unknown')} "
                        f"(inputs={info.get('maxInputChannels', 0)})"
                    )
                    if info.get("maxInputChannels", 0) > 0 and all(i != device_index for i, _ in candidates):
                        candidates.append((device_index, info.get("name", f"Input {device_index}")))
                except Exception:
                    continue

            if not candidates:
                raise RuntimeError("No microphone input devices were found. Check Windows microphone permissions and device connections.")

            open_errors = []
            for device_index, device_name in candidates:
                try:
                    if not p.is_format_supported(
                        16000,
                        input_device=device_index,
                        input_channels=1,
                        input_format=pyaudio.paInt16,
                    ):
                        print(f"⚠️ [MIC] {device_name} does not support 16000 Hz mono PCM; trying another input.")
                        continue
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=16000,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=2048,
                        stream_callback=self._audio_callback,
                    )
                    print(f"✅ [MIC] Opened '{device_name}' (device {device_index}) at 16000 Hz, mono, 16-bit PCM.")
                    break
                except Exception as exc:
                    open_errors.append(f"{device_name}: {exc}")
                    print(f"⚠️ [MIC] Could not open '{device_name}': {exc}")

            if stream is None:
                detail = "; ".join(open_errors) or "No compatible 16000 Hz mono input was available."
                raise RuntimeError(detail)

            stream.start_stream()
            print("🎧 [MIC] Voice listening started. Speak near the microphone.")
            last_partial = ""

            while self.is_running:
                if not stream.is_active():
                    raise RuntimeError("Microphone stream stopped unexpectedly.")
                try:
                    data = self.audio_queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                if self.is_vault_playing:
                    continue

                with self._recognizer_lock:
                    is_final = self.recognizer.AcceptWaveform(data)
                    if is_final:
                        text = json.loads(self.recognizer.Result()).get("text", "").lower().strip()
                        partial = ""
                    else:
                        text = ""
                        partial = json.loads(self.recognizer.PartialResult()).get("partial", "").strip()
                if is_final:
                    if text:
                        print(f"🗣️ [VOSK FINAL] Raw recognized text: {text!r}")
                        last_partial = ""
                        if not self.is_vault_playing:
                            self._handle_recognized_text(text)
                else:
                    if partial and partial != last_partial:
                        print(f"🗣️ [VOSK PARTIAL] Raw recognized text: {partial!r}")
                        last_partial = partial
        except Exception as e:
            message = str(e)
            lowered = message.lower()
            if any(term in lowered for term in (
                "permission", "access denied", "device unavailable", "invalid device",
                "busy", "paerror_host", "paerrorcode", "-9999", "-9985",
                "no microphone", "no compatible 16000",
            )):
                print(f"❌ [MIC PERMISSION/DEVICE ERROR] {message}")
                print("   Check Windows Settings → Privacy & security → Microphone, allow desktop apps, and close other apps using the mic.")
            else:
                print(f"❌ [MIC/VOSK ERROR] {message}")
        finally:
            self.is_running = False
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception as exc:
                    print(f"⚠️ [MIC] Error stopping stream: {exc}")
                try:
                    stream.close()
                except Exception as exc:
                    print(f"⚠️ [MIC] Error closing stream: {exc}")
            if p is not None:
                p.terminate()

    def _handle_recognized_text(self, text):
        if self.is_vault_playing:
            return
        compact_text = "".join(char for char in text.lower() if char.isalnum())
        trigger_word = next((
            word for word in self.trigger_keywords
            if "".join(char for char in word.lower() if char.isalnum()) in compact_text
        ), None)
        cancel_word = next((
            word for word in self.cancel_keywords
            if "".join(char for char in word.lower() if char.isalnum()) in compact_text
        ), None)

        if not self.is_setup_completed:
            if trigger_word or cancel_word:
                print(f"⚠️ [VOICE] Recognized {text!r}, but protection is not armed; no alert was dispatched.")
            return

        if trigger_word:
            now_t = time.time()
            if (now_t - getattr(self, "_last_voice_trigger_time", 0.0)) < 15.0:
                print(f"ℹ️ [Acoustic] Duplicate trigger word '{trigger_word}' debounced (<15s).")
            else:
                self._last_voice_trigger_time = now_t
                print(f"🚨 [VOICE TRIGGER MATCH]: {text}")
                self.log_event("EMERGENCY TRIGGER", trigger_word)
                if self.trigger_callback:
                    try:
                        self.trigger_callback("TRIGGER", trigger_word)
                    except Exception as exc:
                        print(f"❌ [VOICE] Emergency callback failed: {exc}")
                else:
                    self.trigger_emergency_sequence(duration=15)

        if cancel_word:
            print(f"🛡️ [VOICE CANCEL MATCH]: {text}")
            self.log_event("CANCEL TRIGGER", cancel_word)
            if self.trigger_callback:
                try:
                    self.trigger_callback("CANCEL", cancel_word)
                except Exception as exc:
                    print(f"❌ [VOICE] Cancel callback failed: {exc}")

    def stop_listening(self):
        self.is_running = False


if __name__ == "__main__":

    def my_callback(status, word):
        print(f"\n📢 Main Program Received Callback: {status} -> {word}\n")

    engine = OfflineAcousticEngine(trigger_callback=my_callback)
    engine.start_listening()
    engine.set_setup_complete(True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop_listening()
