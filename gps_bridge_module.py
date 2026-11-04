import json
import math
import socket
import threading
import time

HOST = "localhost"
PORT = 8082
RECONNECT_DELAY_SEC = 2.0
RECV_BUFFER_BYTES = 4096
EARTH_RADIUS_M = 6_371_000.0


def haversine_distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two GPS fixes, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(
        math.sqrt(a), math.sqrt(max(0.0, 1.0 - a))
    )


def project_next_coordinate(lat1, lon1, lat2, lon2):
    """Project one observed movement segment forward from the latest fix."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    lam1, lam2 = math.radians(lon1), math.radians(lon2)
    dlam = lam2 - lam1
    bearing = math.atan2(
        math.sin(dlam) * math.cos(phi2),
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlam),
    )
    angular_distance = haversine_distance_m(lat1, lon1, lat2, lon2) / EARTH_RADIUS_M
    projected_lat = math.asin(
        math.sin(phi2) * math.cos(angular_distance)
        + math.cos(phi2) * math.sin(angular_distance) * math.cos(bearing)
    )
    projected_lon = lam2 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(phi2),
        math.cos(angular_distance) - math.sin(phi2) * math.sin(projected_lat),
    )
    return math.degrees(projected_lat), (math.degrees(projected_lon) + 540) % 360 - 180


class GPSBridge:
    def __init__(self, controller):
        self.controller = controller
        self._stop_event = threading.Event()

    def start(self):
        self._stop_event.clear()
        threading.Thread(target=self._run, daemon=True).start()
        print("🛰️ [GPS Bridge] Started - connecting to phone's location stream...")
        return self

    def stop(self):
        self._stop_event.set()

    def _run(self):
        last_notice_time = 0.0
        while not self._stop_event.is_set():
            connect_started = time.monotonic()
            try:
                self._connect_and_read()
            except Exception as exc:
                now = time.time()
                if not self._stop_event.is_set() and (now - last_notice_time >= 30.0):
                    print(f"🛰️ [GPS Bridge] Standby on port {PORT}: {exc}")
                    last_notice_time = now
            finally:
                # Normal peer closes used to reconnect immediately and flood
                # the console. Back off on both normal and exceptional closes.
                elapsed = time.monotonic() - connect_started
                if not self._stop_event.is_set():
                    self._stop_event.wait(max(0.0, RECONNECT_DELAY_SEC - elapsed))

    def _connect_and_read(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect((HOST, PORT))
            sock.settimeout(None)
            print("🛰️ [GPS Bridge] Connected. Listening for location data...")

            buffer = ""
            fixes_before_close = getattr(self, "_accepted_fix_count", 0)
            while not self._stop_event.is_set():
                chunk = sock.recv(RECV_BUFFER_BYTES)
                if not chunk:
                    # Some phone stream providers send a single JSON object
                    # and close without appending the usual newline delimiter.
                    tail = buffer.strip()
                    if tail:
                        self._process_payload_line(tail)
                    now = time.time()
                    if now - getattr(self, "_last_peer_close_notice", 0.0) >= 30.0:
                        if getattr(self, "_accepted_fix_count", 0) == fixes_before_close:
                            detail = "before sending a GPS fix"
                        else:
                            detail = "after sending GPS data"
                        print(
                            f"🛰️ [GPS Bridge] Phone closed its stream {detail}; "
                            f"retrying in {RECONNECT_DELAY_SEC:.0f}s."
                        )
                        self._last_peer_close_notice = now
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                buffer = self._process_buffer(buffer)

    def _process_buffer(self, buffer):
        lines = buffer.split("\n")
        incomplete = lines[-1]

        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            self._process_payload_line(line)

        # Also accept complete JSON objects that arrive without a newline,
        # including multiple compact objects in one TCP packet.
        pending = incomplete.lstrip()
        while pending:
            try:
                payload, consumed = json.JSONDecoder().raw_decode(pending)
            except json.JSONDecodeError:
                break
            if isinstance(payload, dict):
                self._handle_payload(payload)
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        self._handle_payload(item)
            pending = pending[consumed:].lstrip()

        return pending

    def _process_payload_line(self, line):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            if line:
                print(f"⚠️ [GPS Bridge] Ignoring non-JSON stream data: {line[:120]!r}")
            return
        if isinstance(data, dict):
            self._handle_payload(data)
        elif isinstance(data, list):
            for payload in data:
                if isinstance(payload, dict):
                    self._handle_payload(payload)

    def _handle_payload(self, data):
        lat = data.get("latitude", data.get("lat"))
        lon = data.get("longitude", data.get("lon"))
        if lat is None or lon is None:
            return
        try:
            lat = float(lat)
            lon = float(lon)
            accuracy = data.get("accuracy", data.get("accuracy_m"))
            accuracy = float(accuracy) if accuracy is not None else None
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        # Poor-quality fixes should not move the app's authoritative position.
        if accuracy is not None and (accuracy < 0.0 or accuracy > 50.0):
            print(f"⚠️ [GPS Bridge] Ignoring low-quality fix ({accuracy:.0f} m accuracy).")
            return

        now = time.monotonic()
        previous_fix = getattr(self.controller, "last_real_gps_fix_monotonic", 0.0)
        previous_coords = getattr(self, "_last_accepted_fix", None)
        previous_coords_time = getattr(self, "_last_accepted_fix_time", 0.0)
        if previous_fix <= 0.0 or now - previous_fix > 10.0:
            # Establish a clean velocity/deviation baseline after startup or a
            # long stream interruption so stale coordinates cannot look like
            # a sudden movement.
            self.controller.previous_loc = {
                "latitude": lat, "longitude": lon, "timestamp": time.time()
            }
            anomaly_engine = getattr(self.controller, "anomaly_engine", None)
            if anomaly_engine is not None:
                if hasattr(anomaly_engine, "route_deviation_samples"):
                    anomaly_engine.route_deviation_samples = 0
                if hasattr(anomaly_engine, "trajectory_history"):
                    anomaly_engine.trajectory_history.clear()
            predictor = getattr(self.controller, "predictor", None)
            if predictor is not None and hasattr(predictor, "reset"):
                predictor.reset()
            previous_coords = None

        if previous_coords is not None:
            old_lat, old_lon = previous_coords
            elapsed_s = now - previous_coords_time
            distance_m = haversine_distance_m(old_lat, old_lon, lat, lon)
            speed_kmh = (distance_m / elapsed_s * 3.6) if elapsed_s > 0 else 0.0
            if distance_m > 1.0:
                next_lat, next_lon = project_next_coordinate(old_lat, old_lon, lat, lon)
                print(
                    f"[GPS Bridge] Haversine movement: {distance_m:.1f} m "
                    f"in {elapsed_s:.1f} s ({speed_kmh:.1f} km/h)"
                )
                print(
                    f"[GPS Bridge] Projected next location: "
                    f"{next_lat:.6f}, {next_lon:.6f}"
                )
            else:
                print(
                    f"[GPS Bridge] Haversine movement: {distance_m:.1f} m "
                    f"in {elapsed_s:.1f} s; under 1 m, holding projection."
                )
        else:
            print("[GPS Bridge] First GPS fix received; waiting for movement to project the next point.")

        self.controller.location_engine.current_lat = lat
        self.controller.location_engine.current_lon = lon
        self.controller.location_engine.gps_accuracy_m = accuracy
        self.controller.last_real_gps_fix_monotonic = now
        self._last_accepted_fix = (lat, lon)
        self._last_accepted_fix_time = now
        self._accepted_fix_count = getattr(self, "_accepted_fix_count", 0) + 1
        print(f"🛰️ [GPS Bridge] Real location: {lat:.6f}, {lon:.6f}")


if __name__ == "__main__":
    class DummyLocationEngine:
        current_lat = 0.0
        current_lon = 0.0

    class DummyController:
        location_engine = DummyLocationEngine()

    print("=== GPS Bridge - Standalone Test ===")
    bridge = GPSBridge(DummyController())
    bridge.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        bridge.stop()
