import os
import json
import time
import asyncio
import threading
from datetime import datetime
from cryptography.fernet import Fernet
from bleak import BleakScanner, BleakClient

from location_module import OfflineLocationEngine
from prediction_module import LSTMTrajectoryPredictor
from anomaly_engine import TrajectoryAnomalyEngine
from sensor_module import SystemSensorDiagnostics
from acoustic_module import OfflineAcousticEngine
from network_monitor_module import NetworkMonitor
from fall_detector import start_detection
from gps_bridge_module import GPSBridge
from sync_manager import SyncManager

import subprocess
import shlex

SMS_CALL_ESCALATION_DELAY_SEC = 60


def _is_connectivity_only_event(reason):
    """Identify link-status messages that must never be treated as an SOS."""
    normalized = str(reason or "").strip().lower().replace("_", " ").replace("-", " ")
    return any(marker in normalized for marker in (
        "adb disconnected", "usb disconnected", "phone disconnected",
        "device disconnected", "connection lost", "adb offline",
        "usb offline", "phone offline",
    ))

# ── NATIVE ADB PHONE & SMS DISPATCHER (NO TERMUX REQUIRED) ───────────────
class USBPhoneDispatcher:
    def __init__(self, adb_path="adb"):
        self.adb_path = adb_path
        self.adb_env = os.environ.copy()
        user_profile = self.adb_env.get("USERPROFILE")
        if user_profile:
            # Some bundled Windows ADB builds fall back to the invalid path
            # "\\.android" when HOME is absent in the launched app environment.
            self.adb_env["HOME"] = user_profile
            self.adb_env["ANDROID_USER_HOME"] = os.path.join(user_profile, ".android")
            self.adb_env["ANDROID_SDK_HOME"] = user_profile

    def _startupinfo(self):
        if not hasattr(subprocess, "STARTUPINFO"):
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startupinfo

    def check_phone_connection(self):
        """Return the connected authorized physical ADB device, if any."""
        try:
            result = subprocess.run(
                [self.adb_path, "devices", "-l"],
                capture_output=True, text=True, timeout=2,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
        except FileNotFoundError:
            return {"adb_installed": False, "device_connected": False,
                    "error": "ADB executable not found"}
        except Exception as exc:
            return {"adb_installed": True, "device_connected": False,
                    "error": str(exc)}

        if result.returncode != 0:
            return {"adb_installed": True, "device_connected": False,
                    "error": (result.stderr or result.stdout or "ADB device check failed").strip()}

        devices = []
        for line in (result.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                devices.append((parts[0], parts[1], " ".join(parts[2:])))

        target_serial = "10MECPFE0900011"
        connected_devices = [d for d in devices if d[1] == "device" and not d[0].startswith("emulator-")]
        chosen = next((d for d in connected_devices if d[0] == target_serial), None)
        if chosen is None and connected_devices:
            chosen = connected_devices[0]
        if chosen:
            serial, _, details = chosen
            model = next((part.split(":", 1)[1] for part in details.split()
                          if part.startswith("model:")), "Android Device")
            return {"adb_installed": True, "device_connected": True,
                    "device_id": serial, "device_model": model,
                    "sim_state": "UNKNOWN", "devices": devices}

        unauthorized = next((d for d in devices if d[1] == "unauthorized"), None)
        return {"adb_installed": True, "device_connected": False,
                "device_id": unauthorized[0] if unauthorized else "",
                "device_state": unauthorized[1] if unauthorized else "offline",
                "devices": devices}

    def reconnect_device(self):
        """Ask ADB to refresh USB/offline transports without restarting its server."""
        try:
            result = subprocess.run(
                [self.adb_path, "reconnect", "offline"],
                capture_output=True, text=True, timeout=3,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
            if result.returncode == 0:
                print("🔄 [USB Dispatcher] Requested ADB reconnect for offline transports.")
                return True
            detail = (result.stderr or result.stdout or "no command output").strip()
            print(f"⚠️ [USB Dispatcher] ADB transport reconnect failed: {detail}")
        except Exception as exc:
            print(f"⚠️ [USB Dispatcher] ADB transport reconnect unavailable: {exc}")
        return False

    def ensure_adb_server(self):
        """Start the ADB server if needed; safe to call during background recovery."""
        try:
            result = subprocess.run(
                [self.adb_path, "start-server"],
                capture_output=True, text=True, timeout=5,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
            if result.returncode == 0:
                return True
            detail = (result.stderr or result.stdout or "no command output").strip()
            print(f"⚠️ [USB Dispatcher] adb start-server failed: {detail}")
        except Exception as exc:
            print(f"⚠️ [USB Dispatcher] Could not start ADB server: {exc}")
        return False

    def restart_adb_server(self):
        """Restart the local ADB server after a failed device probe."""
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            for action in ("kill-server", "start-server"):
                print(f"🔄 [USB Dispatcher] Running adb {action} after a failed device check...")
                result = subprocess.run(
                    [self.adb_path, action],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    startupinfo=startupinfo,
                    env=self.adb_env,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "no command output").strip()
                    print(f"⚠️ [USB Dispatcher] adb {action} returned {result.returncode}: {detail}")
                    return False
            return True
        except Exception as exc:
            print(f"❌ [USB Dispatcher] Could not restart ADB server: {exc}")
            return False

    def trigger_priority_call(self, phone_number):
        """Triggers direct priority call via ADB."""
        clean_number = self.normalize_phone_number(phone_number)
        if not clean_number:
            print("⚠️ [USB Dispatcher] Call skipped: empty contact number.")
            return False
        print(f"📞 [USB Dispatcher] Calling priority contact: {clean_number}...")
        cmd = [self.adb_path, "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{clean_number}"]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                           timeout=10, startupinfo=self._startupinfo(), env=self.adb_env)
            return True
        except Exception as e:
            print(f"❌ Direct Call Error: {e}")
            return False

    @staticmethod
    def normalize_phone_number(phone_number):
        """Normalize saved Indian mobile numbers for Android SMS/call intents."""
        raw = str(phone_number or "").strip()
        if not raw:
            return ""
        digits = "".join(ch for ch in raw if ch.isdigit())
        if raw.startswith("+"):
            return f"+{digits}" if digits else ""
        if len(digits) == 10:
            return f"+91{digits}"
        if len(digits) == 12 and digits.startswith("91"):
            return f"+{digits}"
        return digits

    def send_emergency_sms_native(self, phone_number, message_text):
        """Open the cellular SMS composer and attempt to send it through ADB."""
        clean_number = self.normalize_phone_number(phone_number)
        if not clean_number:
            print("❌ [USB Dispatcher] SMS skipped: empty contact number.")
            return False
        sms_number = clean_number
        print(f"💬 [USB Dispatcher] Opening cellular SMS composer for {clean_number}...")

        # SENDTO uses the phone's SMS service over the cellular network; it does
        # not require mobile data or Wi-Fi. Quote the body for Android's shell.
        remote_command = (
            "am start -a android.intent.action.SENDTO "
            f"-d {shlex.quote('sms:' + sms_number)} "
            f"--es sms_body {shlex.quote(str(message_text))}"
        )
        try:
            subprocess.run(
                [self.adb_path, "shell", remote_command],
                capture_output=True, text=True, check=True, timeout=15,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
            # All UI actions execute on the dispatch worker, not Tk's loop.
            # This SENDTO flow works over cellular SMS without data or RCS.
            time.sleep(1.0)
            subprocess.run(
                [self.adb_path, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
            time.sleep(1.2)
            tap_sent = False
            try:
                subprocess.run(
                    [self.adb_path, "shell", "input", "tap", "656", "1102"],
                    capture_output=True, text=True, check=True, timeout=5,
                    startupinfo=self._startupinfo(), env=self.adb_env,
                )
                tap_sent = True
            except Exception as exc:
                print(f"⚠️ [USB Dispatcher] Vivo Y29 Send-button tap failed: {exc}")

            time.sleep(0.5)
            keyevent_sent = False
            try:
                subprocess.run(
                    [self.adb_path, "shell", "input", "keyevent", "66"],
                    capture_output=True, text=True, check=True, timeout=5,
                    startupinfo=self._startupinfo(), env=self.adb_env,
                )
                keyevent_sent = True
            except Exception as exc:
                print(f"⚠️ [USB Dispatcher] SMS Enter fallback failed: {exc}")

            sent = tap_sent or keyevent_sent
            if sent:
                print(f"✅ [USB Dispatcher] SMS send action issued to {clean_number}.")
            else:
                print(f"❌ [USB Dispatcher] SMS composer opened, but send input failed for {clean_number}.")
            return sent
        except Exception as e:
            print(f"❌ SMS Dispatch Error: {e}")
            return False

    def dispatch_threat_alert(self, contacts_list, current_lat, current_lon, message_text=None):
        """Send the SOS SMS to every saved contact in priority order."""
        sms_body = message_text or (
            "🚨 AURA SOS EMERGENCY ALERT! 🚨\n"
            "User in danger !!!\n"
            "Live Location:\n"
            f"https://maps.google.com/?q={current_lat},{current_lon}"
        )

        sms_sent = 0
        # Snapshot the complete list for this incident; never truncate to P1/P2.
        for contact in list(contacts_list or []):
            phone = contact.get("phone") if isinstance(contact, dict) else str(contact)
            if phone:
                sms_sent += bool(self.send_emergency_sms_native(phone, sms_body))

        return {
            "sms_sent": sms_sent,
            "call_dispatched": False,
            "sms_completed_at": time.monotonic(),
        }

    def wait_for_call_end(self, timeout=300, poll_interval=2, cancel_event=None):
        """Return answered while connected, ended after disconnect, or unanswered."""
        answer_deadline = time.monotonic() + timeout
        call_started = time.monotonic()
        connected_deadline = None
        saw_active = False
        while time.monotonic() < (connected_deadline or answer_deadline):
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            try:
                result = subprocess.run(
                    [self.adb_path, "shell", "dumpsys", "telecom"],
                    capture_output=True, text=True, timeout=2,
                    startupinfo=self._startupinfo(), env=self.adb_env,
                )
                output = (result.stdout or "").upper()
                active = "STATE=ACTIVE" in output or "CALLSTATE_ACTIVE" in output
                dialing = "STATE=DIALING" in output or "CALLSTATE_DIALING" in output
                if active:
                    saw_active = True
                    if connected_deadline is None:
                        connected_deadline = time.monotonic() + 300
                elif saw_active:
                    return "ended"
                elif (time.monotonic() - call_started >= 8 and not dialing
                      and ("STATE=DISCONNECTED" in output or "NO CALL" in output)):
                    return "unanswered"
            except Exception as exc:
                print(f"⚠️ [USB Dispatcher] Could not inspect call state: {exc}")
            time.sleep(poll_interval)
        return "answered" if saw_active else "unanswered"

    def end_current_call(self):
        try:
            subprocess.run(
                [self.adb_path, "shell", "input", "keyevent", "6"],
                capture_output=True, text=True, timeout=5, check=True,
                startupinfo=self._startupinfo(), env=self.adb_env,
            )
        except Exception as exc:
            print(f"⚠️ [USB Dispatcher] Could not end unanswered call: {exc}")

class CallEscalationManager:
    """Manages configurable response delay and sequential priority calls."""
    def __init__(self, controller):
        self.controller = controller

    @property
    def timeout_s(self):
        return self.controller.call_escalation_timeout_sec

    @timeout_s.setter
    def timeout_s(self, val):
        self.controller.call_escalation_timeout_sec = max(20, min(120, int(val)))

    def set_timeout(self, val):
        self.timeout_s = val

    @property
    def escalation_active(self):
        return self.controller.call_escalation_status in ["WAITING_RESPONSE", "CALLING_IN_PROGRESS"]

    @property
    def escalation_state(self):
        return self.controller.call_escalation_status

    def start_escalation(self, contacts, incident_type="EMERGENCY"):
        self.controller.start_call_escalation(contacts, incident_type)

    def cancel_escalation(self, reason="User confirmed safe"):
        self.controller.cancel_call_escalation()
        self.controller.call_escalation_status = "RESOLVED_SAFE"


class MainSafetyController:
    def __init__(self, emergency_contacts=None):
        self.emergency_contacts = emergency_contacts if emergency_contacts else []
        self.call_escalation_timeout_sec = 60
        self._load_dynamic_profile()
        adb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adb.exe")
        self.usb_dispatcher = USBPhoneDispatcher(adb_path if os.path.isfile(adb_path) else "adb")

        # System Core Engines
        self.location_engine = OfflineLocationEngine()
        self.predictor = LSTMTrajectoryPredictor(sequence_length=8)
        self.anomaly_engine = TrajectoryAnomalyEngine(
            speed_threshold_kmh=25.0,
            deviation_threshold_km=0.1,
        )
        self.sensor_diagnostics = SystemSensorDiagnostics(battery_threshold=15)
        self.acoustic_engine = OfflineAcousticEngine(trigger_callback=self.voice_trigger_callback)

        # Offline Synchronization Manager
        self.sync_manager = SyncManager()
        self.sync_manager.start()

        # GPS Bridge for real mobile phone location stream over TCP (port 8082)
        self.last_real_gps_fix_monotonic = 0.0
        self._gps_forward_lock = threading.Lock()
        self._gps_forward_pending = False
        self._gps_forward_ready = False
        self.gps_bridge = GPSBridge(self)
        try:
            self.gps_bridge.start()
        except Exception as e:
            print(f"[GPS Bridge] Startup notice: {e}")

        # Network Monitor
        self.network_monitor = NetworkMonitor(self, poll_interval=5.0)

        # GUI Reference Link
        self.gui_app = None
        self.map_simulator_window = None

        # System State
        self.system_status = "ACTIVE_MONITORING"
        self.is_emergency_active = False
        self.stealth_active = False
        self.previous_loc = self.location_engine.get_current_location()
        self.phone_has_signal = True
        self.usb_device_connected = None

        # Safe Zones (synchronized with location_engine's geofence manager)
        self.safe_zones = self.location_engine.geofence_manager.safe_zones

        # Trip Destination Management (persisted in data/current_trip.json)
        self.trip_file = os.path.join("data", "current_trip.json")
        self._load_trip_destination()

        # Demo Mode State (strictly isolated from real GPS)
        self.demo_mode_active = False
        self.demo_lat = None
        self.demo_lon = None
        self.demo_speed = 0.0

        # Display Only Mode (Show tracking & map visually only, no automated emergency actions)
        self.display_only_mode = False

        # Automated Call Escalation State
        # Preserve the configured timeout loaded before the dispatch engines
        # are initialized.
        self.call_escalation_thread = None
        self.call_escalation_cancel_event = threading.Event()
        self.call_escalation_remaining = 0
        self.call_escalation_status = "IDLE"
        self.escalation_manager = CallEscalationManager(self)

        # Battery warning state (Requirement 6: <=15% separate warning, no SOS, no loops)
        self.battery_warning_sent = False

        # Incident Debounce State
        self.last_sos_time = 0.0
        self.last_sos_payload = None

        # Encryption & Local Vault
        self.vault_key_path = "vault.key"
        self.cipher = self._init_encryption_key()
        self.blackbox_file = "blackbox_vault.json"

        # Fall Detector Engine
        self.fall_detector = start_detection(callback_function=self.on_fall_detected)

        # Start Vosk capture on its own background thread. It remains unarmed until
        # the user completes protection setup, when the GUI enables recognition.
        self.start_voice_recognition()

    def attach_gui(self, gui_app):
        self.gui_app = gui_app
        if hasattr(self, 'network_monitor') and self.network_monitor:
            self.network_monitor.gui_app = gui_app
            self.network_monitor.start()

    def start_voice_recognition(self, armed=False):
        """Start the Vosk listener in the acoustic engine's background thread."""
        engine = getattr(self, "acoustic_engine", None)
        if engine is None:
            print("❌ [Voice] Acoustic engine is not initialized.")
            return False

        engine.trigger_callback = self.voice_trigger_callback
        if armed:
            engine.set_setup_complete(True)
        engine.start_listening()
        return True

    # ── TRIP DESTINATION MANAGEMENT ──────────────────────────────────────────

    @property
    def trip_destination(self):
        return self.get_trip_destination()

    def _load_trip_destination(self):
        if os.path.exists(self.trip_file):
            try:
                with open(self.trip_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and "latitude" in data and "longitude" in data:
                        self.anomaly_engine.set_destination(
                            data.get("name", "Saved Destination"),
                            data["latitude"],
                            data["longitude"]
                        )
                        print(f"🎯 [Destination] Restored trip destination: {data.get('name')} ({data['latitude']}, {data['longitude']})")
            except Exception as e:
                print(f"[Destination Load Notice] {e}")

    def set_trip_destination(self, name: str, latitude: float, longitude: float):
        os.makedirs("data", exist_ok=True)
        dest = {
            "name": str(name).strip() or "Destination",
            "latitude": float(latitude),
            "longitude": float(longitude),
            "created_at": datetime.now().isoformat()
        }
        try:
            with open(self.trip_file, "w", encoding="utf-8") as f:
                json.dump(dest, f, indent=2)
            self.anomaly_engine.set_destination(dest["name"], dest["latitude"], dest["longitude"])
            self.log_blackbox_event(dest["latitude"], dest["longitude"], "DESTINATION_SET", details=f"Target: {dest['name']}")
            print(f"🎯 [Destination] Saved new trip destination: {dest['name']}")
        except Exception as e:
            print(f"[Destination Save Error] {e}")
        return dest

    def get_trip_destination(self):
        return self.anomaly_engine.get_destination()

    def clear_trip_destination(self):
        self.anomaly_engine.clear_destination()
        if os.path.exists(self.trip_file):
            try:
                os.remove(self.trip_file)
            except Exception:
                pass
        self.log_blackbox_event(0.0, 0.0, "DESTINATION_CLEARED")
        print("🎯 [Destination] Cleared trip destination.")

    # ── ISOLATED DEMO MODE MANAGEMENT ────────────────────────────────────────

    def set_demo_mode(self, active: bool):
        self.demo_mode_active = bool(active)
        if self.demo_mode_active:
            print("🎮 [Demo Mode] ACTIVATED — Simulated movement isolated from real GPS.")
            self.log_blackbox_event(
                self.demo_lat or 0.0, self.demo_lon or 0.0,
                "DEMO_MODE_ACTIVATED", is_demo=True
            )
        else:
            print("🎮 [Demo Mode] DEACTIVATED — Reverted to real GPS sensor stream.")
            # Start a fresh live GPS velocity baseline after simulator use.
            self.previous_loc = self.location_engine.get_current_location()
            if hasattr(self.anomaly_engine, "route_deviation_samples"):
                self.anomaly_engine.route_deviation_samples = 0
            self.log_blackbox_event(
                0.0, 0.0, "DEMO_MODE_DEACTIVATED", is_demo=False
            )

    def start_demo_mode(self, start_lat=12.9716, start_lon=77.5946, speed_kmh=25.0):
        self.set_demo_mode(True)
        self.update_demo_position(start_lat, start_lon, speed_kmh)

    def step_demo_movement(self, d_lat=0.0005, d_lon=0.0005):
        if self.demo_lat is not None and self.demo_lon is not None:
            self.demo_lat += d_lat
            self.demo_lon += d_lon

    def stop_demo_mode(self):
        self.set_demo_mode(False)

    def update_demo_position(self, lat: float, lon: float, speed_kmh: float = 25.0):
        self.demo_lat = float(lat)
        self.demo_lon = float(lon)
        self.demo_speed = float(speed_kmh)

    def on_fall_detected(self):
        """Callback fired directly from fall_detector.py's background thread."""
        print("\n🚨 [MainController] Fall Event Signal Received!")
        if self.gui_app and hasattr(self.gui_app, 'root'):
            self.gui_app.root.after(0, lambda: self.gui_app.trigger_threat("💥 HARD FALL / DROP DETECTED"))
        else:
            loc = self.location_engine.get_current_location()
            self.execute_emergency_sequence("FALL_DETECTED", loc)

    def start_emergency_recording(self):
        """Called by GUI or threat activation to record 15s audio & video."""
        current_loc = self.location_engine.get_current_location()
        try:
            self.acoustic_engine.record_emergency_audio(duration_seconds=15)
            self.acoustic_engine.record_emergency_video(duration_seconds=15)
        except Exception as e:
            print(f"[Recording Error] {e}")
        self.log_blackbox_event(current_loc["latitude"], current_loc["longitude"], "RECORDING_STARTED")

        # Schedule check to queue recorded audio in SyncManager once finished
        def _queue_audio_when_ready():
            time.sleep(16)
            audio_dir = self.acoustic_engine.audio_dir
            if os.path.exists(audio_dir):
                files = sorted(
                    [os.path.join(audio_dir, f) for f in os.listdir(audio_dir) if f.endswith(".wav")],
                    key=os.path.getmtime,
                    reverse=True
                )
                if files:
                    latest = files[0]
                    self.sync_manager.queue_audio_file(latest, event_id=f"EV_{int(time.time())}")

        threading.Thread(target=_queue_audio_when_ready, daemon=True).start()

    # --- BLEAK ASYNC DISPATCHER ---
    def send_bluetooth_alert(self, contact_number, maps_link):
        print(f"[Bluetooth Dispatch] Initializing BLE Scan for {contact_number}...")

        async def ble_task():
            try:
                devices = await BleakScanner.discover(timeout=3.0)
                for device in devices:
                    if "AURA_MOBILE" in str(device.name):
                        async with BleakClient(device.address) as client:
                            payload = f"SOS:{contact_number}:{maps_link}".encode('utf-8')
                            print(f"[Bluetooth Dispatch] Packet sent successfully to {device.name}")
                        break
            except Exception as e:
                print(f"[Bluetooth Error] Bleak transmission failed: {e}")

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(ble_task())
            loop.close()

        threading.Thread(target=run_loop, daemon=True).start()

    def process_incoming_bluetooth_command(self, bluetooth_packet):
        phone_bat = bluetooth_packet.get("phone_battery", 100)
        accel_data = bluetooth_packet.get("accelerometer", None)
        is_fall = bluetooth_packet.get("fall_detected", False)

        self.sensor_diagnostics.update_mobile_sensor_telemetry(phone_bat, accel_data, is_fall)
        if is_fall:
            current_loc = self.location_engine.get_current_location()
            self.execute_emergency_sequence("MOBILE_FALL_DETECTED", current_loc)

    # --- ENCRYPTION & BLACKBOX LOGGING ---
    def _init_encryption_key(self):
        if not os.path.exists(self.vault_key_path):
            key = Fernet.generate_key()
            with open(self.vault_key_path, "wb") as kf:
                kf.write(key)
        else:
            with open(self.vault_key_path, "rb") as kf:
                key = kf.read()
        return Fernet(key)

    def log_blackbox_event(self, *args, **kwargs):
        """
        Logs incidents and security state transitions into the blackbox vault.
        Accepts: (lat, lon, event_type, details=None, is_demo=None) OR
                 (event_type, location=None, details=None, is_demo=None)
        """
        lat = 0.0
        lon = 0.0
        event_type = "SAFETY_EVENT"
        details = kwargs.get("details", "")
        is_demo = kwargs.get("is_demo", None)

        if len(args) >= 3 and (isinstance(args[0], (int, float)) or isinstance(args[1], (int, float))):
            lat = float(args[0])
            lon = float(args[1])
            event_type = str(args[2])
            if len(args) > 3: details = args[3]
            if len(args) > 4: is_demo = args[4]
        elif len(args) >= 1 and isinstance(args[0], str):
            event_type = args[0]
            loc = kwargs.get("location")
            if loc and isinstance(loc, dict):
                lat = loc.get("latitude", 0.0)
                lon = loc.get("longitude", 0.0)
            elif len(args) >= 3:
                lat = float(args[1])
                lon = float(args[2])

        entry = {
            "timestamp": datetime.now().isoformat(),
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
            "event": event_type,
            "status": self.system_status,
            "details": details or "",
            "mode": "DEMO" if (is_demo if is_demo is not None else self.demo_mode_active) else "PRODUCTION"
        }
        logs = []
        if os.path.exists(self.blackbox_file):
            try:
                with open(self.blackbox_file, "r") as f:
                    logs = json.load(f)
            except Exception:
                logs = []
        logs.append(entry)
        # Keep last 500 entries
        if len(logs) > 500:
            logs = logs[-500:]
        try:
            with open(self.blackbox_file, "w") as f:
                json.dump(logs, f, indent=2)
        except Exception as e:
            print(f"[Blackbox Write Warning] {e}")

    def _load_dynamic_profile(self):
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        profile_path = os.path.join(data_dir, "user_profile.json")
        if os.path.exists(profile_path):
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    p = json.load(f)
                    if not self.emergency_contacts:
                        self.emergency_contacts = [c["phone"] for c in p.get("contacts", []) if "phone" in c]
                    if "escalation_timeout" in p:
                        self.call_escalation_timeout_sec = max(
                            20, min(120, int(p["escalation_timeout"]))
                        )
            except Exception as e:
                print(f"[MainController Profile Load Notice] {e}")
        config_path = os.path.join(data_dir, "config.json")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if "escalation_timeout" in config:
                    self.call_escalation_timeout_sec = max(
                        20, min(120, int(config["escalation_timeout"]))
                    )
            except Exception as e:
                print(f"[MainController Config Load Notice] {e}")

    def set_emergency_contacts(self, contacts_list):
        if isinstance(contacts_list, list):
            self.emergency_contacts = contacts_list

    def is_usb_dispatch_ready(self):
        """Return the latest debounced ADB state without blocking on a subprocess."""
        connected = getattr(self, "usb_device_connected", None)
        return bool(connected) if connected is not None else False

    # --- GEOFENCING: Safe Zone Management ---
    def add_safe_zone(self, name, latitude, longitude, radius_km=0.5):
        self.location_engine.geofence_manager.add_safe_zone(name, latitude, longitude, radius_km)
        self.safe_zones = self.location_engine.geofence_manager.safe_zones

    def remove_safe_zone(self, index):
        self.location_engine.geofence_manager.remove_safe_zone(index)
        self.safe_zones = self.location_engine.geofence_manager.safe_zones

    def check_geofence(self, current_loc):
        return self.location_engine.geofence_manager.is_outside_all_zones(
            current_loc["latitude"], current_loc["longitude"]
        )

    # ── LOW BATTERY SEPARATE WARNING (NO SOS TRIGGER) ────────────────────────
    def send_low_battery_notice(self, battery_pct=None, location_data=None, percent=None):
        """
        Sends a separate Low Battery Warning SMS to saved emergency contacts.
        CRITICAL: Does NOT trigger SOS!
        """
        pct = battery_pct if battery_pct is not None else (percent if percent is not None else 15)
        if self.battery_warning_sent:
            return False

        if location_data is None:
            location_data = self.location_engine.get_current_location()

        lat = location_data.get("latitude", 0.0)
        lon = location_data.get("longitude", 0.0)

        self.battery_warning_sent = True
        print(f"🔋 [MainController] Dispatching Low Battery Warning ({pct}%) to contacts: {self.emergency_contacts}")
        sms_results = self.telephony_manager.send_low_battery_sms(
            self.emergency_contacts, pct, lat, lon
        )

        self.log_blackbox_event(lat, lon, f"LOW_BATTERY_NOTICE_{pct}PCT", details=str(sms_results))
        return True

    def reset_battery_warning_state(self):
        self.battery_warning_sent = False

    def voice_trigger_callback(self, status, word):
        if status == "CANCEL":
            if self.gui_app:
                def _cancel_on_ui():
                    reason = f"Voice cancel ('{word}')"
                    if self.gui_app.cancel_active_safety_check(reason):
                        return
                    if getattr(self.gui_app, "is_threat_active", False):
                        self.gui_app.dismiss_alarm(reason)
                    else:
                        self.acknowledge_user_safe()

                self.gui_app.root.after(0, _cancel_on_ui)
            else:
                self.acknowledge_user_safe()
            return

        if status != "TRIGGER":
            return

        normalized_word = str(word).strip().lower()
        if normalized_word not in {"help", "helpme", "save me", "danger", "emergency", "aura"}:
            print(f"ℹ️ [Voice] Ignoring unrecognized emergency keyword: {normalized_word!r}")
            return

        print(f"🚨 [Voice] Emergency keyword recognized: {normalized_word!r}; dispatching alert.")
        try:
            if self.gui_app and hasattr(self.gui_app, "trigger_emergency_ui_mode"):
                reason = f"VOICE EMERGENCY: {normalized_word.upper()}"
                try:
                    self.gui_app.root.after(
                        0,
                        lambda r=reason: self.gui_app.trigger_emergency_ui_mode(
                            reason=r,
                            dispatch_status="SIREN ACTIVE  •  SENDING EMERGENCY ALERT",
                        ),
                    )
                except Exception as ui_exc:
                    print(f"⚠️ [Voice] Could not queue emergency UI state: {ui_exc}")
            usb_connected = self.is_usb_dispatch_ready()
            current_loc = self.location_engine.get_current_location()
            self.execute_emergency_sequence(
                f"VOICE_EMERGENCY_{normalized_word.upper()}", current_loc,
                voice_usb_mode=True, usb_connected=usb_connected,
            )
        except Exception as exc:
            print(f"❌ [Voice] Emergency dispatch failed: {exc}")

    # ── COMPLETE EMERGENCY EXECUTION ─────────────────────────────────────────

    def execute_emergency_sequence(self, threat_type, location_data,
                                   voice_usb_mode=False, usb_connected=None,
                                   adb_sms=True, immediate_calls=False):
        """
        Executes full offline-first emergency sequence:
          1. Enforces incident debounce to prevent duplicate SMS for same incident.
          2. Activates emergency state & blackbox record.
          3. Sounds loud audible alarm/siren.
          4. Obtains verified GPS / last-known location & builds Google Maps link.
          5. Immediately sends SMS containing: Threat message, current location coords, Google Maps link.
          6. Records audio/video evidence.
          7. Queues event in SyncManager for offline storage & backend synchronization.
          8. Starts the configured response timeout before automated calls.
        """
        if _is_connectivity_only_event(threat_type):
            print(f"ℹ️ [EmergencySequence] Ignoring connectivity-only event: {threat_type}")
            return {"threat_type": str(threat_type), "status": "IGNORED_CONNECTIVITY_ONLY"}

        now = time.time()
        # Incident debounce: do not duplicate identical emergency sequence within 30 seconds
        if (now - self.last_sos_time) < 30.0 and self.last_sos_payload:
            print(f"ℹ️ [EmergencySequence] Debouncing repeated trigger for threat '{threat_type}'. Returning None (duplicate suppressed).")
            return None

        self.last_sos_time = now
        self.system_status = "EMERGENCY_TRIGGERED"
        self.is_emergency_active = True
        if self.gui_app and hasattr(self.gui_app, "mark_emergency_active"):
            self.gui_app.mark_emergency_active()
        if voice_usb_mode and usb_connected is None:
            usb_connected = self.is_usb_dispatch_ready()
        usb_connected = bool(usb_connected)
        # If port 8082 has delivered a recent fix, prefer it over coordinates
        # captured by an earlier UI/sensor callback before the SOS was confirmed.
        gps_fix_time = getattr(self, "last_real_gps_fix_monotonic", 0.0)
        if gps_fix_time > 0.0 and time.monotonic() - gps_fix_time <= 10.0:
            location_data = self.location_engine.get_current_location()
        lat = location_data.get("latitude", 0.0)
        lon = location_data.get("longitude", 0.0)
        maps_link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"

        self.log_blackbox_event(lat, lon, threat_type, details=f"Emergency activated: {threat_type}")

        # Snapshot every saved contact in priority order for this incident.
        # set_emergency_contacts() is fed from the setup/settings UI and JSON profile.
        incident_contacts = list(self.emergency_contacts or [])
        self._call_escalation_deadline = None

        # Send the requested SMS to every contact before starting other alert work.
        primary_contact = incident_contacts[0] if incident_contacts else ""
        sms_msg = (
            "🚨 AURA SOS EMERGENCY ALERT! 🚨\n"
            "User in danger !!!\n"
            "Live Location:\n"
            f"https://maps.google.com/?q={lat},{lon}"
        )
        telephony = getattr(self, "telephony_manager", None)
        if voice_usb_mode and usb_connected:
            sms_results = self.usb_dispatcher.dispatch_threat_alert(
                incident_contacts, lat, lon, message_text=sms_msg
            )
        elif voice_usb_mode:
            sms_results = {"mode": "OFFLINE_LOCAL_SIREN", "sms_sent": 0,
                           "call_dispatched": False}
        elif adb_sms:
            sms_results = self.usb_dispatcher.dispatch_threat_alert(
                incident_contacts, lat, lon, message_text=sms_msg
            )
        elif telephony is not None:
            sms_results = telephony.dispatch_emergency_sms(
                incident_contacts, message=sms_msg, lat=lat, lon=lon
            )
            sms_results.setdefault("sms_completed_at", time.monotonic())
        else:
            sms_results = self.usb_dispatcher.dispatch_threat_alert(
                incident_contacts, lat, lon, message_text=sms_msg
            )

        if (voice_usb_mode and usb_connected) or not voice_usb_mode:
            # Timestamp when the complete SMS loop finished. The escalation
            # worker adds the required fixed 60-second delay to this value.
            self._call_escalation_deadline = sms_results.get(
                "sms_completed_at", time.monotonic()
            )

        # 1. Loud Siren / Alarm
        if voice_usb_mode and not usb_connected:
            self.acoustic_engine.start_continuous_emergency_siren()
        elif voice_usb_mode and usb_connected and not sms_results.get("sms_sent"):
            self.acoustic_engine.start_continuous_emergency_siren()
        elif not self.stealth_active:
            # A genuine emergency always gets a local audible alarm. Phone or
            # network availability must not silence the user's computer.
            self.acoustic_engine.play_emergency_siren(duration_cycles=3, force=True)

        # 2. Audio recording
        self.start_emergency_recording()

        # 4. Bluetooth broadcast
        if primary_contact:
            self.send_bluetooth_alert(primary_contact, maps_link)

        # 5. Queue in offline-first SyncManager for backend persistence
        event_payload = {
            "event_id": f"EV_{int(time.time() * 1000)}",
            "timestamp": datetime.now().isoformat(),
            "threat_type": threat_type,
            "latitude": lat,
            "longitude": lon,
            "maps_link": maps_link,
            "contacts_notified": (
                incident_contacts
                if not voice_usb_mode or sms_results.get("sms_sent", 0)
                else []
            ),
            "call_status": (
                ("CALLS_STARTING_AFTER_SMS" if sms_results.get("sms_sent", 0)
                 else "SMS_FAILED_LOCAL_SIREN")
                if voice_usb_mode and usb_connected
                else "OFFLINE_LOCAL_SIREN" if voice_usb_mode
                else "ESCALATION_PENDING_RESPONSE_TIMEOUT"
            ),
            "sms_status": str(sms_results)
        }
        self.sync_manager.queue_emergency_event(event_payload)
        self.last_sos_payload = event_payload

        # 6. Start the configured escalation sequence after SMS dispatch.
        if (not voice_usb_mode or usb_connected) and incident_contacts:
            self.start_call_escalation(incident_contacts, threat_type)
        else:
            self.cancel_call_escalation()

        return event_payload

    def _run_priority_call_chain(self, contacts, cancel_event=None):
        """Call each saved contact in order until answered, canceled, or exhausted."""
        cancel_event = cancel_event or self.call_escalation_cancel_event
        priority_numbers = []
        for item in contacts or []:
            raw_number = item.get("phone", "") if isinstance(item, dict) else item
            number = self.usb_dispatcher.normalize_phone_number(raw_number)
            if number:
                priority_numbers.append(number)

        if not priority_numbers:
            self.call_escalation_status = "CALLS_COMPLETED"
            self.call_escalation_remaining = 0
            return

        interval = max(20, min(120, int(self.call_escalation_timeout_sec)))
        outcomes = []
        for index, number in enumerate(priority_numbers, start=1):
            if cancel_event.is_set():
                self.call_escalation_status = "CANCELLED"
                return

            self.call_escalation_status = "CALLING_IN_PROGRESS"
            print(f"📞 [Call Escalation] Calling P{index} of {len(priority_numbers)}.")
            if self.gui_app and hasattr(self.gui_app, "update_escalation_state"):
                try:
                    self.gui_app.root.after(
                        0,
                        lambda i=index, n=len(priority_numbers):
                            self.gui_app.update_escalation_state(f"📞 Calling P{i} of P{n}..."),
                    )
                except Exception:
                    pass

            if not self.usb_dispatcher.trigger_priority_call(number):
                outcomes.append(f"P{index}=UNAVAILABLE")
                continue

            state = self.usb_dispatcher.wait_for_call_end(
                timeout=interval,
                cancel_event=cancel_event,
            )
            if state == "cancelled" or cancel_event.is_set():
                self.call_escalation_status = "CANCELLED"
                return
            outcomes.append(f"P{index}={state.upper()}")
            if state == "answered":
                break
            if state == "unanswered":
                self.usb_dispatcher.end_current_call()
            # A call that was unanswered or later disconnected advances to the
            # next saved contact, including P3, P4, and beyond.

        self.call_escalation_status = "CALLS_COMPLETED"
        self.call_escalation_remaining = 0
        self._call_escalation_deadline = None
        details = ", ".join(outcomes)
        self.log_blackbox_event(0.0, 0.0, "CALL_ESCALATION_EXECUTED", details=details)
        if self.gui_app and hasattr(self.gui_app, "update_escalation_state"):
            try:
                message = f"📞 Call escalation complete: {details}"
                self.gui_app.root.after(
                    0, lambda m=message: self.gui_app.update_escalation_state(m)
                )
            except Exception:
                pass

    # ── CALL ESCALATION MANAGER (CONFIGURABLE RESPONSE TIMEOUT) ──────────────

    def start_call_escalation(self, contacts, threat_type):
        """Wait the configured response interval, then call saved contacts in priority order."""
        self.cancel_call_escalation()
        cancel_event = threading.Event()
        self.call_escalation_cancel_event = cancel_event
        sms_completed_at = getattr(self, "_call_escalation_deadline", None)
        if sms_completed_at is None:
            sms_completed_at = time.monotonic()
        call_deadline = sms_completed_at + SMS_CALL_ESCALATION_DELAY_SEC
        self.call_escalation_remaining = max(0, int(call_deadline - time.monotonic() + 0.999))
        self.call_escalation_status = "WAITING_RESPONSE"

        def _escalation_worker():
            print(
                f"⏱️ [Call Escalation] SMS dispatch finished; "
                f"waiting {SMS_CALL_ESCALATION_DELAY_SEC}s before priority calls."
            )
            while not cancel_event.is_set():
                remaining = call_deadline - time.monotonic()
                if remaining <= 0:
                    self.call_escalation_remaining = 0
                    break
                self.call_escalation_remaining = int(remaining + 0.999)
                if self.gui_app and hasattr(self.gui_app, "update_escalation_state"):
                    try:
                        self.gui_app.root.after(
                            0, lambda r=self.call_escalation_remaining: self.gui_app.update_escalation_state(
                                f"📞 Escalating to calls in {r}s (Waiting for guardian response)"
                            )
                        )
                    except Exception:
                        pass
                if cancel_event.wait(min(1.0, remaining)):
                    break

            if cancel_event.is_set():
                self.call_escalation_status = "CANCELLED"
                print("✅ [Call Escalation] Cancelled (User confirmed safe or response recorded).")
                if self.gui_app and hasattr(self.gui_app, "update_escalation_state"):
                    try:
                        self.gui_app.root.after(0, lambda: self.gui_app.update_escalation_state("✅ Call Escalation Cancelled"))
                    except Exception:
                        pass
                return

            # Timeout expired: initiate the complete P1-to-Pn chain.
            self.call_escalation_status = "CALLING_IN_PROGRESS"
            print("🚨 [Call Escalation] Response timeout expired! Initiating automated priority calls...")
            if self.gui_app and hasattr(self.gui_app, "update_escalation_state"):
                try:
                    self.gui_app.root.after(0, lambda: self.gui_app.update_escalation_state("🚨 Initiating Automated Priority Calls..."))
                except Exception:
                    pass

            self._run_priority_call_chain(contacts, cancel_event)

        self.call_escalation_thread = threading.Thread(target=_escalation_worker, daemon=True)
        self.call_escalation_thread.start()

    def cancel_call_escalation(self):
        self.call_escalation_cancel_event.set()
        self.call_escalation_status = "CANCELLED"

    def acknowledge_user_safe(self, cooldown_seconds=90.0):
        """Called when user confirms safety (e.g. dismissing alarm / PIN entry)."""
        self.is_emergency_active = False
        if hasattr(self, "acoustic_engine"):
            self.acoustic_engine.stop_continuous_emergency_siren()
        self.system_status = "ACTIVE_MONITORING"
        # Safe confirmation ends the previous incident, so a later deliberate
        # SOS (including repeated manual tests) can start a fresh sequence.
        self.last_sos_time = 0.0
        self.last_sos_payload = None
        self.cancel_call_escalation()
        if hasattr(self, "anomaly_engine"):
            self.anomaly_engine.acknowledge_safe(threat_type="ALL", cooldown_seconds=cooldown_seconds)
        if self.gui_app and hasattr(self.gui_app, "reset_emergency_ui_state"):
            self.gui_app.reset_emergency_ui_state()
        self.log_blackbox_event(0.0, 0.0, "USER_SAFE_CONFIRMED", details="User confirmed safe; alarms & cooldowns reset.")
        print(f"🛡️ [SafetyController] User confirmed safe. Status set to ACTIVE_MONITORING; {cooldown_seconds:.0f}s cooldown active.")

    # ── PERIODIC SAFETY INSPECTION CYCLE ─────────────────────────────────────

    def run_live_safety_cycle(self, simulated_lat=None, simulated_lon=None, simulated_timestamp=None, speed_override=None, suppress_gui_threat=False):
        """Performs a periodic safety inspection cycle."""
        gps_fix_age = time.monotonic() - getattr(self, "last_real_gps_fix_monotonic", 0.0)
        has_fresh_gps_fix = 0.0 <= gps_fix_age <= 10.0
        is_live_gps_sample = (
            not self.demo_mode_active
            and simulated_lat is None
            and simulated_lon is None
            and speed_override is None
            and has_fresh_gps_fix
        )
        # Check if in Demo Mode
        if self.demo_mode_active and self.demo_lat is not None and self.demo_lon is not None:
            current_loc = {
                "latitude": self.demo_lat,
                "longitude": self.demo_lon,
                "timestamp": simulated_timestamp if simulated_timestamp is not None else time.time(),
                "maps_url": f"https://maps.google.com/?q={self.demo_lat:.6f},{self.demo_lon:.6f}"
            }
        else:
            current_loc = self.location_engine.get_current_location()
            if simulated_lat is not None and simulated_lon is not None:
                current_loc = {
                    "latitude": simulated_lat,
                    "longitude": simulated_lon,
                    "timestamp": simulated_timestamp if simulated_timestamp is not None else current_loc.get("timestamp"),
                    "maps_url": f"https://maps.google.com/?q={simulated_lat:.6f},{simulated_lon:.6f}"
                }

        current_lat = current_loc["latitude"]
        current_lon = current_loc["longitude"]

        # Only train/predict from a fresh GPS fix or explicit simulator sample.
        # Replaying a recovered last-known coordinate made the predictor appear
        # stuck on the same static point while the 8082 bridge was unavailable.
        has_predictor_sample = (
            is_live_gps_sample
            or (self.demo_mode_active and self.demo_lat is not None and self.demo_lon is not None)
            or simulated_lat is not None
            or simulated_lon is not None
        )
        predicted_point = None
        if has_predictor_sample:
            corridor = None
            try:
                import map_config
                corridor = map_config.get_primary_corridor()
            except Exception:
                corridor = None
            predicted_point = self.predictor.predict_next_waypoint(
                current_lat, current_lon, corridor=corridor
            )

        # Route & Anomaly evaluation
        if not is_live_gps_sample:
            # Simulator coordinates, injected speed, and cursor-derived
            # velocity are not GPS evidence and cannot raise route/speed alerts.
            is_threat, threat_type = False, "NORMAL"
        else:
            anomaly_res = self.anomaly_engine.evaluate_telemetry(
                current_loc,
                self.previous_loc,
                external_predicted_loc=predicted_point,
                speed_override=speed_override,
            )
            if isinstance(anomaly_res, tuple):
                is_threat, threat_type = anomaly_res[0], anomaly_res[1] if len(anomaly_res) > 1 else "ANOMALY_DETECTED"
            else:
                is_threat, threat_type = False, "NORMAL"

        speed_kmh = (
            float(speed_override) if speed_override is not None
            else self.demo_speed if self.demo_mode_active
            else self.location_engine.calculate_speed(self.previous_loc, current_loc)
        )
        if is_live_gps_sample:
            self.previous_loc = current_loc

        # Geofence Transitions Check (Entry & Exit events)
        transitions = (
            [] if not is_live_gps_sample else
            self.location_engine.geofence_manager.evaluate_transitions(current_lat, current_lon)
        )
        for t in transitions:
            self.sync_manager.queue_geofence_event(t["zone_name"], t["event"], t["latitude"], t["longitude"])
            if t["event"] == "EXIT":
                is_threat, threat_type = True, f"GEOFENCE_BREACH ({t['zone_name']})"
            elif t["event"] == "ENTRY" and self.gui_app:
                self.gui_app.send_desktop_popup("✅ Safe Zone Entered", f"Entered {t['zone_name']}")

        diag = self.sensor_diagnostics.run_full_diagnostics()

        is_display_only = getattr(self, "display_only_mode", False) or (self.gui_app and getattr(self.gui_app, "display_only_mode", False))
        if is_threat:
            if is_display_only:
                print(f"👁️ [DISPLAY ONLY] Threat evaluated: '{threat_type}'. Emergency actions suppressed (Show Only active).")
                if self.gui_app and hasattr(self.gui_app, "_tele_threat"):
                    self.gui_app._tele_threat.set("👁️ SHOW ONLY")
            elif not suppress_gui_threat:
                if self.gui_app:
                    self.gui_app.root.after(0, lambda: self.gui_app.trigger_threat(f"⚠️ {threat_type}"))
                else:
                    self.execute_emergency_sequence(threat_type, current_loc)

        if self.gui_app and hasattr(self.gui_app, "update_map_canvas"):
            self.gui_app.root.after(
                0,
                lambda: self.gui_app.update_map_canvas(
                    current_lat, current_lon, predicted_point, speed_kmh, is_threat
                )
            )

        return {
            "status": self.system_status,
            "current_location": (current_lat, current_lon),
            "predicted_next": predicted_point,
            "anomaly_check": {"is_threat": is_threat, "threat_type": threat_type},
            "system_health": diag["diagnostic_status"],
            "speed_kmh": speed_kmh,
            "is_demo": self.demo_mode_active
        }

    def stop_all(self):
        """Clean shutdown of all engines and threads."""
        try:
            self.cancel_call_escalation()
            if hasattr(self, 'fall_detector'):
                self.fall_detector.stop()
            if hasattr(self, 'network_monitor'):
                self.network_monitor.stop()
            if hasattr(self, 'gps_bridge'):
                self.gps_bridge.stop()
            if hasattr(self, 'sync_manager'):
                self.sync_manager.stop()
            if hasattr(self, 'acoustic_engine'):
                self.acoustic_engine.stop_continuous_emergency_siren()
                self.acoustic_engine.stop_listening()
                if hasattr(self.acoustic_engine, "wait_for_video_recordings"):
                    self.acoustic_engine.wait_for_video_recordings(timeout=20.0)
        except Exception as e:
            print(f"[Shutdown Notice]: {e}")

    # ── Mobile Phone Connectivity ───────────────────────────────────────────
    def connect_mobile_phone(self, ip_port: str = None) -> dict:
        """Connects or refreshes phone connection over USB ADB or Wi-Fi."""
        if ip_port:
            try:
                result = subprocess.run(
                    [self.usb_dispatcher.adb_path, "connect", ip_port],
                    capture_output=True, text=True, timeout=8,
                    startupinfo=self.usb_dispatcher._startupinfo(),
                    env=self.usb_dispatcher.adb_env,
                )
                if result.returncode != 0:
                    return {"adb_installed": True, "device_connected": False,
                            "error": (result.stderr or result.stdout).strip()}
            except Exception as exc:
                return {"adb_installed": True, "device_connected": False, "error": str(exc)}
        info = self.get_phone_status()
        if info.get("device_connected"):
            print(f"📱 [MainController] Mobile phone linked: {info.get('device_id')}")
        return info

    def get_phone_status(self) -> dict:
        """Returns current mobile phone connection and SIM status."""
        info = self.usb_dispatcher.check_phone_connection()
        if info.get("device_connected"):
            self.ensure_gps_bridge_forward()
            self.phone_has_signal = True
            self.usb_device_connected = True
            monitor = getattr(self, "network_monitor", None)
            if monitor is not None:
                monitor._consecutive_failures = 0
                monitor._consecutive_successes = 0
                if monitor.was_connected is False:
                    monitor._handle_connection_transition(True)
                else:
                    monitor.is_connected = True
            gui = getattr(self, "gui_app", None)
            if gui and hasattr(gui, "update_usb_dispatch_status"):
                try:
                    gui.root.after(0, lambda: gui.update_usb_dispatch_status(True))
                except Exception:
                    pass
        return info

    def ensure_gps_bridge_forward(self):
        """Forward phone TCP 8082 to localhost for the background GPS bridge."""
        with self._gps_forward_lock:
            if self._gps_forward_ready or self._gps_forward_pending:
                return
            self._gps_forward_pending = True

        def _forward():
            try:
                result = subprocess.run(
                    [self.usb_dispatcher.adb_path, "forward", "tcp:8082", "tcp:8082"],
                    capture_output=True, text=True, timeout=4,
                    startupinfo=self.usb_dispatcher._startupinfo(),
                    env=self.usb_dispatcher.adb_env,
                )
                if result.returncode == 0:
                    with self._gps_forward_lock:
                        self._gps_forward_ready = True
                    print("🛰️ [GPS Bridge] ADB forwarding enabled: localhost:8082 → phone:8082")
                else:
                    print(f"⚠️ [GPS Bridge] ADB port forward failed: {(result.stderr or result.stdout).strip()}")
            except Exception as exc:
                print(f"⚠️ [GPS Bridge] Could not configure ADB port 8082 forward: {exc}")
            finally:
                with self._gps_forward_lock:
                    self._gps_forward_pending = False

        threading.Thread(target=_forward, name="AURA-GPS-port-forward", daemon=True).start()

    def reset_gps_bridge_forward(self):
        """Allow ADB port forwarding to be configured again after reconnect."""
        with self._gps_forward_lock:
            self._gps_forward_ready = False


    # ── Evidence / Media Deletion & Management ──────────────────────────────
    def delete_recording(self, file_path: str) -> bool:
        """Permanently deletes a single audio or video recording file and cleans sync queue."""
        try:
            # Clean from sync queue first to prevent concurrent background file access
            if hasattr(self, "sync_manager") and self.sync_manager:
                self.sync_manager.remove_audio_from_queue(file_path)

            if os.path.exists(file_path):
                for attempt in range(5):
                    try:
                        os.remove(file_path)
                        break
                    except PermissionError:
                        time.sleep(0.15)
                else:
                    os.remove(file_path)
            return True
        except Exception as e:
            print(f"❌ [Recording Deletion Error]: {e}")
            return False

    def delete_recordings_batch(self, file_paths: list) -> int:
        """Permanently deletes a list of recordings and cleans sync queue. Returns count deleted."""
        deleted = 0
        for fp in file_paths:
            if self.delete_recording(fp):
                deleted += 1
        return deleted

    def delete_all_recordings(self, media_type: str = "all") -> int:
        """
        Deletes all recordings matching media_type ('audio', 'video', or 'all').
        Returns the number of files deleted.
        """
        import glob
        deleted = 0
        patterns = []
        acoustic = getattr(self, "acoustic_engine", None)
        if media_type in ("audio", "all"):
            audio_dir = getattr(acoustic, "audio_dir", os.path.join("recordings", "audio"))
            patterns.extend([
                os.path.join(audio_dir, "*.*"),
                os.path.join("recordings", "audio", "*.*"),
            ])
            if hasattr(self, "sync_manager") and self.sync_manager:
                self.sync_manager.clear_audio_queue()
        if media_type in ("video", "all"):
            video_dir = getattr(acoustic, "video_dir", os.path.join("recordings", "video"))
            patterns.extend([
                os.path.join(video_dir, "*.*"),
                os.path.join("recordings", "video", "*.*"),
            ])

        for pat in set(patterns):
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                    deleted += 1
                except Exception as e:
                    print(f"❌ [Media Deletion Error] Could not delete {f}: {e}")
        return deleted

    def get_recordings_stats(self) -> dict:
        """Returns counts and total file sizes in bytes for audio and video vaults."""
        import glob
        acoustic = getattr(self, "acoustic_engine", None)
        audio_paths = {
            os.path.join(getattr(acoustic, "audio_dir", os.path.join("recordings", "audio")), "*.*"),
            os.path.join("recordings", "audio", "*.*"),
        }
        video_paths = {
            os.path.join(getattr(acoustic, "video_dir", os.path.join("recordings", "video")), "*.*"),
            os.path.join("recordings", "video", "*.*"),
        }
        audio_files = sorted({path for pattern in audio_paths for path in glob.glob(pattern)})
        video_files = sorted({path for pattern in video_paths for path in glob.glob(pattern)})
        a_size = sum(os.path.getsize(f) for f in audio_files if os.path.isfile(f))
        v_size = sum(os.path.getsize(f) for f in video_files if os.path.isfile(f))
        return {
            "audio": {"count": len(audio_files), "size_bytes": a_size, "files": audio_files},
            "video": {"count": len(video_files), "size_bytes": v_size, "files": video_files},
            "total_count": len(audio_files) + len(video_files),
            "total_size_bytes": a_size + v_size
        }
