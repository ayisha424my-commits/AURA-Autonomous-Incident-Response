import time
import math
import numpy as np
from location_module import OfflineLocationEngine

class TrajectoryAnomalyEngine:
    def __init__(self, speed_threshold_kmh=25.0, deviation_threshold_km=0.1):
        self.max_speed_threshold = float(speed_threshold_kmh)
        self.max_deviation_threshold = float(deviation_threshold_km)
        self.gps_engine = OfflineLocationEngine()
        self.trajectory_history = []
        self.planned_route = []
        self.destination = None  # {"name": str, "latitude": float, "longitude": float}

        # Cooldown tracking to prevent repeated alert loops for same ongoing event
        self.cooldown_period_sec = 60.0
        self.last_speed_alert_time = 0.0
        self.last_deviation_alert_time = 0.0
        self.min_speed_sample_distance_m = 20.0
        self.min_route_sample_distance_m = 15.0
        self.route_deviation_required_samples = 3
        self.route_deviation_samples = 0

    @property
    def speed_threshold_kmh(self):
        return self.max_speed_threshold

    @speed_threshold_kmh.setter
    def speed_threshold_kmh(self, val):
        self.max_speed_threshold = float(val)

    def set_speed_threshold(self, speed_kmh: float):
        self.max_speed_threshold = float(speed_kmh)

    def set_destination(self, name: str, latitude: float, longitude: float):
        self.destination = {
            "name": name,
            "latitude": float(latitude),
            "longitude": float(longitude)
        }

    def get_destination(self):
        return self.destination

    def clear_destination(self):
        self.destination = None

    def set_planned_route(self, route_points):
        self.planned_route = list(route_points or [])
        self.route_deviation_samples = 0

    def acknowledge_safe(self, threat_type: str = "ALL", cooldown_seconds: float = None):
        """Called when user confirms they are safe to reset / apply cooldown."""
        now = time.time()
        if cooldown_seconds is not None:
            self.cooldown_period_sec = float(cooldown_seconds)
        if "SPEED" in threat_type.upper() or threat_type == "ALL":
            self.last_speed_alert_time = now
        if "ROUTE" in threat_type.upper() or "DEVIATION" in threat_type.upper() or threat_type == "ALL":
            self.last_deviation_alert_time = now
            self.route_deviation_samples = 0
        self._prev_dest_dist = None

    def check_for_threats(self, speed_kmh=0.0, current_lat=0.0, current_lon=0.0, predicted_point=None):
        """
        Direct threat evaluation checking speed threshold and route deviation.
        Returns: (is_threat: bool, reason_message: str)
        """
        now = time.time()
        # 1. Speed Check
        if speed_kmh > self.max_speed_threshold:
            if (now - self.last_speed_alert_time) > self.cooldown_period_sec:
                self.last_speed_alert_time = now
                return True, f"⚠️ ARE YOU SAFE? SPEED INCREASE DETECTED: {speed_kmh:.1f} km/h (Limit: {self.max_speed_threshold:.1f} km/h)"
            else:
                return False, "COOLDOWN_ACTIVE"

        # 2. Planned Route Corridor Check (if corridor is set)
        if self.planned_route and current_lat and current_lon:
            min_dist_km = min(
                self.gps_engine.haversine_distance(current_lat, current_lon, pt[0], pt[1])
                for pt in self.planned_route
            )
            if min_dist_km > self.max_deviation_threshold:
                if (now - self.last_deviation_alert_time) > self.cooldown_period_sec:
                    self.last_deviation_alert_time = now
                    return True, f"⚠️ ARE YOU SAFE? ROUTE DEVIATION: {round(min_dist_km * 1000, 1)}m off planned corridor!"
                else:
                    return False, "COOLDOWN_ACTIVE"

        # 3. Destination Retreat Check (moving away from destination)
        if self.destination and current_lat and current_lon:
            d_lat = self.destination["latitude"]
            d_lon = self.destination["longitude"]
            dist_km = self.gps_engine.haversine_distance(current_lat, current_lon, d_lat, d_lon)
            prev_d = getattr(self, "_prev_dest_dist", None)
            self._prev_dest_dist = dist_km
            if prev_d is not None and (dist_km - prev_d) > self.max_deviation_threshold and dist_km > 0.5:
                if (now - self.last_deviation_alert_time) > self.cooldown_period_sec:
                    self.last_deviation_alert_time = now
                    return True, f"⚠️ ARE YOU SAFE? ROUTE DEVIATION FROM '{self.destination['name']}' ({dist_km:.2f} km)"
                else:
                    return False, "COOLDOWN_ACTIVE"

        return False, "NORMAL"

    def predict_next_location(self):
        if len(self.trajectory_history) < 2:
            return None

        recent_pts = self.trajectory_history[-3:]
        lats = [p["latitude"] for p in recent_pts]
        lons = [p["longitude"] for p in recent_pts]

        lat_diff = np.mean(np.diff(lats))
        lon_diff = np.mean(np.diff(lons))

        last_pt = recent_pts[-1]
        predicted_lat = round(last_pt["latitude"] + lat_diff, 6)
        predicted_lon = round(last_pt["longitude"] + lon_diff, 6)

        return {"latitude": predicted_lat, "longitude": predicted_lon, "confidence": 0.85, "prediction_mode": "LINEAR_EXTRAPOLATION"}

    @staticmethod
    def _point_to_polyline_distance_km(latitude, longitude, route):
        """Measure perpendicular distance to route segments in local coordinates."""
        if not route:
            return float("inf")
        lat_scale = 110.54
        lon_scale = 111.32 * math.cos(math.radians(latitude))
        projected = [
            ((float(point[1]) - longitude) * lon_scale,
             (float(point[0]) - latitude) * lat_scale)
            for point in route
        ]
        if len(projected) == 1:
            return math.hypot(*projected[0])
        minimum = float("inf")
        for start, end in zip(projected, projected[1:]):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                nearest = start
            else:
                factor = max(0.0, min(1.0, -(start[0] * dx + start[1] * dy) / length_sq))
                nearest = (start[0] + factor * dx, start[1] + factor * dy)
            minimum = min(minimum, math.hypot(*nearest))
        return minimum

    def evaluate_telemetry(self, current_loc, previous_loc, external_predicted_loc=None, speed_override=None):
        """
        Evaluates speed and route deviation anomalies.
        Returns: (is_threat: bool, threat_type: str, message: str)
        """
        now = time.time()
        self.trajectory_history.append(current_loc)
        if len(self.trajectory_history) > 200:
            self.trajectory_history.pop(0)

        # Evaluate speed and perpendicular polyline deviation independently;
        # combine both causes when they are active on this telemetry sample.
        speed = (
            float(speed_override)
            if speed_override is not None
            else self.gps_engine.calculate_speed(previous_loc, current_loc)
        )
        displacement_km = 0.0
        if previous_loc and current_loc:
            displacement_km = self.gps_engine.haversine_distance(
                previous_loc["latitude"], previous_loc["longitude"],
                current_loc["latitude"], current_loc["longitude"],
            )
        accuracy_m = current_loc.get("accuracy_m")
        try:
            fix_quality_ok = accuracy_m is None or float(accuracy_m) <= 50.0
        except (TypeError, ValueError):
            fix_quality_ok = False
        speed_active = (
            fix_quality_ok
            and
            speed > 25.0
            and now - self.last_speed_alert_time > self.cooldown_period_sec
        )
        if speed_active:
            self.last_speed_alert_time = now

        route_distance_km = 0.0
        route_outside_buffer = False
        route_active = False
        if self.planned_route and current_loc.get("latitude") and current_loc.get("longitude"):
            route_distance_km = self._point_to_polyline_distance_km(
                current_loc["latitude"], current_loc["longitude"], self.planned_route
            )
            route_outside_buffer = (
                fix_quality_ok and route_distance_km > self.max_deviation_threshold
            )
            if route_outside_buffer and displacement_km * 1000.0 >= self.min_route_sample_distance_m:
                self.route_deviation_samples += 1
            else:
                # Require three meaningful consecutive fixes outside the
                # strict 100 m buffer. Stationary drift/noise breaks the run.
                self.route_deviation_samples = 0
            route_active = (
                self.route_deviation_samples >= self.route_deviation_required_samples
                and now - self.last_deviation_alert_time > self.cooldown_period_sec
            )
            if route_active:
                self.last_deviation_alert_time = now

        if speed_active and route_active:
            return (True, "SPEED_AND_ROUTE_DEVIATION",
                    f"CRITICAL: Speed Anomaly & Route Deviation "
                    f"({speed:.1f} km/h; {route_distance_km * 1000:.0f} m off route)")
        if route_active:
            return (True, "ROUTE_DEVIATION",
                    f"Route Deviation Detected: {route_distance_km * 1000:.0f} m off planned route")
        if speed_active:
            return (True, "HIGH_SPEED",
                    f"Speed Anomaly Detected: {speed:.1f} km/h "
                    "(Limit: 25.0 km/h)")

        return False, "NORMAL", f"✅ Telemetry Normal. Speed: {speed} km/h"


# Module-level alias for backward compatibility and test suite
AnomalyEngine = TrajectoryAnomalyEngine
