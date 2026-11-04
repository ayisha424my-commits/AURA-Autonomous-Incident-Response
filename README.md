# AURA Offline Safety System

AURA is a Python desktop safety application with a Tkinter dashboard. It includes offline Vosk voice recognition, GPS and map monitoring, Haversine distance calculations, trajectory prediction, emergency evidence recording, and Android phone dispatch over USB ADB.

## Run

Install the Python dependencies:

```powershell
pip install -r requirements.txt
```

Start the desktop application:

```powershell
python main.py
```

The dashboard runs its monitoring engines in the background. Connect and authorize an Android phone over USB for ADB dispatch. SMS still requires cellular service and an SMS-enabled SIM. GPS fixes require a phone-side location stream forwarded to port 8082.

The optional local JSON HTTP service can be started separately with:

```powershell
python app.py
```

It uses Python's standard-library HTTP server and stores its data in JSON files under `data/`.

## Local data

- `data/user_profile.json` — profile and emergency contacts.
- `data/location_history.json` — recorded location points used by the predictor.
- `data/current_trip.json` — selected trip destination.
- `data/safe_zones.json` — configured geofence zones.
- `data/offline_events_vault.json` — locally queued safety events and evidence records.
- `data/backend_store.json` — optional local JSON service records.
- `data/vault/audio/` — recorded audio evidence (`.wav`).
- `data/vault/video/` — recorded video evidence (`.mp4` or `.avi`).

Planned map routes and their coordinates are defined in `map_config.py`.

## Main modules

- `main_controller.py` — coordinates safety monitoring, prediction, voice, and dispatch.
- `gui_module.py` — Tkinter dashboard, telemetry, and map UI.
- `map_simulator.py` — standalone interactive map simulator.
- `location_module.py` — location calculations and geofencing.
- `prediction_module.py` — trajectory prediction and location history.
- `anomaly_engine.py` — speed and route anomaly detection.
- `gps_bridge_module.py` — receives phone GPS fixes over TCP port 8082.
- `fall_detector.py` — phone sensor stream and fall detection.
- `acoustic_module.py` — Vosk offline voice recognition and evidence recording.
- `app.py` — optional standard-library HTTP service with JSON storage.
- `sync_manager.py` — local emergency event and evidence queue.

## Phone GPS and sensor streams

Enable USB debugging on the phone, connect it, and authorize the computer. The AURA controller configures ADB forwarding for GPS port 8082. The phone-side service must send JSON fixes such as:

```json
{"latitude": 11.49, "longitude": 79.76, "accuracy": 8}
```

The fall detector listens for phone sensor data on port 8080.

## Notes

- User profile, route history, trip settings, and queued events are stored as local files; the running desktop app does not use a relational database.
- Outdated reset and integration-test utilities have been removed.
- Emergency SMS and calls use the connected Android phone through ADB. Cellular service is required for SMS and calls; internet access is not required for cellular delivery.
