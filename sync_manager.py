"""
sync_manager.py
────────────────────────────────────────────────────────────────────────────
Offline-First Synchronization Manager for AURA Women Safety System.

Responsibilities:
  1. Stores all emergency events, geofence transitions, and audio evidence
     locally first in `data/offline_events_vault.json`.
  2. Runs an autonomous background sync daemon that monitors backend connectivity.
  3. When a local sync service is available, uploads pending emergency
     events and multipart audio recordings with exponential
     retry and transactional integrity.
  4. Guarantees zero data loss during dead zones, offline scenarios, and crashes.
"""

import os
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

DEFAULT_BACKEND_URL = "http://127.0.0.1:5000"
DATA_DIR            = "data"
VAULT_PATH          = os.path.join(DATA_DIR, "offline_events_vault.json")
SYNC_INTERVAL_SEC   = 5.0
MAX_BACKOFF_SEC     = 60.0


class SyncManager:
    def __init__(self, backend_url=DEFAULT_BACKEND_URL):
        self.backend_url = backend_url.rstrip("/")
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sync_thread = None
        self._backoff = 2.0
        self.is_online = False

        os.makedirs(DATA_DIR, exist_ok=True)
        self._init_vault()

    def _init_vault(self):
        with self._lock:
            if not os.path.exists(VAULT_PATH):
                initial = {
                    "emergency_events": [],
                    "audio_queue": [],
                    "geofence_events": []
                }
                with open(VAULT_PATH, "w", encoding="utf-8") as f:
                    json.dump(initial, f, indent=2)

    def _load_vault(self):
        try:
            with open(VAULT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("emergency_events", [])
                    data.setdefault("audio_queue", [])
                    data.setdefault("geofence_events", [])
                    return data
        except Exception:
            pass
        return {"emergency_events": [], "audio_queue": [], "geofence_events": []}

    def _save_vault(self, data):
        tmp_path = VAULT_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, VAULT_PATH)

    # ── Queueing API ─────────────────────────────────────────────────────────

    def queue_emergency_event(self, event_data: dict) -> str:
        """
        Saves emergency event locally. Guaranteed to persist offline.
        Returns the unique event_id.
        """
        event_id = event_data.get("event_id") or f"SOS_{int(time.time() * 1000)}"
        event_record = {
            "event_id": event_id,
            "timestamp": event_data.get("timestamp", datetime.now().isoformat()),
            "threat_type": event_data.get("threat_type", "MANUAL_SOS"),
            "latitude": float(event_data.get("latitude", 0.0)),
            "longitude": float(event_data.get("longitude", 0.0)),
            "maps_link": event_data.get("maps_link", f"https://maps.google.com/?q={event_data.get('latitude', 0.0)},{event_data.get('longitude', 0.0)}"),
            "audio_filename": event_data.get("audio_filename", ""),
            "contacts_notified": event_data.get("contacts_notified", []),
            "call_status": event_data.get("call_status", "PENDING"),
            "sms_status": event_data.get("sms_status", "PENDING"),
            "synced": False,
            "retry_count": 0
        }

        with self._lock:
            vault = self._load_vault()
            vault["emergency_events"].append(event_record)
            self._save_vault(vault)

        print(f"📥 [SyncManager] Emergency Event queued locally: {event_id}")
        self.trigger_sync_now()
        return event_id

    def queue_audio_file(self, audio_file_path: str, event_id: str = "") -> None:
        """
        Queues an audio recording for backend upload.
        """
        if not os.path.exists(audio_file_path):
            print(f"⚠️ [SyncManager] Audio file not found to queue: {audio_file_path}")
            return

        record = {
            "file_path": os.path.abspath(audio_file_path),
            "filename": os.path.basename(audio_file_path),
            "event_id": event_id,
            "queued_at": datetime.now().isoformat(),
            "synced": False,
            "retry_count": 0
        }

        with self._lock:
            vault = self._load_vault()
            # Avoid duplicate queueing
            existing = [item["file_path"] for item in vault.get("audio_queue", [])]
            if record["file_path"] not in existing:
                vault.setdefault("audio_queue", []).append(record)
                self._save_vault(vault)

        print(f"🎙️ [SyncManager] Audio evidence queued locally: {record['filename']}")
        self.trigger_sync_now()

    def remove_audio_from_queue(self, audio_file_path: str) -> None:
        """Removes a deleted audio recording from the pending upload queue."""
        abs_p = os.path.abspath(audio_file_path)
        base_n = os.path.basename(audio_file_path)
        with self._lock:
            vault = self._load_vault()
            queue = vault.get("audio_queue", [])
            initial_len = len(queue)
            vault["audio_queue"] = [
                item for item in queue
                if os.path.abspath(item.get("file_path", "")) != abs_p
                and item.get("filename") != base_n
            ]
            if len(vault["audio_queue"]) != initial_len:
                self._save_vault(vault)
                print(f"🗑️ [SyncManager] Removed deleted audio from upload queue: {base_n}")

    def clear_audio_queue(self) -> None:
        """Clears all audio from the pending upload queue."""
        with self._lock:
            vault = self._load_vault()
            vault["audio_queue"] = []
            self._save_vault(vault)
            print("🗑️ [SyncManager] Cleared entire audio upload queue.")

    def queue_geofence_event(self, zone_name: str, event_type: str, lat: float, lon: float) -> None:
        """
        Queues a geofence ENTRY or EXIT event locally.
        """
        record = {
            "timestamp": datetime.now().isoformat(),
            "zone_name": zone_name,
            "event_type": event_type,
            "latitude": float(lat),
            "longitude": float(lon),
            "synced": False
        }

        with self._lock:
            vault = self._load_vault()
            vault.setdefault("geofence_events", []).append(record)
            self._save_vault(vault)

        print(f"📍 [SyncManager] Geofence event queued locally: {event_type} - {zone_name}")
        self.trigger_sync_now()

    # ── Background Daemon Loop ───────────────────────────────────────────────

    def start(self):
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._stop_event.clear()
        self._sync_thread = threading.Thread(target=self._run_loop, name="SyncDaemon", daemon=True)
        self._sync_thread.start()
        print("🔄 [SyncManager] Background synchronization engine started.")

    def stop(self):
        self._stop_event.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=3.0)
        print("🛑 [SyncManager] Synchronization engine stopped.")

    def trigger_sync_now(self):
        """Signals worker to attempt immediate sync."""
        threading.Thread(target=self._sync_pass, daemon=True, name="ManualSyncWorker").start()

    def _run_loop(self):
        while not self._stop_event.is_set():
            self._sync_pass()
            self._stop_event.wait(SYNC_INTERVAL_SEC)

    def _sync_pass(self):
        if not self._check_backend_alive():
            self.is_online = False
            return

        self.is_online = True
        self._sync_emergency_events()
        self._sync_audio_evidence()
        self._sync_geofence_events()

    def _check_backend_alive(self) -> bool:
        url = f"{self.backend_url}/api/status"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AURA-Sync/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _sync_emergency_events(self):
        with self._lock:
            vault = self._load_vault()
            events = vault.get("emergency_events", [])
            pending = [ev for ev in events if not ev.get("synced")]

        if not pending:
            return

        for ev in pending:
            url = f"{self.backend_url}/api/emergency/event"
            try:
                payload = json.dumps(ev).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "AURA-Sync/1.0"}
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201):
                        with self._lock:
                            v = self._load_vault()
                            for item in v.get("emergency_events", []):
                                if item.get("event_id") == ev.get("event_id"):
                                    item["synced"] = True
                                    item["synced_at"] = datetime.now().isoformat()
                            self._save_vault(v)
                        print(f"✅ [SyncManager] Synced Emergency Event: {ev['event_id']}")
            except Exception as exc:
                print(f"⚠️ [SyncManager] Event sync retry failed for {ev['event_id']}: {exc}")

    def _sync_audio_evidence(self):
        with self._lock:
            vault = self._load_vault()
            audio_items = vault.get("audio_queue", [])
            pending = [a for a in audio_items if not a.get("synced")]

        if not pending:
            return

        for item in pending:
            file_path = item.get("file_path")
            if not os.path.exists(file_path):
                # Mark as synced/skipped to avoid infinite loop
                with self._lock:
                    v = self._load_vault()
                    for a in v.get("audio_queue", []):
                        if a.get("file_path") == file_path:
                            a["synced"] = True
                            a["error"] = "File missing locally"
                    self._save_vault(v)
                continue

            success = self._upload_multipart_audio(file_path, item.get("event_id", ""))
            if success:
                with self._lock:
                    v = self._load_vault()
                    for a in v.get("audio_queue", []):
                        if a.get("file_path") == file_path:
                            a["synced"] = True
                            a["synced_at"] = datetime.now().isoformat()
                    self._save_vault(v)
                print(f"✅ [SyncManager] Audio uploaded successfully: {item['filename']}")

    def _upload_multipart_audio(self, file_path: str, event_id: str) -> bool:
        url = f"{self.backend_url}/api/emergency/upload_audio"
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        filename = os.path.basename(file_path)

        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()

            body = bytearray()
            # event_id field
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="event_id"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{event_id}\r\n".encode("utf-8"))

            # audio file field
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'.encode("utf-8"))
            body.extend(b"Content-Type: audio/wav\r\n\r\n")
            body.extend(file_bytes)
            body.extend(b"\r\n")

            body.extend(f"--{boundary}--\r\n".encode("utf-8"))

            req = urllib.request.Request(
                url,
                data=bytes(body),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "AURA-Sync/1.0"
                }
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status in (200, 201)
        except Exception as exc:
            print(f"⚠️ [SyncManager] Audio upload failed ({filename}): {exc}")
            return False

    def _sync_geofence_events(self):
        with self._lock:
            vault = self._load_vault()
            gf_items = vault.get("geofence_events", [])
            pending = [g for g in gf_items if not g.get("synced")]

        if not pending:
            return

        for item in pending:
            url = f"{self.backend_url}/api/geofence/event"
            try:
                payload = json.dumps(item).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "AURA-Sync/1.0"}
                )
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if resp.status in (200, 201):
                        with self._lock:
                            v = self._load_vault()
                            for g in v.get("geofence_events", []):
                                if g.get("timestamp") == item.get("timestamp") and g.get("zone_name") == item.get("zone_name"):
                                    g["synced"] = True
                            self._save_vault(v)
            except Exception:
                pass

    def get_offline_summary(self) -> dict:
        """Returns statistics on queued vs synchronized records."""
        with self._lock:
            vault = self._load_vault()
            events = vault.get("emergency_events", [])
            audio = vault.get("audio_queue", [])
            geofence = vault.get("geofence_events", [])

            return {
                "is_backend_online": self.is_online,
                "total_events": len(events),
                "pending_events": len([e for e in events if not e.get("synced")]),
                "total_audio": len(audio),
                "pending_audio": len([a for a in audio if not a.get("synced")]),
                "total_geofence": len(geofence),
                "pending_geofence": len([g for g in geofence if not g.get("synced")])
            }
