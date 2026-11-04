import os
import json
import time
import math
import random

DATA_DIR = "data"
ZONES_FILE = os.path.join(DATA_DIR, "safe_zones.json")


class GeofenceManager:
    """
    Stateful Safe-Location & Geofencing Engine.
    Tracks entry and exit transitions for each predefined zone.
    """
    def __init__(self, haversine_fn):
        self.haversine_distance = haversine_fn
        self.safe_zones = []
        self._zone_states = {}  # {zone_index: "INSIDE" | "OUTSIDE"}
        os.makedirs(DATA_DIR, exist_ok=True)
        self.load_zones()

    def load_zones(self):
        if os.path.exists(ZONES_FILE):
            try:
                with open(ZONES_FILE, "r", encoding="utf-8") as f:
                    self.safe_zones = json.load(f)
                return
            except Exception:
                pass
        # Dynamic safe zones initialized empty - configured by user in Settings or loaded from disk
        self.safe_zones = []

    def save_zones(self):
        try:
            with open(ZONES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.safe_zones, f, indent=2)
        except Exception as exc:
            print(f"[GeofenceManager] Save warning: {exc}")

    def add_safe_zone(self, name: str, latitude: float, longitude: float, radius_km: float = 0.5):
        self.safe_zones.append({
            "name": name,
            "latitude": float(latitude),
            "longitude": float(longitude),
            "radius_km": float(radius_km)
        })
        self.save_zones()

    def remove_safe_zone(self, index: int):
        if 0 <= index < len(self.safe_zones):
            removed = self.safe_zones.pop(index)
            self._zone_states.pop(index, None)
            self.save_zones()
            return removed
        return None

    def evaluate_transitions(self, current_lat: float, current_lon: float) -> list:
        """
        Evaluates current coordinates against all safe zones.
        Returns list of transition events (e.g. EXIT, ENTRY).
        """
        transitions = []
        for idx, zone in enumerate(self.safe_zones):
            dist_km = self.haversine_distance(
                current_lat, current_lon,
                zone["latitude"], zone["longitude"]
            )
            is_inside = dist_km <= zone["radius_km"]
            prev_state = self._zone_states.get(idx)

            if prev_state is None:
                # Initialize starting state
                self._zone_states[idx] = "INSIDE" if is_inside else "OUTSIDE"
            elif prev_state == "INSIDE" and not is_inside:
                # Trigger EXIT
                self._zone_states[idx] = "OUTSIDE"
                transitions.append({
                    "event": "EXIT",
                    "zone_name": zone["name"],
                    "latitude": current_lat,
                    "longitude": current_lon,
                    "distance_km": round(dist_km, 3),
                    "radius_km": zone["radius_km"],
                    "message": f"⚠️ Geofence Breach: Left safe zone '{zone['name']}'"
                })
            elif prev_state == "OUTSIDE" and is_inside:
                # Trigger ENTRY
                self._zone_states[idx] = "INSIDE"
                transitions.append({
                    "event": "ENTRY",
                    "zone_name": zone["name"],
                    "latitude": current_lat,
                    "longitude": current_lon,
                    "distance_km": round(dist_km, 3),
                    "radius_km": zone["radius_km"],
                    "message": f"✅ Entered safe zone '{zone['name']}'"
                })

        return transitions

    def is_outside_all_zones(self, current_lat: float, current_lon: float) -> bool:
        """Returns True if user is outside every safe zone. False if inside at least one zone or no zones set."""
        if not self.safe_zones:
            return False
        for zone in self.safe_zones:
            dist_km = self.haversine_distance(
                current_lat, current_lon,
                zone["latitude"], zone["longitude"]
            )
            if dist_km <= zone["radius_km"]:
                return False
        return True


class OfflineLocationEngine:
    def __init__(self, default_lat=None, default_lon=None, auto_simulate=False):
        recovered_lat, recovered_lon = self._recover_last_known_coords()
        self.current_lat = default_lat if default_lat is not None else recovered_lat
        self.current_lon = default_lon if default_lon is not None else recovered_lon
        self.last_timestamp = time.time()
        self.location_history = []
        self.auto_simulate = auto_simulate

        # Stateful Geofence Manager
        self.geofence_manager = GeofenceManager(self.haversine_distance)

    def _recover_last_known_coords(self):
        """Dynamically retrieves the most recent real coordinate from history logs without hardcoding."""
        import map_config
        hist_file = os.path.join(DATA_DIR, "location_history.json")
        if os.path.exists(hist_file):
            try:
                with open(hist_file, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                    if isinstance(entries, list):
                        for last in reversed(entries):
                            lat = float(last.get("latitude", 0.0))
                            lon = float(last.get("longitude", 0.0))
                            if lat != 0.0 and lon != 0.0:
                                return lat, lon
            except Exception:
                pass
        return map_config.CENTER_LAT, map_config.CENTER_LON

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculates distance in KM using pure Haversine Math."""
        R = 6371.0  # Earth radius in KM
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def get_current_location(self):
        """Returns current GPS telemetry or last known valid GPS."""
        current_time = time.time()

        if self.auto_simulate:
            self.current_lat += random.uniform(-0.0003, 0.0003)
            self.current_lon += random.uniform(-0.0003, 0.0003)

        location_data = {
            "latitude": round(self.current_lat, 6),
            "longitude": round(self.current_lon, 6),
            "timestamp": current_time,
            "maps_url": f"https://maps.google.com/?q={round(self.current_lat, 6)},{round(self.current_lon, 6)}"
        }
        if getattr(self, "gps_accuracy_m", None) is not None:
            location_data["accuracy_m"] = self.gps_accuracy_m

        self.location_history.append(location_data)
        if len(self.location_history) > 100:
            self.location_history.pop(0)

        self.last_timestamp = current_time
        return location_data

    def calculate_speed(self, prev_loc, curr_loc):
        """Computes instantaneous velocity in km/h."""
        if not prev_loc or not curr_loc:
            return 0.0
        dist_km = self.haversine_distance(
            prev_loc["latitude"], prev_loc["longitude"],
            curr_loc["latitude"], curr_loc["longitude"]
        )
        time_hours = (curr_loc.get("timestamp", time.time()) - prev_loc.get("timestamp", time.time())) / 3600.0
        if time_hours <= 0:
            return 0.0
        return round(dist_km / time_hours, 2)
