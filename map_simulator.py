"""
map_simulator.py
──────────────────────────────────────────────────────────────────────────
AURA Live Map Intelligence & Master Control Center — Production Edition

Features:
  • Realistic Cartographic City Map: Marine Riverfront Channel, Sanctuary
    Nature Reserve, Botanical Campus Gardens, NH-45 Express Highway,
    City POIs, Safe Route Corridor, Breadcrumbs, Radar GPS Puck.
  • Complete Safety System Control ("Control Everything from the Map"):
      - Mode Switch: 👁️ DISPLAY ONLY (SHOW ONLY) vs 🛡️ ARMED ACTION MODE
      - 🚨 Manual SOS Trigger (direct distress dispatch)
      - 💥 Simulate Fall (drop/impact sequence)
      - ↗️ Dynamic Route Deviation (offset from corridor)
      - ⚡ Speed Anomaly Boost
      - ✅ "I'm Safe" / Alarm Reset & Cooldown
      - 🎯 Quick Destination Selection (Campus, Home, Metro, Police, Hospital)
      - 🚶/🚴/🚗/⚡ Pace Presets & Multipliers (×1, ×2, ×4, ×8)
      - 🖱️ Click anywhere or drag to roam freely across the map
      - 🎮 Interactive On-Canvas D-Pad & Directional buttons (N, S, E, W)
      - ⌨️ Global Hotkeys (↑↓←→, D, S, R)
  • Bidirectional live synchronization with Main Safety Dashboard & Controller.
"""

import math
import logging
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import map_config

try:
    import winsound
except ImportError:
    winsound = None

# ── Canvas / layout constants ─────────────────────────────────────────────────
CANVAS_W         = 520
CANVAS_H         = 460
TELEMETRY_W      = 260
WIN_W            = CANVAS_W + TELEMETRY_W + 30
WIN_H            = 730
CUSTOM_ROUTE_LABEL = "✏️ Custom Route (Click Map)"

# Hotkey step sizes in degrees (≈ 20 m at this latitude)
LAT_STEP = 0.00020
LON_STEP = 0.00024

# Speed multiplier presets
SPEED_PRESETS = [1, 2, 4, 8]

# Safety-check thresholds and timing live here so simulator behavior is easy to tune.
SAFETY_CHECK_CONFIG = {
    "overspeed_kmh": 25.0,
    "sudden_speed_jump_kmh": 25.0,
    "consecutive_ticks": 2,
    "route_deviation_m": 100.0,
    "route_consecutive_ticks": 3,
    "manual_drag_min_movement_m": 30.0,
    "manual_drag_min_speed_kmh": 60.0,
    "countdown_seconds": 15,
    "safe_cooldown_seconds": 20.0,
}

logger = logging.getLogger(__name__)

# Colour palette
C_BG         = "#0a0f1e"
C_SURFACE    = "#111827"
C_PANEL      = "#161f30"
C_PANEL2     = "#1a253a"
C_BORDER     = "#1e3a5f"
C_ACCENT     = "#38bdf8"
C_ROAD       = "#2563eb"
C_ROAD_DIM   = "#1d4ed8"
C_TRAIL      = "#ef4444"
C_TRAIL_OLD  = "#7f1d1d"
C_MARKER     = "#f97316"
C_MARKER_RIM = "#ffffff"
C_PREDICT    = "#facc15"
C_LANDMARK   = "#10b981"
C_TEXT_HI    = "#f1f5f9"
C_TEXT_MID   = "#94a3b8"
C_TEXT_DIM   = "#475569"
C_THREAT     = "#dc2626"
C_SAFE       = "#10b981"
C_DEVIATE    = "#a855f7"
C_AMBER      = "#f59e0b"


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_to_polyline_distance_m(position, route):
    """Return the shortest local-meter distance from a GPS point to a route line."""
    if not route:
        return float("inf")
    lat, lon = position
    lat_scale = 110540.0
    lon_scale = 111320.0 * math.cos(math.radians(lat))

    def project(point):
        return ((point[1] - lon) * lon_scale, (point[0] - lat) * lat_scale)

    point = (0.0, 0.0)
    projected = [project(vertex) for vertex in route]
    if len(projected) == 1:
        return math.hypot(*projected[0])

    minimum = float("inf")
    for start, end in zip(projected, projected[1:]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            nearest = start
        else:
            t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq))
            nearest = (start[0] + t * dx, start[1] + t * dy)
        minimum = min(minimum, math.hypot(point[0] - nearest[0], point[1] - nearest[1]))
    return minimum


class SafetyMonitor:
    """Debounce speed and route warnings before opening the safety check."""

    def __init__(self, on_trigger, config=None):
        self.config = dict(SAFETY_CHECK_CONFIG if config is None else config)
        self.on_trigger = on_trigger
        self.previous_speed = None
        self.speed_ticks = 0
        self.route_ticks = 0
        self.active = False
        self.sos_active = False
        self.cooldown_until = 0.0

    def update(self, position, speed, route, route_deviation_eligible=False,
               speed_anomaly_eligible=False):
        now = time.monotonic()
        speed = float(speed or 0.0)
        if self.active or self.sos_active or now < self.cooldown_until:
            self.previous_speed = speed
            return

        # Speed alerts require an eligible movement sample strictly above the
        # configured speed limit; acceleration alone below the limit is not SOS.
        speed_condition = speed_anomaly_eligible and speed > self.config["overspeed_kmh"]
        self.speed_ticks = self.speed_ticks + 1 if speed_condition else 0
        self.previous_speed = speed

        # Ignore click-to-place and other non-movement samples for deviation.
        distance_m = point_to_polyline_distance_m(position, route) if route_deviation_eligible else 0.0
        self.route_ticks = self.route_ticks + 1 if (
            route_deviation_eligible and distance_m > self.config["route_deviation_m"]
        ) else 0

        required = self.config["consecutive_ticks"]
        # Route distance has its own eligibility and threshold. Give it
        # precedence when both conditions occur in the same movement sample.
        route_confirmed = self.route_ticks >= self.config.get("route_consecutive_ticks", 1)
        speed_confirmed = self.speed_ticks >= required
        if route_confirmed and speed_condition:
            self._trigger(
                f"CRITICAL: Speed Anomaly & Route Deviation "
                f"({speed:.0f} km/h, {distance_m:.0f} m off route)"
            )
        elif route_confirmed:
            self._trigger(f"Route deviation: {distance_m:.0f} m from route")
        elif speed_confirmed:
            self._trigger(f"Over-speeding: {speed:.0f} km/h")

    def _trigger(self, reason):
        if self.active or self.sos_active:
            return
        self.active = True
        logger.warning("Safety check opened: %s", reason)
        self.on_trigger(reason)

    def resolve(self, cooldown_seconds=None):
        self.active = False
        self.speed_ticks = 0
        self.route_ticks = 0
        self.previous_speed = None
        cooldown = self.config["safe_cooldown_seconds"] if cooldown_seconds is None else cooldown_seconds
        self.cooldown_until = time.monotonic() + cooldown



class _IntProxy:
    def __init__(self, val=4):
        self._val = val
    def get(self):
        return self._val
    def set(self, v):
        self._val = v


class MapSimulatorWindow:
    """Full-featured interactive map simulation & mission control window."""

    def __init__(self, parent_root, controller):
        self.controller      = controller
        self.route_points    = []
        self.current_index   = 0
        self.is_playing      = False
        self.play_job        = None
        self.simulated_clock = time.time()
        self._speed_idx      = 0          # index into SPEED_PRESETS
        self._deviation_active = False
        self._pulse_phase    = 0.0        # for marker animation
        self._trail_coords   = []         # list of (px, py) drawn so far
        self.speed_var       = _IntProxy(4)
        self._base_speed     = 45.0       # base km/h
        self.user_heading_deg = 45.0
        self.is_dragging     = False
        self._drag_start_position = None
        self._drag_route_eligible = False
        self._last_drag_position = None
        self._last_drag_sample_time = 0.0
        self._drag_speed_kmh = 0.0
        self._last_drag_time = 0.0
        self._tele_vars      = {}
        self.safety_route_points = []
        self._after_ids = set()
        self._safety_after_ids = set()
        self._closing = False
        self._sos_active = False
        self.safety_popup = None
        self._safety_chime_stop = threading.Event()
        self._safety_chime_thread = None
        self.safety_countdown_job = None
        self.safety_countdown_remaining = 0
        self.safety_reason = ""

        # Display-Only Mode: defaults to True for visual simulation without false alarms
        # (user can toggle via the prominent top header button to Armed mode)
        self.display_only_mode = True
        if self.controller and hasattr(self.controller, "display_only_mode"):
            self.display_only_mode = self.controller.display_only_mode

        # ── Window setup ──────────────────────────────────────────────────────
        self.win = tk.Toplevel(parent_root)
        self.win.title("AURA — Interactive Map Simulator & Action Control")
        self.win.geometry(f"{WIN_W}x{WIN_H}+40+15")
        self.win.minsize(WIN_W, 680)
        self.win.configure(bg=C_BG)
        self.win.resizable(True, True)
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.safety_monitor = SafetyMonitor(self._show_safety_check)
        self._schedule_after(250, lambda: self.win.attributes("-topmost", False))

        self._build_ui()
        self._bind_hotkeys()
        self._load_route()
        self._start_pulse_animation()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

    def _schedule_after(self, delay_ms, callback):
        if self._closing:
            return None
        job_id = None

        def _run():
            self._after_ids.discard(job_id)
            if not self._closing:
                callback()

        job_id = self.win.after(delay_ms, _run)
        self._after_ids.add(job_id)
        return job_id

    def _cancel_after(self, job_id):
        if not job_id:
            return
        self._after_ids.discard(job_id)
        try:
            self.win.after_cancel(job_id)
        except tk.TclError:
            pass

    def _is_sos_active(self):
        if self._sos_active:
            return True
        if self.controller and getattr(self.controller, "system_status", "") == "EMERGENCY_TRIGGERED":
            return True
        gui = getattr(self.controller, "gui_app", None) if self.controller else None
        return bool(gui and getattr(gui, "is_threat_active", False))

    def _show_safety_check(self, reason):
        if self._closing or self._safety_popup_is_open():
            return
        if self._is_sos_active():
            self._sos_active = True
            self.safety_monitor.sos_active = True
            self._tele_vars["safety_status"].set("SOS")
            return
        self.safety_reason = reason
        self.safety_countdown_remaining = self.safety_monitor.config["countdown_seconds"]
        self._tele_vars["safety_status"].set("CHECK")

        reason_upper = str(reason).upper()
        has_speed_cause = "SPEED" in reason_upper or "OVER-SPEED" in reason_upper
        has_route_cause = "ROUTE" in reason_upper or "DEVIATION" in reason_upper
        if has_speed_cause and has_route_cause:
            cause_title = "CRITICAL: Speed Anomaly & Route Deviation"
        elif has_route_cause:
            cause_title = "Route Deviation Detected"
        elif has_speed_cause:
            cause_title = "Speed Anomaly Detected"
        else:
            cause_title = "Safety Check"

        popup = tk.Toplevel(self.win)
        self.safety_popup = popup
        popup.title(cause_title.upper())
        popup.configure(bg="#0b1220")
        popup.resizable(False, False)
        popup.transient(self.win)
        popup.attributes("-topmost", True)
        popup.protocol("WM_DELETE_WINDOW", lambda: None)

        width, height = 440, 300
        self.win.update_idletasks()
        x = self.win.winfo_rootx() + max(0, (self.win.winfo_width() - width) // 2)
        y = self.win.winfo_rooty() + max(0, (self.win.winfo_height() - height) // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")

        panel = tk.Frame(popup, bg="#0b1220", padx=24, pady=18)
        panel.pack(fill="both", expand=True)
        tk.Label(panel, text=cause_title, font=("Segoe UI", 14, "bold"),
                 fg="#fca5a5", bg="#0b1220", wraplength=390).pack(pady=(0, 8))
        tk.Label(panel, text="Are you safe?", font=("Segoe UI", 20, "bold"),
                 fg="#f8fafc", bg="#0b1220").pack(pady=(0, 8))
        tk.Label(panel, text=reason, font=("Segoe UI", 11, "bold"),
                 fg="#fca5a5", bg="#0b1220", wraplength=380).pack(pady=(0, 14))

        self.safety_timer_label = tk.Label(
            panel, text=f"{self.safety_countdown_remaining} seconds",
            font=("Segoe UI", 16, "bold"), fg="#f8fafc", bg="#0b1220"
        )
        self.safety_timer_label.pack(pady=(0, 8))
        style = ttk.Style(popup)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Safety.Horizontal.TProgressbar", troughcolor="#1f2937",
                        background="#ef4444", thickness=14)
        self.safety_progress = ttk.Progressbar(
            panel, style="Safety.Horizontal.TProgressbar", orient="horizontal",
            mode="determinate", maximum=self.safety_monitor.config["countdown_seconds"],
            value=self.safety_countdown_remaining, length=360
        )
        self.safety_progress.pack(fill="x", pady=(0, 18))

        buttons = tk.Frame(panel, bg="#0b1220")
        buttons.pack(fill="x")
        tk.Button(buttons, text="I'M SAFE", font=("Segoe UI", 10, "bold"),
                  bg="#059669", fg="white", bd=0, padx=14, pady=9,
                  command=self._cancel_safety_check).pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(buttons, text="NEED HELP", font=("Segoe UI", 10, "bold"),
                  bg="#dc2626", fg="white", bd=0, padx=14, pady=9,
                  command=self._dispatch_safety_sos).pack(side="right", fill="x", expand=True, padx=(6, 0))

        popup.grab_set()
        popup.focus_force()
        self._draw_map()
        self._start_safety_chime()
        logger.warning("Safety check shown: %s", reason)
        self.safety_countdown_job = self._schedule_safety_after(1000, self._safety_countdown_tick)

    def _safety_popup_is_open(self):
        popup = self.safety_popup
        if popup is None:
            return False
        try:
            if popup.winfo_exists():
                return True
        except tk.TclError:
            pass

    def _schedule_safety_after(self, delay_ms, callback):
        job_ref = {}

        def _run():
            job_id = job_ref.get("id")
            if job_id:
                self._safety_after_ids.discard(job_id)
            callback()

        job_id = self._schedule_after(delay_ms, _run)
        if job_id:
            job_ref["id"] = job_id
            self._safety_after_ids.add(job_id)
        return job_id
        self.safety_popup = None
        self.safety_countdown_job = None
        return False

    def _safety_countdown_tick(self):
        if self.safety_popup is None or not self.safety_popup.winfo_exists():
            return
        self.safety_countdown_remaining -= 1
        remaining = self.safety_countdown_remaining
        self.safety_timer_label.configure(
            text=f"{remaining} seconds",
            fg="#ef4444" if remaining <= 5 else "#f8fafc",
        )
        self.safety_progress.configure(value=max(0, remaining))
        if remaining <= 0:
            self._dispatch_safety_sos()
            return
        self.safety_countdown_job = self._schedule_safety_after(1000, self._safety_countdown_tick)

    def _close_safety_popup(self):
        self._stop_safety_chime()
        for job_id in tuple(self._safety_after_ids):
            self._cancel_after(job_id)
        self._safety_after_ids.clear()
        self._cancel_after(self.safety_countdown_job)
        self.safety_countdown_job = None
        popup = self.safety_popup
        self.safety_popup = None
        if popup is not None:
            try:
                popup.grab_release()
                popup.destroy()
            except tk.TclError:
                pass

    def _cancel_safety_check(self):
        if self.safety_popup is None:
            return
        logger.info("Safety check cancelled by user: %s", self.safety_reason)
        self._close_safety_popup()
        self._action_confirm_safe(cooldown_seconds=SAFETY_CHECK_CONFIG["safe_cooldown_seconds"])

    def _dispatch_safety_sos(self):
        if self.safety_popup is None:
            return
        logger.warning("Safety check expired or user requested help: %s", self.safety_reason)
        self._close_safety_popup()
        self.safety_monitor.sos_active = True
        self._sos_active = True
        self._tele_vars["safety_status"].set("SOS")
        self._draw_map()
        self._action_trigger_sos()

    def _start_safety_chime(self):
        """Play short repeating alert tones off the UI thread until resolved."""
        self._stop_safety_chime()
        stop_event = threading.Event()
        self._safety_chime_stop = stop_event

        def _play():
            if winsound is None:
                while not stop_event.is_set():
                    print("\a", end="", flush=True)
                    if stop_event.wait(1.4):
                        break
                return
            while not stop_event.is_set():
                try:
                    winsound.Beep(1200, 100)
                    if stop_event.is_set():
                        break
                    winsound.Beep(1600, 110)
                except RuntimeError as exc:
                    logger.warning("Safety chime playback failed: %s", exc)
                    break
                if stop_event.wait(1.2):
                    break

        self._safety_chime_thread = threading.Thread(
            target=_play, name="AURA-safety-chime", daemon=True
        )
        self._safety_chime_thread.start()

    def _stop_safety_chime(self):
        stop_event = getattr(self, "_safety_chime_stop", None)
        if stop_event is not None:
            stop_event.set()

    def _schedule_safety_confirmation(self, position, speed, route_deviation_eligible=False,
                                      additional_ticks=0, speed_anomaly_eligible=False):
        route = tuple(self.safety_route_points)
        def _confirm(remaining):
            self.safety_monitor.update(
                position, speed, route,
                route_deviation_eligible=route_deviation_eligible,
                speed_anomaly_eligible=speed_anomaly_eligible,
            )
            if remaining > 0:
                self._schedule_safety_after(100, lambda: _confirm(remaining - 1))

        self._schedule_safety_after(100, lambda: _confirm(additional_ticks))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # 1. Header bar
        hdr = tk.Frame(self.win, bg=C_SURFACE, height=46)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        badge = tk.Canvas(hdr, width=28, height=28, bg=C_SURFACE, highlightthickness=0)
        badge.pack(side="left", padx=(12, 6), pady=8)
        badge.create_oval(2, 2, 26, 26, fill=C_ACCENT, outline="")
        badge.create_text(14, 14, text="🗺", font=("Segoe UI", 12))

        tk.Label(hdr, text="AURA LIVE MAP MISSION CONTROL (INTERACTIVE MOVEMENT & ACTIONS)",
                 font=("Segoe UI", 10, "bold"), fg=C_ACCENT,
                 bg=C_SURFACE).pack(side="left")

        # Mode Toggle Button (Display Only vs Armed Mode)
        self.btn_mode_toggle = tk.Button(
            hdr, font=("Segoe UI", 8, "bold"),
            bd=0, cursor="hand2", padx=10, pady=3,
            command=self._toggle_display_only_mode
        )
        self.btn_mode_toggle.pack(side="left", padx=(12, 0))
        self._refresh_mode_toggle_btn()

        self._hotkey_hint = tk.Label(
            hdr,
            text="🖱️ Drag Map to Move  |  ↑↓←→ / WASD  |  ⚡ Action Controls",
            font=("Segoe UI", 8), fg=C_TEXT_DIM, bg=C_SURFACE,
        )
        self._hotkey_hint.pack(side="right", padx=12)

        # 2. Main body: canvas + control/telemetry sidebar
        body = tk.Frame(self.win, bg=C_BG)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 0))

        # Left: Map canvas
        self.canvas = tk.Canvas(
            body, width=CANVAS_W, height=CANVAS_H,
            bg="#0c1322", highlightthickness=1,
            highlightbackground=C_BORDER,
        )
        self.canvas.pack(side="left")

        # Map interactions: click to move, drag to roam
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # Right: Comprehensive Master Control & Telemetry Sidebar
        self._build_master_sidebar(body)

        # 3. Bottom control bar (Route, Playback, Directional Stepper)
        ctrl = tk.Frame(self.win, bg=C_PANEL, padx=10, pady=6)
        ctrl.pack(fill="x", padx=10, pady=(6, 4))

        # Route Selector
        tk.Label(ctrl, text="ROUTE", font=("Segoe UI", 8, "bold"),
                 fg="#818cf8", bg=C_PANEL).pack(side="left", padx=(0, 4))

        route_options = list(map_config.get_dynamic_routes(self.controller).keys()) + [CUSTOM_ROUTE_LABEL]
        self.route_var = tk.StringVar(value=route_options[0] if route_options else CUSTOM_ROUTE_LABEL)
        route_dd = ttk.Combobox(ctrl, textvariable=self.route_var,
                                values=route_options, state="readonly",
                                font=("Segoe UI", 8), width=24)
        route_dd.pack(side="left", padx=(0, 8))
        route_dd.bind("<<ComboboxSelected>>", lambda e: self._on_route_change())

        # Play / Pause
        self.play_btn = tk.Button(
            ctrl, text="▶ Play", font=("Segoe UI", 8, "bold"),
            bg=C_SAFE, fg="#ffffff", bd=0, cursor="hand2",
            command=self._toggle_play, padx=10, pady=3,
        )
        self.play_btn.pack(side="left", padx=(0, 4))

        # Step 1x
        tk.Button(
            ctrl, text="⏭ Step", font=("Segoe UI", 8),
            bg="#334155", fg="#ffffff", bd=0, cursor="hand2",
            command=self._step_once, padx=8, pady=3,
        ).pack(side="left", padx=(0, 4))

        # Reset Route
        tk.Button(
            ctrl, text="🔄 Reset", font=("Segoe UI", 8),
            bg="#334155", fg="#ffffff", bd=0, cursor="hand2",
            command=self._reset_route, padx=8, pady=3,
        ).pack(side="left", padx=(0, 10))

        # Cardinal Direction Steppers
        step_box = tk.Frame(ctrl, bg=C_PANEL)
        step_box.pack(side="left", padx=(4, 8))
        for lbl, dlat, dlon, hdg in [
            ("▲ N", LAT_STEP, 0, 0),
            ("▼ S", -LAT_STEP, 0, 180),
            ("◄ W", 0, -LON_STEP, 270),
            ("► E", 0, LON_STEP, 90)
        ]:
            tk.Button(
                step_box, text=lbl, font=("Segoe UI", 7, "bold"),
                bg="#1e293b", fg=C_ACCENT, bd=0, cursor="hand2",
                command=lambda dl=dlat, dn=dlon, h=hdg: self._arrow_move(dl, dn, h),
                padx=5, pady=2
            ).pack(side="left", padx=1)

        # Speed Multiplier Button
        self.speed_mult_btn = tk.Button(
            ctrl, text="🚀 Mult: ×1", font=("Segoe UI", 8, "bold"),
            bg="#0284c7", fg="#ffffff", bd=0, cursor="hand2",
            command=self._cycle_speed, padx=8, pady=3
        )
        self.speed_mult_btn.pack(side="right")

        # 4. Status Bar
        self.status_var = tk.StringVar(value="Ready • Click/drag map to roam freely or use controls.")
        self.status_lbl = tk.Label(
            self.win, textvariable=self.status_var,
            font=("Segoe UI", 8), fg=C_TEXT_MID, bg=C_BG,
            anchor="w", wraplength=WIN_W - 20,
        )
        self.status_lbl.pack(fill="x", padx=14, pady=(2, 6))

    def _build_master_sidebar(self, parent):
        sb = tk.Frame(parent, bg=C_PANEL, width=TELEMETRY_W)
        sb.pack(side="right", fill="both", expand=True, padx=(10, 0))
        sb.pack_propagate(False)

        # Read-only live status panel; simulator controls stay off this sidebar.
        sep2 = tk.Frame(sb, bg=C_BORDER, height=1)
        sep2.pack(fill="x", padx=8, pady=4)

        tk.Label(sb, text="📊 LIVE TELEMETRY", font=("Segoe UI", 8, "bold"),
                 fg=C_ACCENT, bg=C_PANEL).pack(anchor="w", padx=8, pady=(2, 3))

        self._tele_vars = {}
        fields = [
            ("speed_kmh",     "⚡ Speed",      "-- km/h",   C_TEXT_HI),
            ("safety_status", "🛡 Safety",     "OK",        C_SAFE),
            ("street_name",   "📍 Street",     "Botanical", C_TEXT_HI),
            ("target_name",   "🏁 Target",     "Campus",    C_ACCENT),
            ("hav_dist",      "📏 Seg. Dist",  "-- m",      C_TEXT_HI),
            ("cum_dist",      "🛣 Trip Dist",   "-- km",     C_TEXT_HI),
            ("pred_lat",      "🤖 Pred. Lat",  "--",        C_PREDICT),
            ("pred_lon",      "🤖 Pred. Lon",  "--",        C_PREDICT),
            ("confidence",    "🎯 Confidence", "--",        C_PREDICT),
            ("network",       "📶 Network",    "Connected ✅", C_SAFE),
            ("threat_status", "🛡 Mode/Threat", "👁️ SHOW ONLY", C_ACCENT),
            ("wp_index",      "📍 Waypoint",   "0 / 0",     C_TEXT_MID),
        ]

        for key, label, default, color in fields:
            row = tk.Frame(sb, bg=C_PANEL)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=label, font=("Segoe UI", 7, "bold"),
                     fg=C_TEXT_DIM, bg=C_PANEL, anchor="w",
                     width=11).pack(side="left")
            var = tk.StringVar(value=default)
            self._tele_vars[key] = var
            tk.Label(row, textvariable=var, font=("Segoe UI", 7, "bold"),
                     fg=color, bg=C_PANEL, anchor="w").pack(side="left")

    def _refresh_mode_toggle_btn(self):
        if not hasattr(self, "btn_mode_toggle") or not self.btn_mode_toggle.winfo_exists():
            return
        if self.display_only_mode:
            self.btn_mode_toggle.config(
                text="👁️ DISPLAY ONLY (SHOW ONLY)",
                bg="#0284c7", fg="#ffffff", activebackground="#0369a1", activeforeground="#ffffff"
            )
            if "threat_status" in self._tele_vars:
                self._tele_vars["threat_status"].set("👁️ SHOW ONLY")
        else:
            self.btn_mode_toggle.config(
                text="🛡️ ACTION MODE: ARMED",
                bg="#991b1b", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff"
            )
            if "threat_status" in self._tele_vars:
                self._tele_vars["threat_status"].set("● ARMED")

    def _toggle_display_only_mode(self):
        self.display_only_mode = not self.display_only_mode
        if self.controller:
            self.controller.display_only_mode = self.display_only_mode
        if self.controller and getattr(self.controller, "gui_app", None):
            self.controller.gui_app.display_only_mode = self.display_only_mode
            if hasattr(self.controller.gui_app, "_update_display_only_btn"):
                self.controller.gui_app._update_display_only_btn()
        self._refresh_mode_toggle_btn()
        mode_desc = "Display Only (Show Only — Emergency Actions Suppressed)" if self.display_only_mode else "Armed Mode (Emergency Actions Active)"
        self._update_status(f"System Mode changed to: {mode_desc}")

    # ── Master Actions ("Control Everything") ──────────────────────────────────

    def _action_trigger_sos(self):
        """Dispatches immediate SOS emergency distress sequence."""
        self._sos_active = True
        self.safety_monitor.sos_active = True
        if "safety_status" in self._tele_vars:
            self._tele_vars["safety_status"].set("SOS")
        if self.safety_popup is not None:
            self._close_safety_popup()
        logger.warning("SOS dispatched from map simulator.")
        self._update_status("🚨 MANUAL SOS TRIGGERED from Mission Control Map!")
        if self.controller:
            loc = self.controller.location_engine.get_current_location() if hasattr(self.controller, "location_engine") else {}
            if self.controller.gui_app:
                self.controller.gui_app.trigger_threat("🚨 MANUAL SOS (MISSION CONTROL MAP)")
            else:
                self.controller.execute_emergency_sequence("MANUAL_SOS_MAP", loc)

    def _action_simulate_fall(self):
        """Simulates high-G impact / drop fall event."""
        self._update_status("💥 HARD FALL / IMPACT SIMULATED!")
        if self.controller:
            if hasattr(self.controller, "handle_fall_detected"):
                self.controller.handle_fall_detected()
            elif self.controller.gui_app:
                self.controller.gui_app.trigger_threat("⚠️ HARD FALL / DROP DETECTED")

    def _action_speed_boost(self):
        """Injects excessive speed anomaly (>75 km/h)."""
        self._base_speed = 88.0
        self._tele_vars["speed_kmh"].set("88.0 km/h")
        self._update_status("⚡ SPEED ANOMALY INJECTED (88.0 km/h)!")
        position = self._current_position()
        self._fire_safety_cycle(*position, speed_override=88.0)

    def _action_confirm_safe(self, cooldown_seconds=20.0):
        """Reuse the existing safe-confirmation path with the monitor cooldown."""
        self._close_safety_popup()
        self._deviation_active = False
        self._base_speed = 45.0
        self._sos_active = False
        self.safety_monitor.sos_active = False
        self.safety_monitor.resolve(cooldown_seconds)
        self.safety_reason = ""
        self._tele_vars["safety_status"].set("OK")
        if hasattr(self, "btn_deviate") and self.btn_deviate.winfo_exists():
            self.btn_deviate.config(text="↗ Deviate", bg=C_DEVIATE)
        if self.controller:
            gui = getattr(self.controller, "gui_app", None)
            if gui and getattr(gui, "is_threat_active", False):
                self.controller.gui_app.dismiss_alarm("Mission Control Confirmed Safe")
            elif gui and hasattr(gui, "reset_emergency_ui_state"):
                gui.reset_emergency_ui_state()
            if hasattr(self.controller, "acknowledge_user_safe"):
                self.controller.acknowledge_user_safe(cooldown_seconds=cooldown_seconds)
            else:
                self.controller.is_emergency_active = False
                if hasattr(self.controller, "anomaly_engine"):
                    self.controller.anomaly_engine.acknowledge_safe(cooldown_seconds=cooldown_seconds)
                self.controller.system_status = "ACTIVE_MONITORING"
        self._draw_map()
        self._update_status(f"✅ Confirmed Safe — {cooldown_seconds:.0f}s cooldown active.")

    def _set_target_destination(self, name: str, lat: float, lon: float):
        """Sets destination in controller, updates corridor and re-routes."""
        if self.controller:
            self.controller.set_trip_destination(name, lat, lon)
            if self.controller.gui_app:
                self.controller.gui_app.destination_status_str.set(f"🏁 Target: {name}")
                if hasattr(self.controller.gui_app, "_tele_dest"):
                    self.controller.gui_app._tele_dest.set(name)
        self._tele_vars["target_name"].set(name)
        self._draw_map()
        self._update_status(f"🎯 Target set to '{name}' ({lat:.4f}, {lon:.4f})")

    def _set_base_pace(self, spd: float):
        previous_speed = self._base_speed * SPEED_PRESETS[self._speed_idx]
        self._base_speed = spd
        speed = spd * SPEED_PRESETS[self._speed_idx]
        self._tele_vars["speed_kmh"].set(f"{speed:.1f} km/h")
        self._update_status(f"Pace updated to {spd:.0f} km/h")
        position = self._current_position()
        self._fire_safety_cycle(*position, speed_override=speed)

    def _current_position(self):
        if self.route_points and 0 <= self.current_index < len(self.route_points):
            return self.route_points[self.current_index]
        return map_config.CENTER_LAT, map_config.CENTER_LON

    # ── Hotkeys & Movement ────────────────────────────────────────────────────

    def _bind_hotkeys(self):
        self.win.bind("<Up>",    lambda e: self._arrow_move( LAT_STEP, 0, 0))
        self.win.bind("<Down>",  lambda e: self._arrow_move(-LAT_STEP, 0, 180))
        self.win.bind("<Left>",  lambda e: self._arrow_move(0, -LON_STEP, 270))
        self.win.bind("<Right>", lambda e: self._arrow_move(0,  LON_STEP, 90))
        self.win.bind("<w>",     lambda e: self._arrow_move( LAT_STEP, 0, 0))
        self.win.bind("<W>",     lambda e: self._arrow_move( LAT_STEP, 0, 0))
        self.win.bind("<s>",     lambda e: self._arrow_move(-LAT_STEP, 0, 180))
        self.win.bind("<S>",     lambda e: self._arrow_move(-LAT_STEP, 0, 180))
        self.win.bind("<a>",     lambda e: self._arrow_move(0, -LON_STEP, 270))
        self.win.bind("<A>",     lambda e: self._arrow_move(0, -LON_STEP, 270))
        self.win.bind("<d>",     lambda e: self._trigger_deviation())
        self.win.bind("<D>",     lambda e: self._trigger_deviation())
        self.win.bind("<r>",     lambda e: self._reset_route())
        self.win.bind("<R>",     lambda e: self._reset_route())
        self.win.bind("<F9>",    lambda e: self._debug_force_safety_check())
        self.win.focus_set()

    def _debug_force_safety_check(self):
        if not self._is_sos_active() and self.safety_popup is None:
            self.safety_monitor._trigger("Debug: forced safety check")

    def _arrow_move(self, dlat: float, dlon: float, heading_deg: float = None):
        """Move the simulated marker freely by keyboard arrows or cardinal buttons."""
        if not self.route_points:
            lat, lon = map_config.CENTER_LAT, map_config.CENTER_LON
            self.route_points = [(lat, lon)]
            self.current_index = 0

        cur_lat, cur_lon = self.route_points[self.current_index]
        new_lat = round(cur_lat + dlat, 6)
        new_lon = round(cur_lon + dlon, 6)
        print(f"🛰️ [Map Simulator] Lat: {new_lat:.6f}, Lon: {new_lon:.6f}")

        if heading_deg is not None:
            self.user_heading_deg = heading_deg

        self.route_points.append((new_lat, new_lon))
        self.current_index = len(self.route_points) - 1

        px, py = map_config.latlon_to_pixel(new_lat, new_lon, (CANVAS_W, CANVAS_H))
        self._trail_coords.append((px, py))

        # Directional controls are deliberate UI nudges, not GPS movement.
        self._fire_safety_cycle(new_lat, new_lon, route_deviation_eligible=False)
        self._draw_map()
        self._update_status(f"Moved to ({new_lat:.5f}, {new_lon:.5f})")

    # ── Interactive Canvas Click & Drag ───────────────────────────────────────

    def _on_canvas_click(self, event):
        """Handles canvas click: jumping user marker or clicking on-canvas D-Pad."""
        # Check D-Pad in bottom-right corner
        cx, cy = CANVAS_W - 42, CANVAS_H - 42
        dx = event.x - cx
        dy = event.y - cy
        dist = math.hypot(dx, dy)
        if dist <= 36:
            angle = (math.degrees(math.atan2(dx, -dy))) % 360
            if 315 <= angle or angle < 45:
                self._arrow_move(LAT_STEP, 0, 0)      # North
            elif 45 <= angle < 135:
                self._arrow_move(0, LON_STEP, 90)     # East
            elif 135 <= angle < 225:
                self._arrow_move(-LAT_STEP, 0, 180)   # South
            else:
                self._arrow_move(0, -LON_STEP, 270)   # West
            return

        # Direct canvas roaming: teleport/move user marker to clicked spot
        self.is_dragging = True
        self._drag_route_eligible = False
        lat, lon = map_config.pixel_to_latlon(event.x, event.y, (CANVAS_W, CANVAS_H))
        self._drag_start_position = (lat, lon)
        self._last_drag_position = (lat, lon)
        self._last_drag_sample_time = time.monotonic()
        self._last_drag_time = 0.0
        self._drag_speed_kmh = 0.0
        self._drag_route_eligible = False
        self._jump_user_position(lat, lon, route_deviation_eligible=False)

    def _on_canvas_drag(self, event):
        """Smoothly drags user marker across the map."""
        now = time.time()
        if (now - self._last_drag_time) < 0.05:
            return
        self._last_drag_time = now

        if event.x >= CANVAS_W - 75 and event.y >= CANVAS_H - 75:
            return

        lat, lon = map_config.pixel_to_latlon(event.x, event.y, (CANVAS_W, CANVAS_H))
        sample_time = time.monotonic()
        if self._last_drag_position is not None and self._last_drag_sample_time:
            elapsed = max(sample_time - self._last_drag_sample_time, 0.001)
            distance_m = _haversine_km(*self._last_drag_position, lat, lon) * 1000.0
            self._drag_speed_kmh = distance_m / elapsed * 3.6
        self._last_drag_position = (lat, lon)
        self._last_drag_sample_time = sample_time
        # Only actual marker dragging is eligible for route deviation checks.
        # The monitor requires >100 m off-route for three consecutive samples.
        self._drag_route_eligible = True
        self._jump_user_position(
            lat, lon,
            route_deviation_eligible=self._drag_route_eligible,
            speed_override=self._drag_speed_kmh,
            speed_anomaly_eligible=False,
        )

    def _on_canvas_release(self, event):
        self.is_dragging = False
        self._drag_start_position = None
        self._drag_route_eligible = False
        self._last_drag_position = None
        self._last_drag_sample_time = 0.0

    def _jump_user_position(self, lat: float, lon: float, route_deviation_eligible=False,
                            speed_override=None, speed_anomaly_eligible=False):
        lat = round(lat, 6)
        lon = round(lon, 6)
        print(f"🛰️ [Map Simulator] Lat: {lat:.6f}, Lon: {lon:.6f}")
        if not self.route_points:
            self.route_points = [(lat, lon)]
            self.current_index = 0
        else:
            prev_lat, prev_lon = self.route_points[self.current_index]
            dy = lat - prev_lat
            dx = (lon - prev_lon) * math.cos(math.radians(prev_lat))
            if abs(dy) > 1e-6 or abs(dx) > 1e-6:
                self.user_heading_deg = math.degrees(math.atan2(dx, dy)) % 360
            self.route_points.append((lat, lon))
            self.current_index = len(self.route_points) - 1

        px, py = map_config.latlon_to_pixel(lat, lon, (CANVAS_W, CANVAS_H))
        self._trail_coords.append((px, py))
        self._fire_safety_cycle(
            lat, lon, speed_override=speed_override,
            route_deviation_eligible=route_deviation_eligible,
            speed_anomaly_eligible=speed_anomaly_eligible,
        )
        self._draw_map()
        self._update_status(f"Roamed to ({lat:.5f}, {lon:.5f})")

    # ── Deviation & Speed ─────────────────────────────────────────────────────

    def _trigger_deviation(self):
        """Inject a lateral offset to simulate leaving the safe route."""
        if not self.route_points:
            return
        self._deviation_active = not self._deviation_active
        dev_status = "ACTIVE" if self._deviation_active else "OFF"

        if hasattr(self, "btn_deviate") and self.btn_deviate.winfo_exists():
            self.btn_deviate.config(
                text="↗ DEVIATING" if self._deviation_active else "↗ Deviate",
                bg="#f43f5e" if self._deviation_active else C_DEVIATE
            )

        if self._deviation_active:
            n = len(self.route_points)
            dev_start = self.current_index
            lat, lon = self.route_points[dev_start]
            next_lat, next_lon = self.route_points[min(dev_start + 1, n - 1)]
            north_m = (next_lat - lat) * 110540.0
            east_m = (next_lon - lon) * 111320.0 * math.cos(math.radians(lat))
            length_m = math.hypot(north_m, east_m) or 1.0
            # Shift sideways from the route tangent so the deviation is truly off-route.
            offset_north_m = -east_m / length_m * 300.0
            offset_east_m = north_m / length_m * 300.0
            offset_lat = offset_north_m / 110540.0
            offset_lon = offset_east_m / (111320.0 * math.cos(math.radians(lat)))
            for i in range(dev_start, n):
                lat, lon = self.route_points[i]
                self.route_points[i] = (round(lat + offset_lat, 6), round(lon + offset_lon, 6))
            self._draw_map()
            self._update_status("⚠️ DEVIATION INJECTED — Path shifted 300m off safe route!")
            position = self._current_position()
            speed = self._base_speed * SPEED_PRESETS[self._speed_idx]
            # This button changes the simulator display; it is not a real drag
            # and must not open a safety check by itself.
            self._fire_safety_cycle(*position, speed_override=speed)
        else:
            self._on_route_change()
            self._update_status("✅ Deviation cleared — Route restored.")

    def _cycle_speed(self):
        old_speed = self._base_speed * SPEED_PRESETS[self._speed_idx]
        self._speed_idx = (self._speed_idx + 1) % len(SPEED_PRESETS)
        mult = SPEED_PRESETS[self._speed_idx]
        new_speed = self._base_speed * mult
        if hasattr(self, "speed_mult_btn") and self.speed_mult_btn.winfo_exists():
            self.speed_mult_btn.config(text=f"🚀 Mult: ×{mult}")
        if self._tele_vars and "speed_kmh" in self._tele_vars:
            self._tele_vars["speed_kmh"].set(f"{new_speed:.1f} km/h")
        self._update_status(f"Speed multiplier set to ×{mult}")
        if new_speed - old_speed >= SAFETY_CHECK_CONFIG["sudden_speed_jump_kmh"]:
            position = self._current_position()
            self._fire_safety_cycle(*position, speed_override=new_speed)

    # ── Map Rendering (Realistic Cartographic City Layers) ────────────────────

    def _draw_map(self):
        self.canvas.delete("all")
        sz = (CANVAS_W, CANVAS_H)

        # 1. Base Terrain
        self.canvas.create_rectangle(0, 0, CANVAS_W, CANVAS_H, fill="#0c1322", outline="")

        # 2. Coordinate Grid
        self._draw_grid()

        # 3. Waterway: Riverfront Marine Channel
        river_latlons = [
            (11.515, 79.768), (11.495, 79.756), (11.472, 79.744),
            (11.450, 79.739), (11.428, 79.733), (11.405, 79.728)
        ]
        river_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in river_latlons]
        flat_river = [c for pt in river_pixels for c in pt]
        if len(flat_river) >= 4:
            self.canvas.create_line(flat_river, fill="#0284c7", width=32, smooth=True)
            self.canvas.create_line(flat_river, fill="#034b75", width=26, smooth=True)
            rx, ry = river_pixels[len(river_pixels) // 2]
            self.canvas.create_text(rx + 16, ry - 10, text="≋ Riverfront Marine Channel ≋",
                                    fill="#38bdf8", font=("Segoe UI", 7, "italic"))

        # 4. Ecological Green Reserves
        # Sanctuary Nature Reserve (NW)
        p1 = map_config.latlon_to_pixel(11.508, 79.675, sz)
        p2 = map_config.latlon_to_pixel(11.478, 79.715, sz)
        self.canvas.create_rectangle(p1[0], p1[1], p2[0], p2[1], fill="#064e3b", outline="#059669", width=1)
        self.canvas.create_text((p1[0] + p2[0]) / 2, p1[1] + 12, text="🌲 Sanctuary Nature Reserve",
                                fill="#34d399", font=("Segoe UI", 8, "bold"))

        # University Botanical Campus (NE)
        bg1 = map_config.latlon_to_pixel(11.502, 79.750, sz)
        bg2 = map_config.latlon_to_pixel(11.486, 79.775, sz)
        self.canvas.create_rectangle(bg1[0], bg1[1], bg2[0], bg2[1], fill="#064e3b", outline="#059669", width=1)
        self.canvas.create_text((bg1[0] + bg2[0]) / 2, bg1[1] + 12, text="🌿 Botanical Campus Gardens",
                                fill="#34d399", font=("Segoe UI", 8, "bold"))

        # 5. Major Arterial: NH-45 Express Highway
        hway_pts = [
            (11.410, 79.670), (11.435, 79.700), (11.460, 79.728),
            (11.485, 79.752), (11.515, 79.780)
        ]
        hw_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in hway_pts]
        flat_hw = [c for pt in hw_pixels for c in pt]
        if len(flat_hw) >= 4:
            self.canvas.create_line(flat_hw, fill="#0b101d", width=14, smooth=True)
            self.canvas.create_line(flat_hw, fill="#f59e0b", width=9, smooth=True)
            self.canvas.create_line(flat_hw, fill="#ffffff", width=1.5, dash=(6, 4), smooth=True)
            hx, hy = hw_pixels[len(hw_pixels) // 2]
            self.canvas.create_rectangle(hx - 30, hy - 16, hx + 30, hy - 4, fill="#1e3a8a", outline="#3b82f6")
            self.canvas.create_text(hx, hy - 10, text="NH-45 EXPRESS", fill="#ffffff", font=("Segoe UI", 6, "bold"))

        # 6. City POIs & Landmarks
        city_pois = [
            ("🚇 Metro Central", 11.445, 79.708, "#38bdf8"),
            ("🏥 Apex Hospital", 11.455, 79.738, "#f43f5e"),
            ("👮 Police Precinct 4", 11.468, 79.712, "#60a5fa"),
            ("🎓 Campus Sanctuary", 11.4925, 79.758, "#c084fc"),
        ]
        for poi_name, p_lat, p_lon, poi_col in city_pois:
            px, py = map_config.latlon_to_pixel(p_lat, p_lon, sz)
            self.canvas.create_oval(px - 5, py - 5, px + 5, py + 5, fill=poi_col, outline="#ffffff", width=1)
            self.canvas.create_text(px, py + 11, text=poi_name, fill=poi_col, font=("Segoe UI", 7, "bold"))

        # 7. Safe Route Corridor
        self._draw_corridor_band(sz)

        # 8. Route Roads & Trail
        self._draw_roads()
        self._draw_trail()

        # 9. Predicted Point & User Marker
        self._draw_predicted_marker()
        self._draw_marker()

        # 10. Mode Badge in top-left
        badge_txt = "👁️ SHOW ONLY (NO ACTIONS)" if self.display_only_mode else "🛡️ ARMED ACTION MODE"
        badge_col = "#0284c7" if self.display_only_mode else "#991b1b"
        self.canvas.create_rectangle(8, 8, 175, 26, fill="#0f172a", outline=badge_col, width=1.5)
        self.canvas.create_text(91, 17, text=badge_txt, fill="#38bdf8" if self.display_only_mode else "#f87171",
                                font=("Segoe UI", 7, "bold"))

        # 11. On-Canvas D-Pad in bottom-right corner
        self._draw_canvas_dpad()
        self._draw_safety_banner()

    def _draw_safety_banner(self):
        self.canvas.delete("safety_check_banner")
        if self.safety_popup is None:
            return
        x1, y1, x2, y2 = 185, 8, CANVAS_W - 8, 30
        self.canvas.create_rectangle(
            x1, y1, x2, y2, fill="#7f1d1d", outline="#ef4444", width=1.5,
            tags="safety_check_banner"
        )
        self.canvas.create_text(
            (x1 + x2) / 2, (y1 + y2) / 2,
            text="SAFETY CHECK ACTIVE — ARE YOU SAFE?",
            fill="#fff1f2", font=("Segoe UI", 7, "bold"),
            tags="safety_check_banner"
        )

    def _draw_corridor_band(self, sz):
        """Draws the planned green safe corridor band towards active destination."""
        corridor_pts = self._get_safety_corridor_route()
        cp_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in corridor_pts]
        flat_cp = [c for pt in cp_pixels for c in pt]
        if len(flat_cp) >= 4:
            # Wide transparent-look green safe corridor band
            self.canvas.create_line(flat_cp, fill="#065f46", width=22, smooth=True)
            self.canvas.create_line(flat_cp, fill="#059669", width=5, smooth=True)
            # Waypoint markers
            for cx, cy in cp_pixels:
                self.canvas.create_rectangle(cx - 3, cy - 3, cx + 3, cy + 3, fill="#34d399", outline="#ffffff", width=1)

    def _get_safety_corridor_route(self):
        dest = getattr(self.controller, "trip_destination", None) if self.controller else None
        dlat = dest.get("latitude", 11.49250) if dest else 11.49250
        dlon = dest.get("longitude", 79.75800) if dest else 79.75800
        return [
            (11.45800, 79.72000), (11.46200, 79.72500),
            (11.47500, 79.73800), (11.48500, 79.75000), (dlat, dlon)
        ]

    def _draw_canvas_dpad(self):
        cx, cy = CANVAS_W - 42, CANVAS_H - 42
        r = 34
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#0f172a", outline="#38bdf8", width=1.5)
        self.canvas.create_oval(cx - 10, cy - 10, cx + 10, cy + 10, fill="#1e293b", outline="#0284c7")
        self.canvas.create_text(cx, cy - 20, text="▲ N", fill="#38bdf8", font=("Segoe UI", 6, "bold"))
        self.canvas.create_text(cx, cy + 20, text="▼ S", fill="#38bdf8", font=("Segoe UI", 6, "bold"))
        self.canvas.create_text(cx - 20, cy, text="◄ W", fill="#38bdf8", font=("Segoe UI", 6, "bold"))
        self.canvas.create_text(cx + 20, cy, text="► E", fill="#38bdf8", font=("Segoe UI", 6, "bold"))

    def _draw_grid(self):
        step = CANVAS_W // 10
        for i in range(0, CANVAS_W, step):
            self.canvas.create_line(i, 0, i, CANVAS_H, fill="#0f1f35", width=1)
        for i in range(0, CANVAS_H, step):
            self.canvas.create_line(0, i, CANVAS_W, i, fill="#0f1f35", width=1)

    def _draw_roads(self):
        if len(self.route_points) < 2:
            return
        pixel_pts = [map_config.latlon_to_pixel(lat, lon, (CANVAS_W, CANVAS_H))
                     for lat, lon in self.route_points]
        flat = [c for p in pixel_pts for c in p]
        if len(flat) >= 4:
            self.canvas.create_line(*flat, fill="#1e3a5f", width=7, smooth=True)
            self.canvas.create_line(*flat, fill=C_ROAD, width=3, smooth=True)

    def _draw_trail(self):
        if len(self._trail_coords) < 2:
            return
        flat = [c for pt in self._trail_coords for c in pt]
        if len(flat) >= 4:
            self.canvas.create_line(*flat, fill=C_TRAIL, width=3, smooth=True)

    def _draw_marker(self):
        if not self.route_points or self.current_index >= len(self.route_points):
            return
        lat, lon = self.route_points[self.current_index]
        sz = (CANVAS_W, CANVAS_H)
        px, py = map_config.latlon_to_pixel(lat, lon, sz)

        # Radar ripple rings
        pulse_col = "#f43f5e" if self._deviation_active else "#38bdf8"
        for r_off, w_stroke in [(20, 1), (13, 1.5)]:
            self.canvas.create_oval(px - r_off, py - r_off, px + r_off, py + r_off,
                                    outline=pulse_col, width=w_stroke, tags="marker")

        # Outer white ring
        self.canvas.create_oval(px - 9, py - 9, px + 9, py + 9, fill="#ffffff", outline="", tags="marker")

        # Inner GPS disc
        disc_col = "#ef4444" if self._deviation_active else "#0284c7"
        self.canvas.create_oval(px - 7, py - 7, px + 7, py + 7, fill=disc_col, outline="", tags="marker")

        # Heading pointer arrowhead
        rad = math.radians(self.user_heading_deg)
        tip_x = px + 8 * math.sin(rad)
        tip_y = py - 8 * math.cos(rad)
        bl_x  = px + 5 * math.sin(rad + 2.4)
        bl_y  = py - 5 * math.cos(rad + 2.4)
        br_x  = px + 5 * math.sin(rad - 2.4)
        br_y  = py - 5 * math.cos(rad - 2.4)
        self.canvas.create_polygon(tip_x, tip_y, bl_x, bl_y, px, py, br_x, br_y,
                                   fill="#ffffff", outline="", tags="marker")

        # HUD callout tag
        speed_val = self._base_speed * SPEED_PRESETS[self._speed_idx]
        st_name = self._get_street_name(lat, lon)
        tag_text = f"📍 YOU • {speed_val:.0f} km/h • {st_name}"
        tag_y = py - 22 if py > 40 else py + 24
        tag_w = max(len(tag_text) * 3.4 + 10, 50)
        self.canvas.create_rectangle(px - tag_w, tag_y - 7, px + tag_w, tag_y + 7,
                                     fill="#0f172a", outline="#0284c7", width=1.5, tags="marker")
        self.canvas.create_text(px, tag_y, text=tag_text, fill="#38bdf8",
                                font=("Segoe UI", 7, "bold"), tags="marker")

    def _draw_predicted_marker(self):
        if not self.controller or not hasattr(self.controller, "predictor"):
            return
        try:
            pred = self.controller.predictor.predict_next_coordinate()
            if pred and "predicted_latitude" in pred and "predicted_longitude" in pred:
                plat = pred["predicted_latitude"]
                plon = pred["predicted_longitude"]
                ppx, ppy = map_config.latlon_to_pixel(plat, plon, (CANVAS_W, CANVAS_H))
                self.canvas.create_oval(ppx - 8, ppy - 8, ppx + 8, ppy + 8,
                                        outline=C_PREDICT, width=2, tags="marker")
                self.canvas.create_text(ppx, ppy - 12, text="AI Predict",
                                        fill=C_PREDICT, font=("Segoe UI", 6, "bold"), tags="marker")
        except Exception:
            pass

    def _get_street_name(self, lat: float, lon: float) -> str:
        if self.controller and self.controller.gui_app and hasattr(self.controller.gui_app, "get_nearest_street_name"):
            return self.controller.gui_app.get_nearest_street_name(lat, lon)
        return "Botanical Garden Way"

    # ── Safety cycle dispatcher & Master Sync ──────────────────────────────────

    def _fire_safety_cycle(self, lat: float, lon, speed_override: float = None,
                           route_deviation_eligible: bool = False,
                           speed_anomaly_eligible: bool = False):
        mult = SPEED_PRESETS[self._speed_idx]
        speed = speed_override if speed_override is not None else (self._base_speed * mult)

        # Simulator UI movement remains visual-only except for deliberate
        # marker drags, which are debounced against the fixed planned route.
        route = self.safety_route_points or self._get_safety_corridor_route()
        self.safety_monitor.update(
            (lat, lon), speed, route,
            route_deviation_eligible=route_deviation_eligible,
            speed_anomaly_eligible=False,
        )

        if self.controller:
            self.controller.update_demo_position(lat, lon, speed_kmh=speed)

        # 2. Master Sync with Main GUI Dashboard
        is_threat = False
        threat_type = "NORMAL"
        if self.controller and self.controller.gui_app:
            gui = self.controller.gui_app
            gui.current_sim_lat = lat
            gui.current_sim_lon = lon
            gui.demo_speed = speed
            gui.user_heading_deg = self.user_heading_deg

            gui.update_map_canvas(lat, lon, None, speed, is_threat)

        # 3. Update Local Telemetry
        self._tele_vars["speed_kmh"].set(f"{speed:.1f} km/h")
        st_name = self._get_street_name(lat, lon)
        self._tele_vars["street_name"].set(st_name)

        if hasattr(self.controller, "predictor"):
            try:
                summary = self.controller.predictor.get_history_summary()
                seg_m   = summary.get("last_segment_m", 0.0)
                cum_km  = summary.get("cumulative_distance_km", 0.0)
                self._tele_vars["hav_dist"].set(f"{seg_m:.1f} m")
                self._tele_vars["cum_dist"].set(f"{cum_km:.3f} km")
                pred = self.controller.predictor.predict_next_coordinate()
                if pred:
                    self._tele_vars["pred_lat"].set(f"{pred.get('predicted_latitude', 0.0):.5f}")
                    self._tele_vars["pred_lon"].set(f"{pred.get('predicted_longitude', 0.0):.5f}")
                    self._tele_vars["confidence"].set(f"{pred.get('confidence', 0.0) * 100:.1f}%")
            except Exception:
                pass

        if is_threat:
            self._tele_vars["threat_status"].set("👁️ DEVIATION (SHOW ONLY)" if self.display_only_mode else f"⚠️ {threat_type}")
            self._update_status(f"⚠️ Anomaly: {threat_type} ({'Visual Only' if self.display_only_mode else 'Actions Armed'})", is_threat=True)
        else:
            self._tele_vars["threat_status"].set("👁️ SHOW ONLY" if self.display_only_mode else "● NORMAL")

    # ── Play / pause / step ───────────────────────────────────────────────────

    def _toggle_play(self):
        if self.is_playing:
            self._pause()
        else:
            self._play()

    def _play(self):
        if self.route_var.get() == CUSTOM_ROUTE_LABEL:
            self._update_status("Custom mode: click the map or drag to roam.")
            return
        if self.current_index >= len(self.route_points) - 1:
            self.current_index = 0
            self._trail_coords = []
        self.is_playing = True
        self.play_btn.config(text="⏸ Pause", bg=C_AMBER)
        self._tick()

    def _pause(self):
        self.is_playing = False
        if hasattr(self, "play_btn") and self.play_btn.winfo_exists():
            self.play_btn.config(text="▶ Play", bg=C_SAFE)
        if self.play_job:
            self._cancel_after(self.play_job)
            self.play_job = None

    def _tick(self):
        if not self.is_playing:
            return
        self._step_once()
        if self.current_index >= len(self.route_points) - 1:
            self._pause()
            self._update_status("✅ Route complete — all waypoints traversed.")
            return
        mult = SPEED_PRESETS[self._speed_idx]
        delay_ms = max(70, int(2200 / (self._base_speed * mult)))
        self.play_job = self._schedule_after(delay_ms, self._tick)

    def _step_once(self):
        if not self.route_points or self.current_index >= len(self.route_points) - 1:
            return
        self.current_index += 1
        lat, lon = self.route_points[self.current_index]

        # Calculate heading
        if self.current_index > 0:
            plat, plon = self.route_points[self.current_index - 1]
            dy = lat - plat
            dx = (lon - plon) * math.cos(math.radians(plat))
            if abs(dy) > 1e-6 or abs(dx) > 1e-6:
                self.user_heading_deg = math.degrees(math.atan2(dx, dy)) % 360

        px, py = map_config.latlon_to_pixel(lat, lon, (CANVAS_W, CANVAS_H))
        self._trail_coords.append((px, py))
        self._fire_safety_cycle(lat, lon)
        self._draw_map()
        self._update_wp_label()

    def _start_pulse_animation(self):
        def _tick():
            if self._closing or not self.win.winfo_exists():
                return
            self._pulse_phase = (self._pulse_phase + 0.18) % (2 * math.pi)
            self.canvas.delete("marker")
            self._draw_marker()
            self._draw_predicted_marker()
            self._update_network_status()
            self._pulse_job = self._schedule_after(60, _tick)
        self._pulse_job = self._schedule_after(60, _tick)

    def _update_network_status(self):
        if self.controller and hasattr(self.controller, "phone_has_signal"):
            has_sig = self.controller.phone_has_signal
            self._tele_vars["network"].set("Connected ✅" if has_sig else "Offline ⚠️")

    def _load_route(self):
        self._trail_coords = []
        if self.route_var.get() == CUSTOM_ROUTE_LABEL:
            self.route_points = []
            self._update_status("Custom mode: click/drag map to move freely.")
        else:
            self.route_points = list(map_config.get_dynamic_routes(self.controller).get(self.route_var.get(), []))
            if self.route_points:
                map_config.auto_center_for_points(self.route_points)
            self._update_status("Route loaded. Click ▶ Play or roam freely on map.")
        self.safety_route_points = list(self.route_points) if len(self.route_points) >= 2 else self._get_safety_corridor_route()
        if self.controller and hasattr(self.controller, "anomaly_engine"):
            self.controller.anomaly_engine.set_planned_route(self.safety_route_points)
        self.current_index   = 0
        self.simulated_clock = time.time()
        self._deviation_active = False
        if self.controller and getattr(self.controller, "gui_app", None):
            self.controller.gui_app.clear_map_display()
        self._draw_map()
        self._update_wp_label()

    def _on_route_change(self):
        self._pause()
        self._load_route()

    def _reset_route(self):
        self._pause()
        if self.route_var.get() == CUSTOM_ROUTE_LABEL:
            self.route_points = []
        self.current_index   = 0
        self.simulated_clock = time.time()
        self._trail_coords   = []
        self._deviation_active = False
        if hasattr(self, "btn_deviate") and self.btn_deviate.winfo_exists():
            self.btn_deviate.config(text="↗ Deviate", bg=C_DEVIATE)
        self._draw_map()
        self._update_wp_label()
        self._update_status("Route reset.")
        if self.controller and self.controller.gui_app:
            self.controller.gui_app.clear_map_display()

    def _update_status(self, extra: str = "", is_threat: bool = False):
        total = max(len(self.route_points) - 1, 0)
        self.status_var.set(f"WP {self.current_index}/{total} • {extra}")
        if is_threat:
            self.status_lbl.configure(fg=C_THREAT)
        else:
            self.status_lbl.configure(fg=C_TEXT_MID)
        self._update_wp_label()

    def _update_wp_label(self):
        total = max(len(self.route_points) - 1, 0)
        self._tele_vars["wp_index"].set(f"{self.current_index} / {total}")

    def _on_close(self):
        self._pause()
        self._close_safety_popup()
        self._closing = True
        for job_id in tuple(self._after_ids):
            try:
                self.win.after_cancel(job_id)
            except tk.TclError:
                pass
        self._after_ids.clear()
        if self.controller and getattr(self.controller, "map_simulator_window", None) is self:
            self.controller.map_simulator_window = None
        self.win.destroy()


# ── Public factory ─────────────────────────────────────────────────────────────

def open_map_simulator(parent_root, controller) -> MapSimulatorWindow:
    """Open the interactive map simulator window and return the instance."""
    sim = MapSimulatorWindow(parent_root, controller)
    if not hasattr(sim, "speed_var"):
        sim.speed_var = _IntProxy(4)
    sim._base_speed = 45.0
    if controller is not None:
        controller.map_simulator_window = sim
    return sim
