"""Standard-library HTTP backend for the AURA safety system."""

import json
import math
import os
import re
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BACKEND_STORAGE_DIR = os.path.join(DATA_DIR, "backend_storage")
BACKEND_AUDIO_DIR = os.path.join(BACKEND_STORAGE_DIR, "audio")
BACKEND_VIDEO_DIR = os.path.join(BACKEND_STORAGE_DIR, "video")
STORE_FILE = os.path.join(DATA_DIR, "backend_store.json")
PAST_LOCATIONS_FILE = os.path.join(DATA_DIR, "past_locations.json")
ROUTE_FILE = os.path.join(DATA_DIR, "planned_route.json")
os.makedirs(BACKEND_AUDIO_DIR, exist_ok=True)
os.makedirs(BACKEND_VIDEO_DIR, exist_ok=True)

TABLES = ("emergency_events", "audio_evidence", "geofence_events", "location_history", "battery_logs")


def load_store():
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            store = json.load(f)
        if not isinstance(store, dict):
            raise ValueError("JSON store must be an object")
    except (OSError, ValueError, json.JSONDecodeError):
        store = {}
    for table in TABLES:
        if not isinstance(store.get(table), list):
            store[table] = []
    return store


def save_store(store):
    os.makedirs(DATA_DIR, exist_ok=True)
    temp_path = STORE_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, STORE_FILE)


STORE = load_store()


def load_planned_route():
    try:
        with open(ROUTE_FILE, "r", encoding="utf-8") as f:
            route = json.load(f)
        return route if isinstance(route, list) else []
    except (OSError, ValueError, json.JSONDecodeError):
        return []


PLANNED_ROUTE = load_planned_route()


def read_locations():
    try:
        with open(PAST_LOCATIONS_FILE, "r", encoding="utf-8") as f:
            locations = json.load(f)
        return locations if isinstance(locations, list) else []
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def calculate_distance(lat1, lon1, lat2, lon2):
    radius = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def save_location(lat, lon, speed_kmh=0.0, is_deviated=False):
    """Append location telemetry to the JSON store and legacy location history."""
    stamp = datetime.now().isoformat()
    STORE["location_history"].append({
        "id": len(STORE["location_history"]) + 1,
        "timestamp": stamp,
        "latitude": lat,
        "longitude": lon,
        "speed_kmh": speed_kmh,
        "is_deviated": int(bool(is_deviated)),
        "created_at": stamp,
    })
    save_store(STORE)
    history = read_locations()
    history.append({"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "lat": lat, "lon": lon})
    with open(PAST_LOCATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-500:], f, indent=4)


def predict_next_location():
    history = read_locations()
    if len(history) < 2:
        return None
    previous, last = history[-2], history[-1]
    return {
        "predicted_lat": round(last["lat"] + last["lat"] - previous["lat"], 6),
        "predicted_lon": round(last["lon"] + last["lon"] - previous["lon"], 6),
        "method": "linear_velocity_vector",
    }


def check_route_deviation(current_lat, current_lon, max_dev_km=1.5):
    if not PLANNED_ROUTE:
        return False, 0.0
    distances = [calculate_distance(current_lat, current_lon, point[0], point[1]) for point in PLANNED_ROUTE]
    distance = min(distances)
    return distance > max_dev_km, distance


def new_row(table, values):
    row = dict(values)
    row["id"] = max((item.get("id", 0) for item in STORE[table]), default=0) + 1
    row.setdefault("created_at", datetime.now().isoformat())
    STORE[table].append(row)
    return row


def upsert_emergency(values):
    existing = next((row for row in STORE["emergency_events"] if row.get("event_id") == values.get("event_id")), None)
    values = dict(values)
    values.setdefault("sync_status", "SYNCED")
    if existing:
        existing.update(values)
        return existing
    return new_row("emergency_events", values)


class SafetyHandler(BaseHTTPRequestHandler):
    server_version = "AURA/1.0"

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10 * 1024 * 1024:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Expected a JSON object")
        return parsed

    def do_OPTIONS(self):
        self.send_json({}, 204)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/route":
                return self.send_json({"status": "success", "route": PLANNED_ROUTE})
            if path == "/api/status":
                return self.send_json({"status": "online", "service": "AURA Women Safety Backend", "timestamp": datetime.now().isoformat(), "database": {"emergency_events": len(STORE["emergency_events"]), "audio_recordings": len(STORE["audio_evidence"]), "geofence_events": len(STORE["geofence_events"])}})
            if path == "/api/emergency/events":
                rows = sorted(STORE["emergency_events"], key=lambda row: row.get("id", 0), reverse=True)[:50]
                return self.send_json({"status": "success", "count": len(rows), "events": rows})
            if path == "/api/recordings":
                files = []
                for media_type, directory in (("audio", BACKEND_AUDIO_DIR), ("video", BACKEND_VIDEO_DIR)):
                    for name in os.listdir(directory):
                        full = os.path.join(directory, name)
                        if os.path.isfile(full):
                            stat = os.stat(full)
                            files.append({"type": media_type, "filename": name, "size_bytes": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat()})
                return self.send_json({"status": "success", "count": len(files), "recordings": files})
            return self.send_json({"status": "error", "message": "Not found"}, 404)
        except Exception as exc:
            return self.send_json({"status": "error", "message": str(exc)}, 500)

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/emergency/upload_audio":
                return self.upload_audio()
            data = self.read_json()
            if path == "/api/route":
                route = data.get("route", [])
                if not isinstance(route, list):
                    return self.send_json({"status": "error", "message": "Expected list of [lat, lon] waypoints"}, 400)
                global PLANNED_ROUTE
                PLANNED_ROUTE = route
                with open(ROUTE_FILE, "w", encoding="utf-8") as f:
                    json.dump(route, f, indent=2)
                return self.send_json({"status": "success", "message": f"Planned route updated with {len(route)} waypoints.", "route": route})
            if path == "/update_location":
                lat, lon, speed = float(data.get("lat", 0)), float(data.get("lon", 0)), float(data.get("speed", 0))
                deviated, distance = check_route_deviation(lat, lon)
                save_location(lat, lon, speed, deviated)
                if deviated:
                    print(f"[BACKEND ALERT] User deviated from route by {distance:.2f} km!")
                return self.send_json({"status": "success", "route_deviated": deviated, "deviation_distance_km": round(distance, 2), "prediction": predict_next_location()})
            if path == "/fall_detected":
                lat, lon = data.get("lat", 0.0), data.get("lon", 0.0)
                event_id = f"FALL_{int(time.time())}"
                upsert_emergency({"event_id": event_id, "timestamp": datetime.now().isoformat(), "threat_type": "PHONE_FALL_DETECTED", "latitude": lat, "longitude": lon, "maps_link": f"https://maps.google.com/?q={lat},{lon}"})
                save_store(STORE)
                print(f"[BACKEND EMERGENCY] Phone Fall Detected! (Lat: {lat}, Lon: {lon})")
                return self.send_json({"status": "Fall Alert Received", "action": "Triggering SOS", "event_id": event_id})
            if path == "/battery_status":
                level, source = data.get("level", 100), data.get("source", "Unknown Device")
                new_row("battery_logs", {"timestamp": datetime.now().isoformat(), "level": level, "source": source})
                save_store(STORE)
                return self.send_json({"status": "Battery Logged", "level": level})
            if path == "/api/emergency/event":
                event_id = data.get("event_id", f"SOS_{int(time.time() * 1000)}")
                lat, lon = float(data.get("latitude", 0)), float(data.get("longitude", 0))
                upsert_emergency({"event_id": event_id, "timestamp": data.get("timestamp", datetime.now().isoformat()), "threat_type": data.get("threat_type", "EMERGENCY_TRIGGER"), "latitude": lat, "longitude": lon, "maps_link": data.get("maps_link", f"https://maps.google.com/?q={lat},{lon}"), "audio_filename": data.get("audio_filename", ""), "contacts_notified": json.dumps(data.get("contacts_notified", [])), "call_status": data.get("call_status", "UNKNOWN"), "sms_status": data.get("sms_status", "UNKNOWN"), "sync_status": "SYNCED"})
                save_store(STORE)
                print(f"[BACKEND] Saved Emergency Event: {event_id}")
                return self.send_json({"status": "success", "event_id": event_id, "saved": True}, 201)
            if path == "/api/geofence/event":
                new_row("geofence_events", {"timestamp": data.get("timestamp", datetime.now().isoformat()), "zone_name": data.get("zone_name", "Unknown Zone"), "event_type": data.get("event_type", "BREACH"), "latitude": float(data.get("latitude", 0)), "longitude": float(data.get("longitude", 0))})
                save_store(STORE)
                return self.send_json({"status": "success", "logged": True}, 201)
            if path == "/api/sync":
                events = data.get("emergency_events", [])
                geofences = data.get("geofence_events", [])
                for event in events:
                    upsert_emergency({"event_id": event.get("event_id"), "timestamp": event.get("timestamp"), "threat_type": event.get("threat_type"), "latitude": event.get("latitude"), "longitude": event.get("longitude"), "maps_link": event.get("maps_link"), "audio_filename": event.get("audio_filename", ""), "contacts_notified": json.dumps(event.get("contacts_notified", [])), "call_status": event.get("call_status", "UNKNOWN"), "sms_status": event.get("sms_status", "UNKNOWN"), "sync_status": "SYNCED"})
                for item in geofences:
                    new_row("geofence_events", {"timestamp": item.get("timestamp"), "zone_name": item.get("zone_name"), "event_type": item.get("event_type"), "latitude": item.get("latitude"), "longitude": item.get("longitude")})
                save_store(STORE)
                return self.send_json({"status": "success", "synced_events": len(events), "synced_geofence": len(geofences)})
            if path == "/api/db/reset":
                for table in TABLES:
                    STORE[table] = []
                save_store(STORE)
                return self.send_json({"status": "success", "message": "All backend JSON records have been reset."})
            return self.send_json({"status": "error", "message": "Not found"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({"status": "error", "message": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"status": "error", "message": str(exc)}, 500)

    def upload_audio(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return self.send_json({"status": "error", "message": "Expected multipart/form-data"}, 400)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100 * 1024 * 1024:
            return self.send_json({"status": "error", "message": "Invalid upload size"}, 400)
        raw = self.rfile.read(length)
        from email.parser import BytesParser
        from email.policy import default
        envelope = b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        form = BytesParser(policy=default).parsebytes(envelope)
        upload = None
        event_id = "UNKNOWN_EVENT"
        for part in form.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "audio":
                upload = (part.get_filename(), part.get_payload(decode=True) or b"")
            elif name == "event_id":
                event_id = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if not upload or not upload[0]:
            return self.send_json({"status": "error", "message": "No audio file part in request"}, 400)
        filename = os.path.basename(upload[0].replace("\\", "/"))
        if not filename:
            return self.send_json({"status": "error", "message": "No selected file"}, 400)
        destination = os.path.join(BACKEND_AUDIO_DIR, filename)
        with open(destination, "wb") as f:
            f.write(upload[1])
        size = len(upload[1])
        STORE["audio_evidence"].append({"id": len(STORE["audio_evidence"]) + 1, "event_id": event_id, "filename": filename, "filepath": destination, "file_size_bytes": size, "uploaded_at": datetime.now().isoformat()})
        for event in STORE["emergency_events"]:
            if event.get("event_id") == event_id:
                event["audio_filename"] = filename
        save_store(STORE)
        return self.send_json({"status": "success", "filename": filename, "size": size}, 201)

    def do_DELETE(self):
        path = unquote(urlsplit(self.path).path)
        try:
            match = re.fullmatch(r"/api/recordings/(audio|video)/(.+)", path)
            if match:
                media_type, filename = match.groups()
                filename = os.path.basename(filename)
                directory = BACKEND_AUDIO_DIR if media_type == "audio" else BACKEND_VIDEO_DIR
                target = os.path.join(directory, filename)
                deleted = os.path.isfile(target)
                if deleted:
                    os.remove(target)
                if media_type == "audio":
                    STORE["audio_evidence"] = [row for row in STORE["audio_evidence"] if row.get("filename") != filename]
                    for event in STORE["emergency_events"]:
                        if event.get("audio_filename") == filename:
                            event["audio_filename"] = ""
                    save_store(STORE)
                return self.send_json({"status": "success", "media_type": media_type, "filename": filename, "deleted": deleted})
            if path == "/api/recordings/all":
                query = parse_qs(urlsplit(self.path).query)
                media_type = query.get("type", ["all"])[0].lower()
                if media_type not in ("audio", "video", "all"):
                    return self.send_json({"status": "error", "message": "Unsupported media type"}, 400)
                directories = [BACKEND_AUDIO_DIR, BACKEND_VIDEO_DIR] if media_type == "all" else [BACKEND_AUDIO_DIR if media_type == "audio" else BACKEND_VIDEO_DIR]
                count = 0
                for directory in directories:
                    for name in os.listdir(directory):
                        target = os.path.join(directory, name)
                        if os.path.isfile(target):
                            os.remove(target)
                            count += 1
                if media_type in ("audio", "all"):
                    STORE["audio_evidence"] = []
                    for event in STORE["emergency_events"]:
                        event["audio_filename"] = ""
                    save_store(STORE)
                return self.send_json({"status": "success", "deleted_count": count, "media_type": media_type})
            return self.send_json({"status": "error", "message": "Not found"}, 404)
        except Exception as exc:
            return self.send_json({"status": "error", "message": str(exc)}, 500)

    def log_message(self, fmt, *args):
        print("[HTTP] " + (fmt % args))


if __name__ == "__main__":
    print("Safety Backend Engine running with JSON storage on port 5000...")
    HTTPServer(("0.0.0.0", 5000), SafetyHandler).serve_forever()
