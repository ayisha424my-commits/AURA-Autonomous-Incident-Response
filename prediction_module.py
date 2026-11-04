import os
import json
import math
import time
import csv
import numpy as np
from datetime import datetime

try:
    import map_config
except Exception:
    map_config = None

# ── Persistence paths ─────────────────────────────────────────────────────────
_DATA_DIR          = "data"
_HISTORY_FILE      = os.path.join(_DATA_DIR, "location_history.json")
_LOCATION_LOG_CSV  = os.path.join(_DATA_DIR, "location_log.csv")
_MAX_PERSISTED     = 1000   # keep last N entries in the JSON file

# Battery/Storage optimization thresholds
_MIN_SAMPLING_DIST_M = 3.0    # skip recording if moved less than 3m
_MAX_STATIONARY_GAP_S = 30.0   # force record if stationary for > 30s


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Returns distance in metres between two WGS-84 coordinate pairs."""
    R = 6_371_000.0  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _haversine_batch_m(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Vectorised Haversine for consecutive waypoint pairs."""
    R = 6_371_000.0
    lat1, lat2 = np.radians(lats[:-1]), np.radians(lats[1:])
    lon1, lon2 = np.radians(lons[:-1]), np.radians(lons[1:])
    dphi = lat2 - lat1
    dlam = lon2 - lon1
    a = np.sin(dphi / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlam / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))


class LSTMTrajectoryPredictor:
    """
    LSTM & Cluster-Augmented Trajectory Predictor with Practical Fallback.

    Features:
      1. Stateful recency-weighted velocity extrapolation (LSTM sequence approximation).
      2. Location clustering: Mines frequent location clusters/zones from historical logs.
      3. Practical fallback: If history is insufficient (<2 points) or velocity
         extrapolation is noisy, falls back to the nearest location cluster zone centroid.
      4. Battery and storage efficiency: Filters duplicate/jitter coordinates when stationary.
    """

    def __init__(self, sequence_length: int = 10):
        self.sequence_length    = sequence_length
        self._lats: list[float] = []
        self._lons: list[float] = []
        self._timestamps: list[float] = []
        self._cumulative_dist_km: float = 0.0
        self._last_segment_m: float     = 0.0
        self._last_saved_time: float    = 0.0

        # Cluster zones mined from historical data
        self.clusters: list[dict] = []

        os.makedirs(_DATA_DIR, exist_ok=True)
        self._load_history()
        self._mine_clusters_from_history()

    # ── Mining historical clusters for fallback ───────────────────────────────

    def _mine_clusters_from_history(self):
        """Discovers spatial clusters/zones from historical CSV and JSON data."""
        pts = []

        # From CSV
        if os.path.exists(_LOCATION_LOG_CSV):
            try:
                with open(_LOCATION_LOG_CSV, mode="r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        lat = float(row.get("latitude", 0))
                        lon = float(row.get("longitude", 0))
                        if lat and lon:
                            pts.append((lat, lon))
            except Exception:
                pass

        # From JSON history
        if os.path.exists(_HISTORY_FILE):
            try:
                with open(_HISTORY_FILE, "r", encoding="utf-8") as fh:
                    items = json.load(fh)
                    for item in items:
                        lat = float(item.get("latitude", 0))
                        lon = float(item.get("longitude", 0))
                        if lat and lon:
                            pts.append((lat, lon))
            except Exception:
                pass

        if not pts:
            self.clusters = []
            return

        # Simple grid-based centroid clustering (~100m grid)
        bins = {}
        for lat, lon in pts:
            grid_key = (round(lat, 3), round(lon, 3))
            if grid_key not in bins:
                bins[grid_key] = {"lats": [], "lons": []}
            bins[grid_key]["lats"].append(lat)
            bins[grid_key]["lons"].append(lon)

        clusters = []
        for idx, (k, v) in enumerate(bins.items(), start=1):
            if len(v["lats"]) >= 2:
                c_lat = float(np.mean(v["lats"]))
                c_lon = float(np.mean(v["lons"]))
                clusters.append({
                    "name": f"Location Cluster #{idx}",
                    "lat": round(c_lat, 6),
                    "lon": round(c_lon, 6),
                    "count": len(v["lats"])
                })

        clusters.sort(key=lambda c: c["count"], reverse=True)
        self.clusters = clusters[:10]

    # ── History management with battery/storage efficiency ────────────────────

    def update_history(self, lat: float, lon: float, timestamp: float = None) -> bool:
        """
        Pushes a new GPS fix into the buffer with adaptive sampling.
        Returns True if the point was recorded, False if skipped to save battery/storage.
        """
        ts = timestamp if timestamp is not None else time.time()

        if self._lats:
            seg_m = _haversine_m(self._lats[-1], self._lons[-1], lat, lon)
            time_gap = ts - self._last_saved_time

            # Filter stationary jitter to conserve battery & storage
            if seg_m < _MIN_SAMPLING_DIST_M and time_gap < _MAX_STATIONARY_GAP_S:
                return False

            self._last_segment_m = seg_m
            self._cumulative_dist_km += seg_m / 1000.0
        else:
            self._last_segment_m = 0.0

        self._last_saved_time = ts
        self._lats.append(float(lat))
        self._lons.append(float(lon))
        self._timestamps.append(float(ts))

        if len(self._lats) > self.sequence_length:
            self._lats.pop(0)
            self._lons.pop(0)
            self._timestamps.pop(0)

        self._persist_entry(lat, lon, ts)
        return True

    # ── Prediction with practical fallback ────────────────────────────────────

    def predict_next_coordinate(self) -> dict | None:
        """
        Predicts the next location. If sequence extrapolation is unreliable
        or history is insufficient, falls back to the nearest cluster/zone.
        """
        n = len(self._lats)

        # Insufficient history fallback
        if n < 2:
            if n == 1:
                cur_lat, cur_lon = self._lats[0], self._lons[0]
                cluster = self._find_nearest_cluster(cur_lat, cur_lon)
                return {
                    "predicted_latitude":     cluster["lat"],
                    "predicted_longitude":    cluster["lon"],
                    "confidence":             0.40,
                    "prediction_mode":        "CLUSTER_FALLBACK_INSUFFICIENT_HISTORY",
                    "cluster_name":           cluster["name"],
                    "segment_distance_m":     round(self._last_segment_m, 2),
                    "cumulative_distance_km": round(self._cumulative_dist_km, 4),
                }
            elif self.clusters:
                # Absolute fallback when empty
                c = self.clusters[0]
                return {
                    "predicted_latitude":     c["lat"],
                    "predicted_longitude":    c["lon"],
                    "confidence":             0.25,
                    "prediction_mode":        "GLOBAL_CLUSTER_FALLBACK",
                    "cluster_name":           c["name"],
                    "segment_distance_m":     0.0,
                    "cumulative_distance_km": 0.0
                }
            return None

        lats = np.array(self._lats, dtype=np.float64)
        lons = np.array(self._lons, dtype=np.float64)

        # Exponential recency weights
        weights = np.exp(np.linspace(-2.0, 0.0, n - 1))
        weights /= weights.sum()

        diff_lats = np.diff(lats)
        diff_lons = np.diff(lons)
        seg_dists_m = _haversine_batch_m(lats, lons)

        pred_dlat = float(np.dot(diff_lats, weights))
        pred_dlon = float(np.dot(diff_lons, weights))

        predicted_lat = round(lats[-1] + pred_dlat, 7)
        predicted_lon = round(lons[-1] + pred_dlon, 7)

        # Confidence assessment
        depth_score = min(n / self.sequence_length, 1.0)
        mean_seg = float(np.mean(seg_dists_m)) if len(seg_dists_m) > 0 else 0.0

        if len(seg_dists_m) > 1 and mean_seg > 1.0:
            cv = float(np.std(seg_dists_m) / (mean_seg + 1e-9))
            variance_penalty = max(0.2, 1.0 - cv * 0.3)
        else:
            variance_penalty = 0.95

        confidence = round(depth_score * variance_penalty, 3)

        # If variance is extreme / GPS erratic, augment with cluster anchor
        prediction_mode = "LSTM_SEQUENCE_EXTRAPOLATION"
        cluster_name = None
        if confidence < 0.35 and self.clusters:
            nearest = self._find_nearest_cluster(lats[-1], lons[-1])
            # Blend 50% sequence + 50% cluster vector
            predicted_lat = round((predicted_lat + nearest["lat"]) / 2.0, 7)
            predicted_lon = round((predicted_lon + nearest["lon"]) / 2.0, 7)
            prediction_mode = "LSTM_CLUSTER_BLENDED_FALLBACK"
            cluster_name = nearest["name"]
            confidence = max(confidence, 0.50)

        return {
            "predicted_latitude":     predicted_lat,
            "predicted_longitude":    predicted_lon,
            "confidence":             confidence,
            "prediction_mode":        prediction_mode,
            "cluster_name":           cluster_name,
            "segment_distance_m":     round(self._last_segment_m, 2),
            "cumulative_distance_km": round(self._cumulative_dist_km, 4),
        }

    def _find_nearest_cluster(self, lat: float, lon: float) -> dict:
        """Finds closest location cluster zone."""
        if not self.clusters:
            return {"name": "Default Anchor", "lat": lat, "lon": lon}
        best = self.clusters[0]
        min_d = float('inf')
        for c in self.clusters:
            d = _haversine_m(lat, lon, c["lat"], c["lon"])
            if d < min_d:
                min_d = d
                best = c
        return best

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _persist_entry(self, lat: float, lon: float, ts: float) -> None:
        if lat == 0.0 and lon == 0.0:
            return
        try:
            existing: list = []
            if os.path.exists(_HISTORY_FILE):
                try:
                    with open(_HISTORY_FILE, "r", encoding="utf-8") as fh:
                        raw = fh.read().strip()
                        if raw and raw != "[]":
                            existing = json.loads(raw)
                except Exception:
                    existing = []

            if not isinstance(existing, list):
                existing = []

            existing.append({
                "timestamp": datetime.fromtimestamp(ts).isoformat(),
                "latitude":  round(lat, 7),
                "longitude": round(lon, 7),
            })

            if len(existing) > _MAX_PERSISTED:
                existing = existing[-_MAX_PERSISTED:]

            with open(_HISTORY_FILE, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, separators=(",", ":"))
        except Exception as exc:
            print(f"[Predictor] Persist warning: {exc}")

    def _load_history(self) -> None:
        if not os.path.exists(_HISTORY_FILE):
            return
        try:
            with open(_HISTORY_FILE, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
                if not raw or raw == "[]":
                    return
                entries: list = json.loads(raw)
            if not isinstance(entries, list):
                return
            recent = entries[-self.sequence_length:]
            for e in recent:
                try:
                    ts = time.mktime(datetime.fromisoformat(e["timestamp"]).timetuple())
                except Exception:
                    ts = time.time()
                lat = float(e.get("latitude", 0.0))
                lon = float(e.get("longitude", 0.0))
                if lat != 0.0 and lon != 0.0:
                    self._lats.append(lat)
                    self._lons.append(lon)
                    self._timestamps.append(float(ts))
            if self._lats:
                print(f"[Predictor] Loaded {len(self._lats)} historical waypoints from disk.")
        except Exception as exc:
            try:
                with open(_HISTORY_FILE, "w", encoding="utf-8") as fh:
                    fh.write("[]\n")
            except Exception:
                pass
            print(f"[Predictor] History load notice: {exc}")

    def get_history_summary(self) -> dict:
        return {
            "buffer_depth":           len(self._lats),
            "sequence_length":        self.sequence_length,
            "cumulative_distance_km": round(self._cumulative_dist_km, 4),
            "last_segment_m":         round(self._last_segment_m, 2),
            "clusters_count":         len(self.clusters),
            "last_known_lat":         self._lats[-1] if self._lats else None,
            "last_known_lon":         self._lons[-1] if self._lons else None,
        }

    def reset(self) -> None:
        self._lats.clear()
        self._lons.clear()
        self._timestamps.clear()
        self._cumulative_dist_km = 0.0
        self._last_segment_m     = 0.0

    def predict_next_waypoint(self, lat: float = None, lon: float = None, corridor=None) -> dict | None:
        """
        Predicts the next corridor waypoint ahead of the current position and
        prints a continuous live log to the terminal.
        """
        if lat is None or lon is None:
            if self._lats:
                lat, lon = self._lats[-1], self._lons[-1]
            else:
                pred = self.predict_next_coordinate()
                return pred

        self.update_history(lat, lon)
        seq = self.predict_next_coordinate()

        pts = list(corridor) if corridor else []
        if not pts and map_config is not None:
            try:
                pts = list(map_config.get_primary_corridor())
            except Exception:
                pts = []

        next_wp = None
        if pts:
            best_i = 0
            best_d = float("inf")
            for i, (wlat, wlon) in enumerate(pts):
                d = _haversine_m(lat, lon, wlat, wlon)
                if d < best_d:
                    best_d = d
                    best_i = i
            nxt = min(best_i + 1, len(pts) - 1)
            next_wp = pts[nxt]
            # If already past last, hold destination
            if best_i >= len(pts) - 1:
                next_wp = pts[-1]

        if next_wp:
            conf = seq.get("confidence", 0.55) if seq else 0.55
            # Boost confidence when locked to named corridor
            conf = min(0.99, max(conf, 0.62))
            result = {
                "predicted_latitude": round(next_wp[0], 7),
                "predicted_longitude": round(next_wp[1], 7),
                "confidence": round(conf, 3),
                "prediction_mode": "CORRIDOR_NEXT_WAYPOINT",
                "segment_distance_m": round(self._last_segment_m, 2),
                "cumulative_distance_km": round(self._cumulative_dist_km, 4),
            }
        else:
            result = seq

        if result:
            conf_pct = float(result.get("confidence", 0.0)) * 100.0
            print(
                f"[Prediction Engine] Next Waypoint: "
                f"{result.get('predicted_latitude')}, {result.get('predicted_longitude')} "
                f"| Confidence: {conf_pct:.1f}%"
            )
        return result