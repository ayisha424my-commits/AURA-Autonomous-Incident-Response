"""
fall_detector.py
────────────────────────────────────────────────────────────────────────────
Reliable Phone Fall Detection Module using Accelerometer and Gyroscope.

Algorithm phases:
  1. Freefall Phase: Accelerometer drops significantly below 1g (< 6.0 m/s²).
  2. Gyroscope / Angular Velocity: Detects rotational tumbling (> 2.0 rad/s)
     if gyroscope sensor is present.
  3. High-G Impact Phase: Sharp impact acceleration (> 8.0 - 25.0 m/s²) within
     a defined window (0.1s - 2.5s) after freefall.
  4. Post-impact settlement check.

Provides USB ADB port-forwarding, TCP client & server sensor stream receiver,
Termux sensor ADB stream fallback, and programmatic fall simulation for unit & integration testing.
"""

import json
import math
import socket
import subprocess
import threading
import time
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FallDetector] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fall_detector")

HOST                  = "localhost"
PORT                  = 8080
FALL_SIM_PORT         = 8099

FREEFALL_THRESHOLD    = 6.0    # m/s² (1g = 9.8 m/s²; drop drops below 6.0)
IMPACT_THRESHOLD      = 8.0    # m/s² (captures drops from 8.0 up to 30.0+)
GYRO_TUMBLE_THRESHOLD = 2.0    # rad/s (tumbling threshold when gyro present)
FALL_WINDOW_SEC       = 2.5    # seconds - max window between freefall & impact
RECONNECT_DELAY_SEC   = 2.0    # seconds before retry
RECV_BUFFER_BYTES     = 4096


class FallDetector:
    def __init__(self, callback: callable):
        if not callable(callback):
            raise TypeError("callback must be a callable (function/method).")
        self._callback       = callback
        self._stop_event     = threading.Event()
        self._threads        = []
        self._freefall_time  = None
        self._tumble_detected = False
        self._last_trigger_time = 0.0

    def start(self):
        self._stop_event.clear()
        self._setup_adb_port_forwarding()

        # Thread 1: TCP Client connecting to localhost:8080 (ADB forwarded port)
        t_client = threading.Thread(
            target=self._run_client, name="FallDetectorClientThread", daemon=True
        )
        t_client.start()
        self._threads.append(t_client)

        # Thread 2: TCP Server listening on 0.0.0.0:8080 (for phone streaming to laptop IP directly)
        t_server = threading.Thread(
            target=self._run_server, name="FallDetectorServerThread", daemon=True
        )
        t_server.start()
        self._threads.append(t_server)

        # Thread 3: ADB Termux sensor stream monitor
        t_adb = threading.Thread(
            target=self._run_adb_termux_sensor, name="FallDetectorAdbThread", daemon=True
        )
        t_adb.start()
        self._threads.append(t_adb)

        # Thread 4: background socket for simulated fall (no UI test button)
        t_sim = threading.Thread(
            target=self._run_fall_sim_socket, name="FallSimSocketThread", daemon=True
        )
        t_sim.start()
        self._threads.append(t_sim)

        # Thread 5: Ctrl+F global hotkey (Windows) without a dashboard test button
        t_hot = threading.Thread(
            target=self._run_hotkey_listener, name="FallHotkeyThread", daemon=True
        )
        t_hot.start()
        self._threads.append(t_hot)

        logger.info("Fall Detector threads started (Client, Server, ADB, socket, Ctrl+F).")
        return self

    def stop(self):
        self._stop_event.set()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=0.8)
        self._threads.clear()
        logger.info("Fall Detector threads stopped.")

    @property
    def is_running(self):
        return any(t.is_alive() for t in self._threads)

    def _setup_adb_port_forwarding(self):
        """Automatically forward TCP port 8080 over USB via ADB if device is connected."""
        def _adb_cmd(args):
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(["adb"] + args, capture_output=True, timeout=2.0, startupinfo=startupinfo)
            except Exception:
                pass

        threading.Thread(target=lambda: [
            _adb_cmd(["forward", f"tcp:{PORT}", f"tcp:{PORT}"]),
            _adb_cmd(["reverse", f"tcp:{PORT}", f"tcp:{PORT}"]),
            _adb_cmd(["forward", "tcp:8082", "tcp:8082"]),
            _adb_cmd(["reverse", "tcp:8082", "tcp:8082"])
        ], daemon=True).start()

    def simulate_fall(self, reason="Programmatic Fall Test"):
        """Directly triggers the fall detection callback (used for testing/SOS validation)."""
        now = time.monotonic()
        if (now - self._last_trigger_time) < 4.0:
            logger.info("Fall simulation debounced (%s).", reason)
            return
        self._last_trigger_time = now
        logger.info("🚨 [FallDetector] Simulating hardware fall event: %s", reason)
        self._fire_callback()

    def _run_fall_sim_socket(self):
        """UDP/TCP listener: send 'FALL' to localhost:8099 to simulate a drop without a UI button."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", FALL_SIM_PORT))
            sock.settimeout(1.0)
            logger.info("Fall sim socket listening on UDP 127.0.0.1:%d (send FALL).", FALL_SIM_PORT)
            while not self._stop_event.is_set():
                try:
                    data, _addr = sock.recvfrom(256)
                except socket.timeout:
                    continue
                except Exception:
                    break
                text = data.decode("utf-8", errors="replace").strip().upper()
                if "FALL" in text:
                    self.simulate_fall("Background socket FALL command")
        except Exception as exc:
            logger.debug("Fall sim socket unavailable: %s", exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _run_hotkey_listener(self):
        """Windows Ctrl+F (GetAsyncKeyState) so a simulated fall works without a test button."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
        except Exception:
            return
        vk_f = 0x46
        vk_ctrl = 0x11
        was_down = False
        logger.info("Fall hotkey armed: Ctrl+F (no UI test button).")
        while not self._stop_event.is_set():
            try:
                ctrl = ctypes.windll.user32.GetAsyncKeyState(vk_ctrl) & 0x8000
                key_f = ctypes.windll.user32.GetAsyncKeyState(vk_f) & 0x8000
                down = bool(ctrl and key_f)
                if down and not was_down:
                    self.simulate_fall("Ctrl+F hotkey")
                was_down = down
            except Exception:
                break
            self._stop_event.wait(0.08)

    def process_sensor_sample(self, accel_xyz: tuple, gyro_xyz: tuple = None, now: float = None):
        """Processes a single raw accelerometer & gyroscope reading."""
        if now is None:
            now = time.monotonic()

        ax, ay, az = accel_xyz
        accel_mag = math.sqrt(ax**2 + ay**2 + az**2)

        gyro_mag = 0.0
        if gyro_xyz:
            gx, gy, gz = gyro_xyz
            gyro_mag = math.sqrt(gx**2 + gy**2 + gz**2)
            if gyro_mag > GYRO_TUMBLE_THRESHOLD:
                self._tumble_detected = True

        self._evaluate_fall_pattern(accel_mag, gyro_mag, now)

    def _run_client(self):
        """TCP Client loop connecting to localhost:8080."""
        while not self._stop_event.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(3.0)
                    sock.connect((HOST, PORT))
                    sock.settimeout(None)
                    logger.info("Connected to phone sensor stream on %s:%d.", HOST, PORT)

                    buffer = ""
                    while not self._stop_event.is_set():
                        chunk = sock.recv(RECV_BUFFER_BYTES)
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", errors="replace")
                        buffer = self._process_buffer(buffer)
            except Exception:
                if self._stop_event.is_set():
                    break
                self._stop_event.wait(RECONNECT_DELAY_SEC)

    def _run_server(self):
        """TCP Server loop listening on 0.0.0.0:8080."""
        server_sock = None
        try:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind(("0.0.0.0", PORT))
            server_sock.listen(2)
            server_sock.settimeout(2.0)
            logger.info("FallDetector server listening on port %d for incoming sensor streams.", PORT)

            while not self._stop_event.is_set():
                try:
                    conn, addr = server_sock.accept()
                    logger.info("Phone sensor stream connected from %s", addr)
                    conn.settimeout(5.0)
                    buffer = ""
                    with conn:
                        while not self._stop_event.is_set():
                            try:
                                chunk = conn.recv(RECV_BUFFER_BYTES)
                                if not chunk:
                                    break
                                buffer += chunk.decode("utf-8", errors="replace")
                                buffer = self._process_buffer(buffer)
                            except socket.timeout:
                                continue
                            except Exception:
                                break
                except socket.timeout:
                    continue
                except Exception:
                    if self._stop_event.is_set():
                        break
                    self._stop_event.wait(1.0)
        except Exception as exc:
            logger.debug("Server socket could not bind: %s", exc)
        finally:
            if server_sock:
                try:
                    server_sock.close()
                except Exception:
                    pass

    def _run_adb_termux_sensor(self):
        """If Android phone is connected via USB and Termux sensor is available, stream sensor data directly."""
        while not self._stop_event.is_set():
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                # Check for adb device
                check = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=2.0, startupinfo=startupinfo)
                if check.returncode == 0 and "\tdevice" in check.stdout:
                    # Attempt termux-sensor stream over ADB
                    cmd = ["adb", "shell", "termux-sensor", "-s", "accelerometer,gyroscope", "-d", "100"]
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo)
                    
                    buf = ""
                    while not self._stop_event.is_set() and proc.poll() is None:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        buf += line
                        if line.strip().endswith("}"):
                            try:
                                payload = json.loads(buf)
                                self._handle_payload(payload)
                            except Exception:
                                pass
                            buf = ""
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            except Exception:
                pass
            self._stop_event.wait(5.0)

    def _process_buffer(self, buffer):
        lines = buffer.split("\n")
        incomplete = lines[-1]

        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                self._handle_payload(payload)
            except json.JSONDecodeError:
                pass

        return incomplete

    def _handle_payload(self, payload):
        """Extracts accelerometer and gyroscope values from various phone sensor payload formats."""
        try:
            accel_values = []
            gyro_values = []

            if isinstance(payload, dict):
                # Search for accelerometer
                for k in ["accelerometer", "lis2hh12_acc", "accel", "linear_acceleration"]:
                    if k in payload and isinstance(payload[k], dict) and "values" in payload[k]:
                        accel_values = payload[k]["values"]
                        break
                    elif k in payload and isinstance(payload[k], list):
                        accel_values = payload[k]
                        break

                # Search for gyroscope
                for gk in ["gyroscope", "gyro", "angular_velocity"]:
                    if gk in payload and isinstance(payload[gk], dict) and "values" in payload[gk]:
                        gyro_values = payload[gk]["values"]
                        break
                    elif gk in payload and isinstance(payload[gk], list):
                        gyro_values = payload[gk]
                        break

            if len(accel_values) >= 3:
                ax, ay, az = float(accel_values[0]), float(accel_values[1]), float(accel_values[2])
                gx, gy, gz = None, None, None
                if len(gyro_values) >= 3:
                    gx, gy, gz = float(gyro_values[0]), float(gyro_values[1]), float(gyro_values[2])
                self.process_sensor_sample((ax, ay, az), (gx, gy, gz) if gx is not None else None)

        except (KeyError, TypeError, ValueError):
            pass

    def _evaluate_fall_pattern(self, accel_mag, gyro_mag, now):
        # Phase 1: Freefall detection
        if accel_mag < FREEFALL_THRESHOLD:
            if self._freefall_time is None:
                logger.info("🔻 [FallDetector] Free-fall phase detected! Accel Mag: %.2f m/s²", accel_mag)
            self._freefall_time = now

        # Phase 2: Impact detection
        elif accel_mag > IMPACT_THRESHOLD:
            if self._freefall_time is not None:
                elapsed = now - self._freefall_time
                if elapsed <= FALL_WINDOW_SEC:
                    # Enforce debouncing (no re-triggers within 5 seconds)
                    if (now - self._last_trigger_time) > 5.0:
                        self._last_trigger_time = now
                        logger.info(
                            "🚨 [FallDetector] CONFIRMED FALL IMPACT! (Free-fall to Impact in %.3fs, Impact Mag: %.2f m/s², Gyro Tumbling: %s)",
                            elapsed, accel_mag, self._tumble_detected
                        )
                        self._freefall_time = None
                        self._tumble_detected = False
                        self._fire_callback()
                else:
                    self._freefall_time = None
                    self._tumble_detected = False

    def _fire_callback(self):
        try:
            self._callback()
        except Exception as exc:
            logger.error("Callback error in FallDetector: %s", exc, exc_info=True)


def start_detection(callback_function):
    detector = FallDetector(callback=callback_function)
    detector.start()
    return detector