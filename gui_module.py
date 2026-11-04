import os
import glob
import re
import json
import time
import math
import platform
import threading
import subprocess
import wave
import tkinter as tk
from tkinter import messagebox, ttk
from datetime import datetime
from acoustic_module import OfflineAcousticEngine
import map_simulator
import map_config
import settings_module

is_emergency_active = False

# Temporarily bypass the persisted armed-session shortcut so each launch opens
# the Login / Setup screen. Saved profile data remains available for setup.
AUTO_LOGIN_ENABLED = False

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

try:
    from plyer import notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import winsound
    WINSOUND_AVAILABLE = True
except ImportError:
    WINSOUND_AVAILABLE = False


# ── Colour palette (single source of truth) ────────────────────────────────────
_P = {
    # Backgrounds
    "bg_root":    "#060b14",
    "bg_panel":   "#0d1421",
    "bg_card":    "#111827",
    "bg_card2":   "#0f1929",
    "bg_input":   "#1a2540",

    # Borders / accents
    "border":     "#1e3a5f",
    "border2":    "#243552",
    "accent":     "#38bdf8",      # sky-400
    "accent2":    "#818cf8",      # indigo-400
    "green":      "#10b981",      # emerald-500
    "amber":      "#f59e0b",      # amber-500
    "red":        "#ef4444",      # red-500
    "purple":     "#a855f7",      # purple-500
    "predict":    "#facc15",      # yellow prediction markers

    # Text
    "text_hi":    "#f1f5f9",
    "text_bright":"#f8fafc",
    "text_mid":   "#94a3b8",
    "text_dim":   "#475569",

    # Threat
    "threat_bg":  "#450a0a",
    "threat_fg":  "#fca5a5",

    # SOS button
    "sos_bg":     "#b91c1c",
    "sos_hover":  "#ef4444",
}


class ModernSafetyApp:
    def __init__(self, root, controller=None):
        self.root       = root
        self.controller = controller

        self.root.title("AURA — AI Emergency Safety System")
        self.root.geometry("1280x760")
        self.root.minsize(1100, 680)
        self.root.configure(bg=_P["bg_root"])
        self.root.resizable(True, True)

        self.is_fake_shutdown = False
        self.root.bind("<F8>", lambda event: self.trigger_fake_shutdown())
        self.root.bind("<F12>", lambda event: self.trigger_threat("🚨 MANUAL EMERGENCY (F12 HOTKEY)"))
        self.root.bind("<Control-s>", lambda event: self.trigger_threat("🚨 MANUAL EMERGENCY (HOTKEY)"))
        self.root.bind("<Control-S>", lambda event: self.trigger_threat("🚨 MANUAL EMERGENCY (HOTKEY)"))



        os.makedirs(os.path.join("recordings", "audio"), exist_ok=True)
        os.makedirs(os.path.join("recordings", "video"), exist_ok=True)
        project_root = os.path.dirname(os.path.abspath(__file__))
        self.vault_audio_dir = os.path.join(project_root, "data", "vault", "audio")
        self.vault_video_dir = os.path.join(project_root, "data", "vault", "video")
        os.makedirs(self.vault_audio_dir, exist_ok=True)
        os.makedirs(self.vault_video_dir, exist_ok=True)
        self._vault_playback_job = None
        os.makedirs("assets", exist_ok=True)
        os.makedirs("data", exist_ok=True)

        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.init()
            except Exception as e:
                print(f"Audio Mixer Warning: {e}")

        # ── User profile state ────────────────────────────────────────────────
        self.user_name   = ""
        self.user_phone  = ""
        self.contacts    = []    # [{name, phone}, …] — index 0 = P1
        self.safe_locations = []
        self.delay_timer = tk.IntVar(value=15)
        self.enable_usb_dispatch = tk.BooleanVar(value=True)
        self.contact_rows = []

        # ── Extended Safety Configuration State ──────────────────────────────
        self.escalation_timeout_var = tk.IntVar(value=60)   # Configurable 20 - 120s
        self.speed_threshold_var    = tk.IntVar(value=25)   # Speed anomaly threshold (km/h)
        self.active_threat_reason   = ""

        # ── Predefined Destination & Free Roam Map Movement State ────────────
        self.destination_status_str = tk.StringVar(value="🏁 Destination: None")
        self.demo_status_str        = tk.StringVar(value="🎮 Free Roam: ACTIVE")
        self.escalation_status_str  = tk.StringVar(value="")
        self.demo_mode_active       = True
        self.demo_loop_job          = None
        self.demo_step_index        = 0
        self.demo_speed             = 45.0
        self.roam_pace_mode         = "Drive"
        self.current_sim_lat        = map_config.CENTER_LAT
        self.current_sim_lon        = map_config.CENTER_LON
        self.user_heading_deg       = 45.0
        self.is_dragging_user       = False
        self._last_move_time        = time.time()
        self._map_base_drawn        = False
        self.demo_coords            = []

        self.display_only_mode      = False
        if self.controller:
            self.controller.display_only_mode = self.display_only_mode

        self.p1_entry = None
        self.p2_entry = None

        self.time_left        = 15
        self.total_time       = 15
        self.is_threat_active = False
        # Explicit popup lifecycle flags. These describe the confirmation
        # popup only; the emergency dispatch state has its own flag above.
        self.alert_active     = False
        self.popup_showing    = False
        self.is_emergency_active = False
        self.timer_job        = None
        self.threat_popup     = None
        self.timer_canvas     = None
        self._emergency_banner_frame = None
        self._emergency_overlay = None
        self._emergency_flash_job = None
        self._emergency_flash_on = False
        self._popup_chime_stop = threading.Event()

        self.is_protection_active  = False
        self.low_battery_alert_sent = False

        self.battery_status_str = tk.StringVar(value="🔋 Battery: 100%")
        self.signal_status_str  = tk.StringVar(value="📶 Signal: Checking…")
        self.usb_dispatch_status_str = tk.StringVar(value="Checking ADB connection…")
        self.usb_dispatch_status_color = "#64748b"
        self.speed_indicator_str = tk.StringVar(value="Speed: -- km/h")

        self._tele_speed    = tk.StringVar(value="-- km/h")
        self._tele_seg_dist = tk.StringVar(value="-- m")
        self._tele_cum_dist = tk.StringVar(value="-- km")
        self._tele_pred_lat = tk.StringVar(value="--")
        self._tele_pred_lon = tk.StringVar(value="--")
        self._tele_conf     = tk.StringVar(value="--")
        self._tele_network  = tk.StringVar(value="Checking…")
        self._tele_threat   = tk.StringVar(value="● MONITORING")
        self._tele_dest     = tk.StringVar(value="None")

        self.acoustic_engine = None

        self.main_container = tk.Frame(self.root, bg=_P["bg_root"])
        self.main_container.pack(fill="both", expand=True)

        self.root.bind_all("<Escape>", lambda e: self.dismiss_alarm("ESC Key Pressed"))

        self.map_trail_points = []

        self.profile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), settings_module.PROFILE_PATH
        )
        self._alert_overlay = None
        self._usb_was_linked = None
        self.load_user_profile()

        threading.Thread(target=self.monitor_battery_status, daemon=True).start()
        self.show_splash_screen()

    def load_user_profile(self):
        """Restores contacts and thresholds from local JSON (never used to pre-fill setup fields)."""
        try:
            settings_module.apply_profile_to_app(self)
        except Exception as e:
            print(f"[Profile Load Warning] {e}")

    def save_user_profile(self):
        """Save the profile and emergency contacts to the local JSON file."""
        try:
            settings_module.save_user_profile(
                settings_module.collect_profile_from_app(self),
                path=self.profile_path,
            )
            print("💾 [Profile] User profile saved successfully.")
            return True
        except Exception as e:
            print(f"[Profile Save Warning] {e}")
            return False

    def send_desktop_popup(self, title, message):
        """Dispatches an audible alert and an OS desktop notification toast with rate limiting."""
        now = time.time()
        if not hasattr(self, "_popup_history"):
            self._popup_history = {}
        last_time = self._popup_history.get(title, 0)

        if now - last_time < 10.0:
            return
        self._popup_history[title] = now

        def _beep():
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
        threading.Thread(target=_beep, daemon=True).start()

        if PLYER_AVAILABLE:
            def _toast():
                try:
                    notification.notify(title=title, message=message,
                                        app_name="AURA Safety Engine", timeout=5)
                except Exception:
                    pass
            threading.Thread(target=_toast, daemon=True).start()

    def clear_container(self):
        for w in self.main_container.winfo_children():
            w.destroy()

    def clean_phone(self, phone_str):
        if not phone_str:
            return ""
        digits = re.sub(r"\D", "", str(phone_str))
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        return digits

    def is_valid_phone(self, phone_str):
        cleaned = self.clean_phone(phone_str)
        return len(cleaned) == 10 or (10 <= len(cleaned) <= 15)

    def _add_placeholder(self, entry, placeholder,
                         color=_P["text_dim"], real_color=_P["text_hi"]):
        entry.insert(0, placeholder)
        entry.config(fg=color)
        entry._placeholder = placeholder
        entry._is_placeholder = True

        def on_focus_in(e):
            if getattr(entry, "_is_placeholder", False):
                entry.delete(0, tk.END)
                entry.config(fg=real_color)
                entry._is_placeholder = False

        def on_focus_out(e):
            if not entry.get().strip():
                entry.insert(0, placeholder)
                entry.config(fg=color)
                entry._is_placeholder = True

        entry.bind("<FocusIn>",  on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    @staticmethod
    def _entry_value_or_blank(entry):
        if getattr(entry, "_is_placeholder", False):
            return ""
        return entry.get().strip()

    def _glowing_label(self, parent, text, font_size=8, color=None,
                       bg=None, **kwargs):
        """A label styled as a glowing status badge."""
        c = color or _P["accent"]
        b = bg    or _P["bg_card"]
        return tk.Label(
            parent, text=text,
            font=("Segoe UI", font_size, "bold"),
            fg=c, bg=b,
            padx=6, pady=2,
            **kwargs,
        )

    def _card(self, parent, accent_color=_P["border2"]):
        """Returns a 2-pixel left-border card. Content goes in .content."""
        outer = tk.Frame(parent, bg=_P["bg_card"],
                         highlightbackground=_P["border"],
                         highlightthickness=1)
        strip = tk.Frame(outer, bg=accent_color, width=3)
        strip.pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=_P["bg_card"])
        inner.pack(side="left", fill="both", expand=True, padx=(10, 8), pady=8)
        outer.content = inner
        return outer

    def _section_header(self, parent, icon, title, color):
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill="x", anchor="w", pady=(0, 6))
        badge = tk.Canvas(row, width=22, height=22,
                          bg=parent["bg"], highlightthickness=0)
        badge.pack(side="left", padx=(0, 6))
        badge.create_oval(1, 1, 21, 21, fill=color, outline="")
        badge.create_text(11, 11, text=icon, font=("Segoe UI", 10))
        tk.Label(row, text=title, font=("Segoe UI", 8, "bold"),
                 fg=color, bg=parent["bg"]).pack(side="left")
        return row

    def _circle_icon_button(self, parent, icon, caption, command,
                             bg=_P["bg_card"], fg=_P["accent"], size=40):
        wrap = tk.Frame(parent, bg=parent["bg"])
        c = tk.Canvas(wrap, width=size, height=size,
                      bg=parent["bg"], highlightthickness=0, cursor="hand2")
        c.pack()
        circle   = c.create_oval(2, 2, size - 2, size - 2, fill=bg, outline="")
        label_id = c.create_text(size / 2, size / 2, text=icon,
                                  font=("Segoe UI", int(size * 0.38)), fill=fg)
        tk.Label(wrap, text=caption, font=("Segoe UI", 7),
                 fg=_P["text_dim"], bg=parent["bg"]).pack(pady=(2, 0))

        def _click(e):  command()
        def _hin(e):
            c.itemconfig(circle, fill=fg)
            c.itemconfig(label_id, fill=_P["bg_root"])
        def _hout(e):
            c.itemconfig(circle, fill=bg)
            c.itemconfig(label_id, fill=fg)

        c.bind("<Button-1>", _click)
        c.bind("<Enter>",    _hin)
        c.bind("<Leave>",    _hout)
        return wrap

    def show_splash_screen(self):
        self.clear_container()
        splash = tk.Frame(self.main_container, bg=_P["bg_root"])
        splash.pack(fill="both", expand=True)

        canvas = tk.Canvas(splash, width=340, height=340,
                           bg=_P["bg_root"], highlightthickness=0)
        canvas.pack(pady=(40, 10))

        # Static vector logo
        canvas.create_oval(60, 60, 280, 280, fill="#1e1b4b",
                           outline="#6366f1", width=2)
        canvas.create_oval(130, 90, 210, 170, fill="#f472b6", outline="")
        canvas.create_polygon(105, 250, 170, 140, 235, 250,
                              fill="#ec4899", outline="")
        canvas.create_text(170, 200, text="AURA",
                           font=("Segoe UI", 22, "bold"), fill="#ffffff")
        canvas.create_text(170, 230, text="SAFETY ENGINE",
                           font=("Segoe UI", 9, "bold"), fill=_P["accent"])

        tk.Label(splash, text="AURA SAFETY SYSTEM",
                 font=("Segoe UI", 26, "bold"),
                 fg=_P["accent"], bg=_P["bg_root"]).pack(pady=(0, 4))
        tk.Label(splash, text="Offline Edge AI  •  Realtime Location Intel  •  Emergency Dispatch",
                 font=("Segoe UI", 10), fg=_P["text_mid"],
                 bg=_P["bg_root"]).pack(pady=(0, 14))

        # Animated spinner
        self._spinner_angle  = 0
        self._spinner_active = True

        def _spin():
            if not self._spinner_active:
                return
            canvas.delete("spin")
            canvas.create_oval(24, 24, 316, 316, outline="#1e293b",
                               width=5, tags="spin")
            canvas.create_arc(24, 24, 316, 316,
                              start=self._spinner_angle, extent=100,
                              outline=_P["accent"], width=5,
                              style="arc", tags="spin")
            # Second arc for double-helix look
            canvas.create_arc(24, 24, 316, 316,
                              start=self._spinner_angle + 180, extent=60,
                              outline=_P["accent2"], width=3,
                              style="arc", tags="spin")
            self._spinner_angle = (self._spinner_angle + 8) % 360
            self.root.after(28, _spin)

        _spin()

        def _launch():
            time.sleep(2.0)
            self._spinner_active = False
            try:
                if AUTO_LOGIN_ENABLED and self.is_protection_active and self.contacts:
                    self.root.after(0, self._restore_armed_profile)
                else:
                    self.root.after(0, self.build_setup_screen)
            except Exception:
                pass

        threading.Thread(target=_launch, daemon=True).start()

    def _restore_armed_profile(self):
        """Restore the voice listener and contacts together with the saved armed state."""
        try:
            self._activate_voice_recognition()
        except Exception as exc:
            print(f"❌ [Voice] Could not restore armed voice recognition: {exc}")
        self.start_safety_monitoring_loop()
        self.build_modern_dashboard(
            pending_notification="✅ System Armed (Restored from Profile)"
        )

    def _activate_voice_recognition(self):
        if self.controller:
            numbers = [
                contact.get("phone", "") if isinstance(contact, dict) else str(contact)
                for contact in self.contacts
            ]
            self.controller.set_emergency_contacts([number for number in numbers if number])
            self.controller.start_voice_recognition(armed=True)
            self.acoustic_engine = self.controller.acoustic_engine
        else:
            if self.acoustic_engine is None:
                self.acoustic_engine = OfflineAcousticEngine(
                    trigger_callback=self.handle_voice_event
                )
            self.acoustic_engine.start_listening()
            self.acoustic_engine.set_setup_complete(True)

    def build_setup_screen(self):
        self.clear_container()

        setup_card = tk.Frame(
            self.main_container, bg=_P["bg_panel"],
            highlightbackground=_P["border"], highlightthickness=1,
        )
        setup_card.place(relx=0.5, rely=0.5, anchor="center",
                         width=560, height=620)

        hdr = tk.Frame(setup_card, bg=_P["bg_panel"])
        hdr.pack(fill="x", padx=32, pady=(28, 6))
        tk.Label(hdr, text="🛡️  AURA SAFETY ENGINE SETUP",
                 font=("Segoe UI", 15, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(anchor="w")
        sub_desc = "AI-Powered Offline Edge Emergency Protection  •  New configuration (fields stay blank)"
        tk.Label(hdr, text=sub_desc,
                 font=("Segoe UI", 9), fg=_P["text_dim"],
                 bg=_P["bg_panel"]).pack(anchor="w", pady=(2, 10))

        frm = tk.Frame(setup_card, bg=_P["bg_panel"])
        frm.pack(fill="both", expand=True, padx=32)

        def _lbl(text, color=_P["accent2"]):
            tk.Label(frm, text=text, font=("Segoe UI", 8, "bold"),
                     fg=color, bg=_P["bg_panel"]).pack(anchor="w", pady=(8, 2))

        def _entry(**kw):
            e = tk.Entry(frm, font=("Segoe UI", 11),
                         bg=_P["bg_input"], fg=_P["text_hi"],
                         insertbackground=_P["accent"],
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=_P["border"],
                         highlightcolor=_P["accent"], **kw)
            e.pack(fill="x", ipady=9, pady=(0, 4))
            return e

        _lbl("FULL NAME")
        e_name = _entry()

        _lbl("MOBILE NUMBER  (10 digits)")
        e_phone = _entry()

        # Emergency contacts
        contacts_hdr = tk.Frame(frm, bg=_P["bg_panel"])
        contacts_hdr.pack(fill="x", pady=(10, 4))
        tk.Label(contacts_hdr, text="EMERGENCY CONTACTS  (P1 = highest priority)",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(side="left")

        actions_box = tk.Frame(contacts_hdr, bg=_P["bg_panel"])
        actions_box.pack(side="right")

        def _clear_all_fields():
            e_name.delete(0, tk.END)
            e_phone.delete(0, tk.END)
            for r in list(self.contact_rows):
                r["frame"].destroy()
            self.contact_rows.clear()
            _add_contact_row()

        tk.Button(actions_box, text="Clear",
                  font=("Segoe UI", 8),
                  bg=_P["bg_card"], fg=_P["text_mid"],
                  bd=0, cursor="hand2", padx=8, pady=2,
                  command=_clear_all_fields).pack(side="left", padx=(0, 6))

        tk.Button(actions_box, text="+ Add",
                  font=("Segoe UI", 8, "bold"),
                  bg=_P["accent"], fg=_P["bg_root"],
                  bd=0, cursor="hand2", padx=10, pady=2,
                  command=lambda: _add_contact_row()).pack(side="left")

        contacts_box = tk.Frame(frm, bg=_P["bg_panel"])
        contacts_box.pack(fill="x")

        self.contact_rows = []

        def _add_contact_row(init_name="", init_phone=""):
            row = tk.Frame(contacts_box, bg=_P["bg_panel"])
            row.pack(fill="x", pady=3)

            e_cn = tk.Entry(row, font=("Segoe UI", 10),
                            bg=_P["bg_input"], fg=_P["text_hi"],
                            insertbackground=_P["accent"], bd=0,
                            highlightthickness=1,
                            highlightbackground=_P["border"],
                            highlightcolor=_P["accent"])
            e_cn.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 4))
            if init_name:
                e_cn.insert(0, init_name)
                e_cn._is_placeholder = False
            else:
                self._add_placeholder(e_cn, "Contact Name")

            e_cp = tk.Entry(row, font=("Segoe UI", 10),
                            bg=_P["bg_input"], fg=_P["text_hi"],
                            insertbackground=_P["accent"], bd=0,
                            highlightthickness=1,
                            highlightbackground=_P["border"],
                            highlightcolor=_P["accent"])
            e_cp.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 4))
            if init_phone:
                e_cp.insert(0, init_phone)
                e_cp._is_placeholder = False
            else:
                self._add_placeholder(e_cp, "10-Digit Mobile")

            def _del():
                self.contact_rows = [r for r in self.contact_rows
                                     if r["frame"] is not row]
                row.destroy()

            tk.Button(row, text="✖", font=("Segoe UI", 9, "bold"),
                      bg=_P["red"], fg="#fff", bd=0, cursor="hand2",
                      width=3, command=_del).pack(side="right", ipady=4)

            self.contact_rows.append({"frame": row,
                                       "name_entry": e_cn,
                                       "phone_entry": e_cp})

        # Setup fields stay blank for a new configuration (do not restore prior contacts).
        _add_contact_row()

        def _do_setup():
            n = e_name.get().strip()
            p = e_phone.get().strip()
            if not n:
                messagebox.showerror("Setup Error", "Please enter your Full Name.")
                return
            if not self.is_valid_phone(p):
                messagebox.showerror("Setup Error",
                                     "Invalid Mobile Number! Enter a valid 10-digit number.")
                return

            parsed = []
            for row in self.contact_rows:
                cn = self._entry_value_or_blank(row["name_entry"])
                cp = self._entry_value_or_blank(row["phone_entry"])
                if cn and cp:
                    if not self.is_valid_phone(cp):
                        messagebox.showerror(
                            "Validation Error",
                            f"Contact number '{cp}' must be exactly 10 digits.")
                        return
                    parsed.append({"name": cn, "phone": cp})

            if not parsed:
                messagebox.showerror("Setup Error",
                                     "Add at least one valid 10-digit Emergency Contact.")
                return

            self.user_name  = n
            self.user_phone = p
            self.contacts   = parsed
            self.is_protection_active = True
            if not self.save_user_profile():
                self.is_protection_active = False
                messagebox.showerror(
                    "Setup Error",
                    f"Could not save your profile to:\n{self.profile_path}\n\nCheck that the data folder is writable and try again.",
                )
                return

            try:
                if self.controller:
                    self.controller.set_emergency_contacts(
                        [c["phone"] for c in parsed])
                self._activate_voice_recognition()
            except Exception as exc:
                print(f"❌ [Setup] Protection startup warning: {exc}")
                messagebox.showwarning(
                    "Protection Startup Warning",
                    f"Your profile was saved, but a protection service did not start:\n{exc}\n\nThe dashboard will still open.",
                )
            try:
                self.start_safety_monitoring_loop()
            except Exception as exc:
                print(f"❌ [Setup] Safety monitor startup warning: {exc}")
            # Replacing the setup contents with the dashboard completes activation
            # even if an optional voice or monitoring service reported an error.
            self.build_modern_dashboard(
                pending_notification="✅ System Armed & Protection Active")
            self.root.after_idle(self.open_map_simulator)

        tk.Button(
            setup_card,
            text="ACTIVATE SYSTEM PROTECTION  ➔",
            font=("Segoe UI", 10, "bold"),
            bg=_P["green"], fg="#ffffff", bd=0, cursor="hand2",
            command=_do_setup,
        ).pack(fill="x", padx=32, ipady=12, pady=(14, 28))

    # ── Backend monitoring loop ───────────────────────────────────────────────

    def start_safety_monitoring_loop(self):
        if getattr(self, "_monitoring_loop_started", False):
            return
        self._monitoring_loop_started = True

        def cycle():
            if self.is_protection_active and self.controller:
                # Do not run background inspection if an emergency threat is currently active on screen
                if not getattr(self, "is_threat_active", False):
                    try:
                        self.controller.run_live_safety_cycle()
                    except Exception as e:
                        print(f"[Safety Cycle Error] {e}")
            try:
                self.root.after(3000, cycle)
            except Exception:
                pass
        cycle()

    # ── Battery monitoring ────────────────────────────────────────────────────

    def monitor_battery_status(self):
        while True:
            if self.is_protection_active:
                pct = None
                power_plugged = False
                if PSUTIL_AVAILABLE:
                    battery = psutil.sensors_battery()
                    if battery:
                        pct = battery.percent
                        power_plugged = battery.power_plugged

                # Also inspect phone battery from external sensor diagnostics
                if self.controller and hasattr(self.controller, "sensor_diagnostics"):
                    phone_bat = self.controller.sensor_diagnostics.external_sensor_data.get("phone_battery")
                    if phone_bat is not None:
                        pct = phone_bat

                if pct is not None:
                    self.battery_status_str.set(f"🔋 Battery: {pct:.0f}%")
                    # When battery reaches 15% or below and on battery: send separate warning SMS (NOT SOS)
                    if pct <= 15 and not power_plugged and not self.low_battery_alert_sent:
                        self.low_battery_alert_sent = True
                        self.root.after(0, lambda p=pct: self.trigger_low_battery_alert(p))
                    elif pct > 20 or power_plugged:
                        # Reset warning state when recharged or plugged in
                        self.low_battery_alert_sent = False

                # Update mobile phone connection state
                if self.controller and hasattr(self.controller, "telephony_manager"):
                    try:
                        p_info = self.controller.telephony_manager.check_phone_connection()
                        if p_info.get("device_connected"):
                            model = p_info.get("device_model", "Phone")
                            self.signal_status_str.set(f"📱 Phone: {model}")
                        else:
                            net_ok = hasattr(self.controller, "network_monitor") and self.controller.network_monitor.is_connected
                            if net_ok:
                                self.signal_status_str.set("📶 Signal: Connected")
                            else:
                                self.signal_status_str.set("📱 Phone: Disconnected")
                    except Exception:
                        pass
            time.sleep(10)

    def trigger_low_battery_alert(self, percent):
        """Dispatches separate Low Battery Warning SMS to guardians. Does NOT trigger SOS."""
        msg = f"🔋 Low battery warning ({percent:.0f}%)"
        self.send_desktop_popup(
            "🔋 Low Battery Warning",
            f"Battery is at {percent:.0f}%. Low Battery Warning SMS sent to saved contacts with current location."
        )
        if self.controller:
            try:
                loc = self.controller.location_engine.get_current_location()
                self.controller.send_low_battery_notice(percent, loc)
            except Exception as e:
                print(f"[Low Battery Notice Error] {e}")
        self.build_modern_dashboard(pending_notification=msg, is_warning=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # 3 – SYSTEM DISARM CONFIRMATION
    # ═══════════════════════════════════════════════════════════════════════════

    def open_deactivation_dialog(self):
        if not messagebox.askyesno("Disarm protection", "Turn off AURA protection for this session?"):
            return
        self.dismiss_alarm("Protection manually disarmed")
        self.is_protection_active = False
        if self.controller:
            self.controller.system_status = "DEACTIVATED"
        self.build_setup_screen()

    def trigger_stealth_fake_shutdown(self):
        self.clear_container()
        self.root.attributes("-fullscreen", True)
        self.root.config(cursor="none")
        stealth = tk.Canvas(self.main_container,
                            bg="#000000", highlightthickness=0)
        stealth.pack(fill="both", expand=True)
        if self.controller:
            self.controller.start_emergency_recording()
        self.send_desktop_popup("AURA Stealth Engine",
                                "Pitch Black Mode. Covert tracking active.")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4 – MAIN DASHBOARD
    # ═══════════════════════════════════════════════════════════════════════════

    def build_modern_dashboard(self, pending_notification=None,
                                is_error=False, is_warning=False,
                                impact_alert=None):
        self.clear_container()
        self._emergency_banner_frame = None
        self.map_trail_points = []

        # Entering user dashboard defaults map to Display Only mode (Show Only — no actions taken)
        self.display_only_mode = True
        if self.controller:
            self.controller.display_only_mode = True

        # ── Top navigation bar ────────────────────────────────────────────────
        topbar = tk.Frame(self.main_container, bg=_P["bg_panel"],
                          padx=14, pady=9,
                          highlightbackground=_P["border"],
                          highlightthickness=1)
        topbar.pack(fill="x")

        # Logo + title
        logo_row = tk.Frame(topbar, bg=_P["bg_panel"])
        logo_row.pack(side="left")
        logo_c = tk.Canvas(logo_row, width=26, height=26,
                           bg=_P["bg_panel"], highlightthickness=0)
        logo_c.pack(side="left", padx=(0, 8))
        logo_c.create_oval(1, 1, 25, 25, fill=_P["accent2"], outline="")
        logo_c.create_text(13, 13, text="🛡", font=("Segoe UI", 12))
        tk.Label(logo_row, text="AURA SAFETY DASHBOARD",
                 font=("Segoe UI", 13, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(side="left")

        # Right side icons
        icon_row = tk.Frame(topbar, bg=_P["bg_panel"])
        icon_row.pack(side="right")
        self._circle_icon_button(
            icon_row, "🔒", "Disarm", self.open_deactivation_dialog,
            bg=_P["bg_card"], fg=_P["red"]).pack(side="right", padx=4)
        self._circle_icon_button(
            icon_row, "⚙️", "Settings", self.open_settings_dialog,
            bg=_P["bg_card"], fg=_P["accent"]).pack(side="right", padx=4)

        # Status badges
        badge_row = tk.Frame(topbar, bg=_P["bg_panel"])
        badge_row.pack(side="right", padx=10)
        for txt, fg in [
            (self.battery_status_str.get(),  _P["amber"]),
            ("🗂️ Vault: ON",                  _P["green"]),
            ("🛰️ Blackbox: ACTIVE",           _P["accent"]),
        ]:
            tk.Label(badge_row, text=txt,
                     font=("Segoe UI", 8, "bold"),
                     fg=fg, bg=_P["bg_card"],
                     padx=8, pady=3).pack(side="left", padx=3)

        # ── Alert banners ─────────────────────────────────────────────────────
        if impact_alert:
            bar = tk.Frame(self.main_container, bg="#991b1b", pady=6)
            bar.pack(fill="x")
            tk.Label(bar, text=f"💥 IMPACT: {impact_alert}",
                     font=("Segoe UI", 9, "bold"),
                     fg="#fff", bg="#991b1b").pack()

        if pending_notification:
            bg_c = "#450a0a" if is_error else ("#78350f" if is_warning else "#064e3b")
            fg_c = _P["red"] if is_error else ("#fde047" if is_warning else _P["green"])
            bar = tk.Frame(self.main_container, bg=bg_c, pady=5)
            bar.pack(fill="x")
            tk.Label(bar, text=pending_notification,
                     font=("Segoe UI", 9, "bold"),
                     fg=fg_c, bg=bg_c).pack()
            if is_error and "EMERGENCY ALERT DISPATCHED" in str(pending_notification).upper():
                self._emergency_banner_frame = bar

        # ── Body ──────────────────────────────────────────────────────────────
        body = tk.Frame(self.main_container, bg=_P["bg_root"])
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # Left column
        left = tk.Frame(body, bg=_P["bg_root"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._build_left_column(left)

        # Centre column (map + actions)
        centre = tk.Frame(body, bg=_P["bg_root"])
        centre.pack(side="left", fill="both", expand=True, padx=6)
        self._build_centre_column(centre)

        # Right column (telemetry sidebar)
        right = tk.Frame(body, bg=_P["bg_root"])
        right.pack(side="right", fill="both", padx=(6, 0))
        self._build_telemetry_sidebar(right)

    # ── Left column ───────────────────────────────────────────────────────────

    def _build_left_column(self, parent):
        # Profile card
        pc = self._card(parent, accent_color=_P["accent2"])
        pc.pack(fill="x", pady=(0, 8))
        avatar = tk.Canvas(pc.content, width=42, height=42,
                           bg=_P["bg_card"], highlightthickness=0)
        avatar.pack(side="left", padx=(0, 10))
        init = (self.user_name[:1] or "A").upper()
        avatar.create_oval(1, 1, 41, 41, fill="#6366f1", outline="")
        avatar.create_text(21, 21, text=init,
                           font=("Segoe UI", 16, "bold"), fill="#fff")

        pinfo = tk.Frame(pc.content, bg=_P["bg_card"])
        pinfo.pack(side="left", fill="x", expand=True)
        tk.Label(pinfo, text=f"{self.user_name}  •  {self.user_phone}",
                 font=("Segoe UI", 10, "bold"),
                 fg=_P["text_hi"], bg=_P["bg_card"]).pack(anchor="w")
        tk.Label(pinfo, text=f"⏱  SOS Countdown: {self.delay_timer.get()}s",
                 font=("Segoe UI", 8),
                 fg=_P["text_mid"], bg=_P["bg_card"]).pack(anchor="w")

        md_card = self._card(parent, accent_color="#7c3aed")
        md_card.pack(fill="x", pady=(0, 8))
        self._section_header(md_card.content, "📡", "EMERGENCY DISPATCH CONTACTS (USB/ADB)", "#7c3aed")

        p1_val = self.contacts[0]["phone"] if len(self.contacts) > 0 and self.contacts[0].get("phone") else "Not configured"
        p2_val = self.contacts[1]["phone"] if len(self.contacts) > 1 and self.contacts[1].get("phone") else "Not configured"

        def _md_readonly_field(label, p_num, val):
            r = tk.Frame(md_card.content, bg=_P["bg_card"])
            r.pack(fill="x", pady=2)
            lbl_color = _P["amber"] if p_num == 1 else _P["accent2"]
            tk.Label(r, text=label, font=("Segoe UI", 8, "bold"),
                     fg=lbl_color, bg=_P["bg_card"],
                     width=14, anchor="w").pack(side="left", padx=(2, 4))
            e = tk.Entry(r, font=("Segoe UI", 10, "bold"),
                         bg=_P["bg_card2"], fg=_P["text_hi"],
                         readonlybackground=_P["bg_card2"],
                         bd=0, highlightthickness=1,
                         highlightbackground=_P["border"])
            e.insert(0, val)
            e.config(state="readonly")
            e.pack(side="left", fill="x", expand=True, ipady=4)
            e._is_placeholder = False
            return e

        self.p1_entry = _md_readonly_field("P1 (Primary):", 1, p1_val)
        self.p2_entry = _md_readonly_field("P2 (Secondary):", 2, p2_val)

        # Action Buttons row: [✏️ Update Contacts in Settings] [🧪 Test Alert]
        btn_row = tk.Frame(md_card.content, bg=_P["bg_card"])
        btn_row.pack(fill="x", pady=(6, 2))

        tk.Button(btn_row, text="✏️ Update Contacts in Settings",
                  font=("Segoe UI", 8, "bold"),
                  bg="#7c3aed", fg="#ffffff",
                  bd=0, cursor="hand2", pady=4,
                  command=self.open_settings_dialog).pack(side="left", fill="x", expand=True, padx=(0, 2))

        tk.Button(btn_row, text="🧪 Test Alert",
                  font=("Segoe UI", 8, "bold"),
                  bg="#334155", fg=_P["text_hi"],
                  bd=0, cursor="hand2", pady=4,
                  command=lambda: self.trigger_threat("🧪 TEST ALERT (P1 & P2)")).pack(side="right", fill="x", expand=True, padx=(2, 0))

        tk.Label(md_card.content,
                 text="🔒 Read-only on dashboard. Click 'Update Contacts in Settings' to edit.",
                 font=("Segoe UI", 7), fg=_P["text_dim"],
                 bg=_P["bg_card"]).pack(anchor="w", pady=(3, 0))

        # Contacts card
        cc = self._card(parent, accent_color=_P["accent"])
        cc.pack(fill="both", expand=True, pady=(0, 8))
        self._section_header(cc.content, "👥", "EMERGENCY CONTACTS (by priority)", _P["accent"])
        for idx, item in enumerate(self.contacts, start=1):
            r = tk.Frame(cc.content, bg=_P["bg_card"])
            r.pack(fill="x", pady=3)
            priority_color = _P["red"] if idx == 1 else (_P["amber"] if idx == 2 else _P["text_mid"])
            tk.Label(r, text=f"P{idx}", font=("Segoe UI", 9, "bold"),
                     fg=priority_color, bg=_P["bg_card"],
                     width=3).pack(side="left")
            tk.Label(r, text=f"{item['name']}",
                     font=("Segoe UI", 9, "bold"),
                     fg=_P["text_hi"], bg=_P["bg_card"]).pack(side="left", padx=(4, 0))
            tk.Label(r, text=f"  {item['phone']}",
                     font=("Segoe UI", 8),
                     fg=_P["text_mid"], bg=_P["bg_card"]).pack(side="left")

        # Evidence vault card
        ev = self._card(parent, accent_color="#7c2d12")
        ev.pack(fill="x", pady=(0, 4))
        ev_hdr = self._section_header(ev.content, "📁", "EVIDENCE VAULT", "#f97316")
        tk.Button(ev_hdr, text="🗑️ Manage / Purge", font=("Segoe UI", 7, "bold"),
                  bg=_P["bg_card2"], fg=_P["red"], bd=0, cursor="hand2", padx=6, pady=1,
                  command=self.open_purge_vaults_dialog).pack(side="right")
        vf = tk.Frame(ev.content, bg=_P["bg_card"])
        vf.pack(fill="x", pady=4)

        for icon, label, color, folder, attr in [
            ("🎙️", "Audio Vault",  _P["accent"],  self.vault_audio_dir, "a_count_lbl"),
            ("📹", "Video Vault",  _P["purple"], self.vault_video_dir, "v_count_lbl"),
        ]:
            fr = tk.Frame(vf, bg=_P["bg_card2"], cursor="hand2")
            fr.pack(side="left", fill="both", expand=True,
                    padx=(0, 4) if "Audio" in label else (4, 0), ipady=6)
            lbl_icon = tk.Label(fr, text=icon, font=("Segoe UI", 18),
                                fg=color, bg=_P["bg_card2"], cursor="hand2")
            lbl_icon.pack()
            lbl_title = tk.Label(fr, text=label, font=("Segoe UI", 8, "bold"),
                                 fg=color, bg=_P["bg_card2"], cursor="hand2")
            lbl_title.pack()
            cnt = tk.Label(fr, text=f"{label}: 0 file(s)", font=("Segoe UI", 7),
                           fg=_P["text_dim"], bg=_P["bg_card2"], cursor="hand2")
            cnt.pack()
            setattr(self, attr, cnt)
            _folder = folder
            _label = label

            def _make_callbacks(f=_folder, t=_label, card_widgets=(fr, lbl_icon, lbl_title, cnt)):
                def _click(e):
                    self.open_folder_contents(f, t)
                def _enter(e):
                    for w in card_widgets:
                        w.configure(bg="#1e293b")
                def _leave(e):
                    for w in card_widgets:
                        w.configure(bg=_P["bg_card2"])
                for w in card_widgets:
                    w.bind("<Button-1>", _click)
                    w.bind("<Enter>", _enter)
                    w.bind("<Leave>", _leave)

            _make_callbacks()

        threading.Thread(target=self.refresh_media_lists, daemon=True).start()

    # ── Centre column ─────────────────────────────────────────────────────────

    def _build_centre_column(self, parent):
        # Call Escalation Status Banner (visible when waiting or calling)
        self.escalation_frame = tk.Frame(parent, bg="#1e293b", highlightbackground=_P["amber"], highlightthickness=1)
        self.escalation_lbl = tk.Label(self.escalation_frame, textvariable=self.escalation_status_str,
                                       font=("Segoe UI", 8, "bold"), fg=_P["amber"], bg="#1e293b")
        self.escalation_lbl.pack(side="left", padx=8, pady=4)
        tk.Button(self.escalation_frame, text="Dismiss Escalation", font=("Segoe UI", 7, "bold"),
                  bg=_P["red"], fg="#fff", bd=0, cursor="hand2",
                  command=self.cancel_escalation_by_user).pack(side="right", padx=6, pady=2)

        # Live View-Only Status Bar
        view_bar = tk.Frame(parent, bg=_P["bg_card"], padx=8, pady=4)
        view_bar.pack(fill="x", pady=(0, 6))

        tk.Label(view_bar, text="👁️ RADAR MODE: VIEW ONLY", font=("Segoe UI", 8, "bold"),
                 fg=_P["accent"], bg=_P["bg_card"]).pack(side="left")

        tk.Label(view_bar, text="•  Actions & Movement controlled via Simulator", font=("Segoe UI", 7),
                 fg=_P["text_dim"], bg=_P["bg_card"]).pack(side="left", padx=(6, 0))

        tk.Button(
            view_bar, text="🎯 Recenter View", font=("Segoe UI", 7, "bold"),
            bg="#1e293b", fg=_P["text_hi"], bd=0, cursor="hand2", padx=8, pady=2,
            command=self.recenter_map_on_user
        ).pack(side="right")

        # Map card
        mc = self._card(parent, accent_color=_P["green"])
        mc.pack(fill="both", expand=True, pady=(0, 8))

        mhdr = tk.Frame(mc.content, bg=_P["bg_card"])
        mhdr.pack(fill="x", pady=(0, 6))
        badge = tk.Canvas(mhdr, width=22, height=22,
                          bg=_P["bg_card"], highlightthickness=0)
        badge.pack(side="left", padx=(0, 6))
        badge.create_oval(1, 1, 21, 21, fill=_P["green"], outline="")
        badge.create_text(11, 11, text="🗺️", font=("Segoe UI", 10))
        tk.Label(mhdr, text="LIVE ROUTE MAP",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["green"], bg=_P["bg_card"]).pack(side="left")
        tk.Label(mhdr, textvariable=self.speed_indicator_str,
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["amber"], bg=_P["bg_card"]).pack(side="right")

        # Realistic Map canvas (Strictly View-Only)
        self.map_canvas = tk.Canvas(
            mc.content, bg="#0c1322",
            highlightthickness=1, highlightbackground=_P["border"],
            height=280,
        )
        self.map_canvas.pack(fill="both", expand=True)

        # In-canvas notice explaining this canvas is View-Only (no intrusive OS toasts)
        def _show_view_only_canvas_hint():
            if not hasattr(self, "map_canvas") or not self.map_canvas.winfo_exists():
                return
            self.map_canvas.delete("view_only_hint")
            cw = self.map_canvas.winfo_width() or 480
            self.map_canvas.create_rectangle(
                cw // 2 - 180, 10, cw // 2 + 180, 36,
                fill="#0f172a", outline=_P["accent"], width=1, tags="view_only_hint"
            )
            self.map_canvas.create_text(
                cw // 2, 23,
                text="👁️ Live Radar View  •  Use Simulator below to control movement",
                fill=_P["accent"], font=("Segoe UI", 8, "bold"), tags="view_only_hint"
            )
            self.root.after(2500, lambda: self.map_canvas.delete("view_only_hint") if hasattr(self, "map_canvas") and self.map_canvas.winfo_exists() else None)

        self.map_canvas.bind("<Button-1>", lambda e: _show_view_only_canvas_hint())
        self.map_canvas.bind("<Double-Button-1>", lambda e: self.open_map_simulator())

        # Interactive guidance strip underneath map
        hint_row = tk.Frame(mc.content, bg=_P["bg_card"])
        hint_row.pack(fill="x", pady=(3, 0))
        tk.Label(
            hint_row,
            text="👁️ Live Satellite Radar (View Only)  •  Open Interactive Map Simulator below to move map & test actions",
            font=("Segoe UI", 7), fg=_P["text_dim"], bg=_P["bg_card"]
        ).pack(side="left")

        self._draw_map_grid()

        # SOS button (full-width, glowing red)
        sos_outer = tk.Frame(parent, bg=_P["bg_root"])
        sos_outer.pack(fill="x", pady=(0, 8))
        sos_btn = tk.Button(
            sos_outer,
            text="🚨  MANUAL SOS TRIGGER",
            font=("Segoe UI", 12, "bold"),
            bg=_P["sos_bg"], fg="#ffffff", bd=0, cursor="hand2",
            activebackground=_P["sos_hover"],
            command=self.trigger_threat,
        )
        sos_btn.pack(fill="x", ipady=12)
        sos_btn.bind("<Enter>", lambda e: sos_btn.config(bg=_P["sos_hover"]))
        sos_btn.bind("<Leave>", lambda e: sos_btn.config(bg=_P["sos_bg"]))

        # Map simulator launcher & clear buttons
        tk.Button(
            parent,
            text="🎮  Open Route Telemetry Controller",
            font=("Segoe UI", 9, "bold"),
            bg="#1e3a5f", fg=_P["accent"],
            activebackground="#2563eb", activeforeground="#ffffff",
            bd=0, cursor="hand2",
            highlightthickness=1, highlightbackground=_P["accent"],
            command=self.open_map_simulator,
        ).pack(fill="x", ipady=9, pady=(0, 4))

        tk.Button(
            parent, text="🔄  Clear Map Path",
            font=("Segoe UI", 8), bg=_P["bg_card2"],
            fg=_P["text_mid"], bd=0, cursor="hand2",
            command=self.clear_map_display,
        ).pack(fill="x", ipady=5)

    # ── Right telemetry sidebar ───────────────────────────────────────────────

    def _build_telemetry_sidebar(self, parent):
        parent.configure(width=200)
        parent.pack_propagate(False)

        hdr = tk.Frame(parent, bg=_P["bg_panel"],
                       highlightbackground=_P["border"], highlightthickness=1)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="⚡ LIVE TELEMETRY",
                 font=("Segoe UI", 9, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"],
                 pady=8).pack()

        fields = [
            ("⚡ Speed",        self._tele_speed,    _P["amber"]),
            ("📏 Seg. Dist",    self._tele_seg_dist, _P["text_hi"]),
            ("🛣 Trip Dist",    self._tele_cum_dist, _P["text_hi"]),
            ("🏁 Destination",  self._tele_dest,     _P["accent2"]),
            ("🤖 Pred. Lat",   self._tele_pred_lat, _P["predict"]),
            ("🤖 Pred. Lon",   self._tele_pred_lon, _P["predict"]),
            ("🎯 Confidence",   self._tele_conf,     _P["predict"]),
            ("📶 Network",      self._tele_network,  _P["green"]),
            ("🛡 Status",       self._tele_threat,   _P["green"]),
        ]
        for label, var, color in fields:
            row = tk.Frame(parent, bg=_P["bg_card"],
                           highlightbackground=_P["border"],
                           highlightthickness=1)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=("Segoe UI", 7, "bold"),
                     fg=_P["text_dim"], bg=_P["bg_card"],
                     anchor="w", width=12).pack(side="left",
                                                padx=(6, 2), pady=4)
            value_label = tk.Label(row, textvariable=var, font=("Segoe UI", 8, "bold"),
                                   fg=color, bg=_P["bg_card"], anchor="w")
            value_label.pack(side="left", padx=(0, 6))
            if var is self._tele_threat:
                self._tele_threat_label = value_label

        # Separator
        tk.Frame(parent, bg=_P["border"], height=1).pack(fill="x", pady=8)

        # Geofences
        geo_card = self._card(parent, accent_color=_P["green"])
        geo_card.pack(fill="x", pady=2)
        self._section_header(geo_card.content, "📍", "SAFE ZONES", _P["green"])
        tk.Label(geo_card.content,
                 text="Home-Botanical_Campus",
                 font=("Segoe UI", 7), fg=_P["text_mid"],
                 bg=_P["bg_card"]).pack(anchor="w")

        # Hardware telemetry
        hw_card = self._card(parent, accent_color=_P["accent"])
        hw_card.pack(fill="x", pady=(4, 0))
        hw_hdr = self._section_header(hw_card.content, "📱", "HARDWARE", _P["accent"])
        tk.Button(hw_hdr, text="📱 Link Phone", font=("Segoe UI", 7, "bold"),
                  bg=_P["bg_card2"], fg=_P["accent"], bd=0, cursor="hand2", padx=5, pady=1,
                  command=self.open_phone_connection_dialog).pack(side="right")
        tk.Label(hw_card.content, textvariable=self.signal_status_str,
                 font=("Segoe UI", 8), fg=_P["green"],
                 bg=_P["bg_card"]).pack(anchor="w")
        usb_status_row = tk.Frame(hw_card.content, bg=_P["bg_card"])
        usb_status_row.pack(fill="x", pady=(3, 0))
        self.usb_status_led = tk.Canvas(
            usb_status_row, width=14, height=14, bg=_P["bg_card"],
            highlightthickness=0,
        )
        self.usb_status_led.pack(side="left", padx=(0, 5))
        self.usb_status_led.create_oval(
            2, 2, 12, 12, fill=self.usb_dispatch_status_color,
            outline="", tags="usb_status_led",
        )
        self.usb_status_label = tk.Label(
            usb_status_row, textvariable=self.usb_dispatch_status_str,
            font=("Segoe UI", 8, "bold"), fg=self.usb_dispatch_status_color,
            bg=_P["bg_card"], anchor="w",
        )
        self.usb_status_label.pack(side="left")
        tk.Label(hw_card.content, textvariable=self.battery_status_str,
                 font=("Segoe UI", 8), fg=_P["amber"],
                 bg=_P["bg_card"]).pack(anchor="w")

    def update_usb_dispatch_status(self, connected):
        """Render ADB link status only; connectivity changes never trigger SOS."""
        connected = bool(connected)
        color = "#22c55e" if connected else "#64748b"
        label = "Connected (USB Online)" if connected else "Disconnected (Phone Dispatch Unavailable)"
        self.usb_dispatch_status_color = color
        self.usb_dispatch_status_str.set(label)
        led = getattr(self, "usb_status_led", None)
        if led is not None and led.winfo_exists():
            led.itemconfigure("usb_status_led", fill=color)
        status_label = getattr(self, "usb_status_label", None)
        if status_label is not None and status_label.winfo_exists():
            status_label.configure(fg=color)

    def open_phone_connection_dialog(self):
        """Dedicated Mobile Phone Connection & Offline Telephony Management dialog."""
        dlg = tk.Toplevel(self.root)
        dlg.title("📱 Mobile Phone Connection & Offline Dispatch")
        dlg.geometry("540x530")
        dlg.minsize(480, 420)
        dlg.configure(bg=_P["bg_panel"])
        dlg.transient(self.root)
        dlg.grab_set()

        # Header
        tk.Label(dlg, text="📱 MOBILE PHONE CONNECTION",
                 font=("Segoe UI", 12, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(pady=(16, 4))
        tk.Label(dlg, text="Connect Android phone via USB or Wi-Fi to send offline emergency SMS with location.",
                 font=("Segoe UI", 8), fg=_P["text_dim"], bg=_P["bg_panel"]).pack(pady=(0, 12))

        # Status Card
        stat_card = tk.Frame(dlg, bg=_P["bg_card"], padx=16, pady=12,
                             highlightbackground=_P["border"], highlightthickness=1)
        stat_card.pack(fill="x", padx=20, pady=(0, 10))

        lbl_dev = tk.Label(stat_card, text="Checking connection...", font=("Segoe UI", 9, "bold"),
                           fg=_P["text_hi"], bg=_P["bg_card"], anchor="w")
        lbl_dev.pack(fill="x", pady=2)

        lbl_sim = tk.Label(stat_card, text="", font=("Segoe UI", 8),
                           fg=_P["text_mid"], bg=_P["bg_card"], anchor="w")
        lbl_sim.pack(fill="x", pady=2)

        lbl_ports = tk.Label(stat_card, text="", font=("Segoe UI", 8),
                             fg=_P["text_dim"], bg=_P["bg_card"], anchor="w")
        lbl_ports.pack(fill="x", pady=2)

        def _refresh_phone_status():
            try:
                info = self.controller.get_phone_status() if (
                    self.controller and hasattr(self.controller, "get_phone_status")
                ) else {"adb_installed": False, "device_connected": False}
            except Exception as exc:
                info = {"adb_installed": True, "device_connected": False, "error": str(exc)}

            if not info.get("adb_installed"):
                lbl_dev.config(text="❌ ADB Tool Not Found in System PATH", fg=_P["red"])
                lbl_sim.config(text="Please install Android Platform Tools (ADB) to enable phone control.")
                lbl_ports.config(text="")
            elif info.get("error"):
                lbl_dev.config(text="⚠️ ADB device check failed", fg=_P["amber"])
                lbl_sim.config(text=str(info["error"])[:180], fg=_P["text_mid"])
                lbl_ports.config(text="Retrying automatically; check USB debugging authorization if needed.")
            elif info.get("device_connected"):
                model = info.get("device_model", "Android Device")
                dev_id = info.get("device_id", "")
                sim = info.get("sim_state", "UNKNOWN")
                lbl_dev.config(text=f"✅ Connected: {model} ({dev_id})", fg=_P["green"])
                lbl_sim.config(text=f"📶 SIM State: {sim} | Ready for Offline SMS & Priority Calls", fg=_P["text_hi"])
                lbl_ports.config(text="🔄 Telemetry & GPS Bridge Forwarding Active (8080, 8082)")
                self.signal_status_str.set(f"📱 Phone: {model}")
            else:
                if info.get("device_state") == "unauthorized":
                    lbl_dev.config(text=f"⚠️ Authorize USB debugging: {info.get('device_id', '')}", fg=_P["amber"])
                    lbl_sim.config(text="Unlock the phone and accept the USB debugging authorization prompt.", fg=_P["text_mid"])
                    lbl_ports.config(text="Then this dialog will detect it automatically.")
                else:
                    lbl_dev.config(text="⚠️ No Android Phone Detected", fg=_P["amber"])
                    lbl_sim.config(text="Connect phone via USB (with USB Debugging) or Wi-Fi below.", fg=_P["text_mid"])
                    lbl_ports.config(text="Offline SMS will fallback to queuing until phone is linked.")
                    self.signal_status_str.set("📱 Phone: Disconnected")

        _refresh_phone_status()

        # USB Action Card
        usb_card = tk.Frame(dlg, bg=_P["bg_card"], padx=14, pady=10,
                            highlightbackground=_P["border"], highlightthickness=1)
        usb_card.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(usb_card, text="🔌 USB CONNECTION", font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_card"]).pack(anchor="w", pady=(0, 4))
        tk.Label(usb_card, text="Plug phone into PC via USB cable. Ensure 'USB Debugging' is enabled.",
                 font=("Segoe UI", 8), fg=_P["text_dim"], bg=_P["bg_card"]).pack(anchor="w", pady=(0, 6))

        def _do_usb_connect():
            try:
                info = self.controller.connect_mobile_phone() if (
                    self.controller and hasattr(self.controller, "connect_mobile_phone")
                ) else {}
            except Exception as exc:
                info = {"error": str(exc)}
            _refresh_phone_status()
            if info.get("device_connected"):
                messagebox.showinfo("Phone Connected", f"Successfully linked phone:\n{info.get('device_model')} ({info.get('device_id')})\nSIM: {info.get('sim_state')}")
            elif info.get("error"):
                messagebox.showerror("ADB Connection Error", str(info["error"]))
            else:
                messagebox.showwarning("Phone Not Found", "No authorized phone detected over USB.\n\nPlease check:\n1. USB cable is firmly connected\n2. 'USB Debugging' is enabled in Android Developer Options\n3. Tap 'Allow USB Debugging' popup on phone screen.")

        tk.Button(usb_card, text="🔄 Auto-Detect & Link USB Phone", font=("Segoe UI", 8, "bold"),
                  bg=_P["bg_card2"], fg=_P["accent"], bd=0, cursor="hand2", padx=8, pady=4,
                  command=_do_usb_connect).pack(anchor="w")

        # Wi-Fi Action Card
        wifi_card = tk.Frame(dlg, bg=_P["bg_card"], padx=14, pady=10,
                             highlightbackground=_P["border"], highlightthickness=1)
        wifi_card.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(wifi_card, text="📶 WI-FI WIRELESS CONNECTION", font=("Segoe UI", 8, "bold"),
                 fg=_P["purple"], bg=_P["bg_card"]).pack(anchor="w", pady=(0, 4))

        wifi_row = tk.Frame(wifi_card, bg=_P["bg_card"])
        wifi_row.pack(fill="x", pady=4)
        tk.Label(wifi_row, text="Phone IP:Port:", font=("Segoe UI", 8), fg=_P["text_mid"], bg=_P["bg_card"]).pack(side="left")
        e_ip = tk.Entry(wifi_row, font=("Segoe UI", 9), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        e_ip.insert(0, "192.168.1.5:5555")
        e_ip.pack(side="left", fill="x", expand=True, padx=6, ipady=3)

        def _do_wifi_connect():
            ip_val = e_ip.get().strip()
            try:
                res = self.controller.connect_mobile_phone(ip_val) if (
                    self.controller and hasattr(self.controller, "connect_mobile_phone")
                ) else {}
            except Exception as exc:
                res = {"error": str(exc)}
            _refresh_phone_status()
            if res.get("device_connected") or res.get("connected"):
                messagebox.showinfo("Wi-Fi Connected", f"Successfully connected wirelessly to phone at {ip_val}!")
            elif res.get("error"):
                messagebox.showerror("Wi-Fi Connection Failed", str(res["error"]))
            else:
                messagebox.showerror("Wi-Fi Connection Failed", f"Could not connect to {ip_val}.\nMake sure Wireless Debugging / ADB over TCP is enabled on phone.")

        tk.Button(wifi_row, text="Connect Wi-Fi", font=("Segoe UI", 8, "bold"),
                  bg=_P["purple"], fg="#ffffff", bd=0, cursor="hand2", padx=8, pady=3,
                  command=_do_wifi_connect).pack(side="right")

        dialog_status_job = [None]
        def _poll_phone_status():
            if not dlg.winfo_exists():
                return
            _refresh_phone_status()
            dialog_status_job[0] = dlg.after(5000, _poll_phone_status)

        def _close_dialog():
            if dialog_status_job[0] is not None:
                try:
                    dlg.after_cancel(dialog_status_job[0])
                except tk.TclError:
                    pass
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _close_dialog)
        dialog_status_job[0] = dlg.after(5000, _poll_phone_status)

        # Test Offline Alert Button
        test_row = tk.Frame(dlg, bg=_P["bg_panel"], padx=20)
        test_row.pack(fill="x", pady=(2, 8))

        def _test_offline_alert():
            saved_numbers = [c.get("phone", "") for c in self.contacts if c.get("phone")]
            if not saved_numbers:
                messagebox.showwarning("No Contacts", "Please add emergency contacts in Settings first.")
                return
            loc = self.controller.location_engine.get_current_location() if self.controller else {"latitude": 11.48896, "longitude": 79.75388}
            lat = loc.get("latitude", 11.48896)
            lon = loc.get("longitude", 79.75388)
            maps_link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
            msg = f"TEST OFFLINE ALERT from AURA Women Safety. Current Location: Lat {lat:.6f}, Lon {lon:.6f}. Map: {maps_link}"

            dispatcher = getattr(self.controller, "usb_dispatcher", None) if self.controller else None
            if dispatcher is not None:
                def _send_test_alert():
                    res = dispatcher.dispatch_threat_alert(
                        saved_numbers, lat, lon, message_text=msg
                    )
                    try:
                        self.root.after(0, lambda: messagebox.showinfo(
                            "Test Alert Dispatched",
                            f"Test cellular SMS attempted for {len(saved_numbers)} contact(s):\n\n"
                            f"Numbers: {', '.join(saved_numbers)}\nLocation Link: {maps_link}\n"
                            f"Sent: {res.get('sms_sent', 0)}"
                        ))
                    except tk.TclError:
                        pass

                threading.Thread(
                    target=_send_test_alert, name="AURA-test-sms", daemon=True
                ).start()
            else:
                messagebox.showinfo("Test Alert", f"Alert formatted with location link:\n{maps_link}\nReady to dispatch.")

        tk.Button(test_row, text="🧪 Send Test Offline Alert with Location Link", font=("Segoe UI", 8, "bold"),
                  bg=_P["bg_card2"], fg=_P["text_bright"], bd=0, cursor="hand2", padx=8, pady=5,
                  command=_test_offline_alert).pack(fill="x")

        tk.Button(dlg, text="Close", font=("Segoe UI", 8),
                  bg=_P["bg_panel"], fg=_P["text_dim"], bd=0, cursor="hand2",
                  command=dlg.destroy).pack(side="bottom", pady=8)

    # ═══════════════════════════════════════════════════════════════════════════
    # 5 – VAULT DIALOG
    # ═══════════════════════════════════════════════════════════════════════════

    def _vault_files(self, media_type):
        folder = self.vault_audio_dir if media_type == "audio" else self.vault_video_dir
        return sorted(glob.glob(os.path.join(folder, "*.*")))

    def _set_vault_playback(self, active):
        if getattr(self, "_vault_playback_job", None) is not None:
            try:
                self.root.after_cancel(self._vault_playback_job)
            except tk.TclError:
                pass
            self._vault_playback_job = None
        engine = getattr(self, "acoustic_engine", None)
        if engine is None and self.controller:
            engine = getattr(self.controller, "acoustic_engine", None)
        if engine is not None:
            try:
                engine.set_vault_playing(active)
            except Exception as exc:
                print(f"⚠️ [Vault Playback] Could not update microphone suppression: {exc}")

    def _arm_external_vault_playback(self, duration_seconds):
        """Suppress mic input through the estimated length of an external player clip."""
        self._set_vault_playback(True)
        if duration_seconds and duration_seconds > 0:
            def _resume():
                self._vault_playback_job = None
                self._set_vault_playback(False)
            self._vault_playback_job = self.root.after(
                int(duration_seconds * 1000) + 1000, _resume
            )

    def open_folder_contents(self, folder_path, title):
        os.makedirs(folder_path, exist_ok=True)
        dlg = tk.Toplevel(self.root)
        dlg.title(f"📁 {title} — Evidence Manager")
        dlg.geometry("640x500")
        dlg.minsize(540, 400)
        dlg.configure(bg=_P["bg_panel"])
        dlg.transient(self.root)
        dlg.lift()
        dlg.focus_force()

        header_frame = tk.Frame(dlg, bg=_P["bg_panel"])
        header_frame.pack(fill="x", padx=15, pady=(12, 4))

        tk.Label(header_frame, text=f"📁 {title.upper()}",
                 font=("Segoe UI", 11, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(side="left")

        status_lbl = tk.Label(header_frame, text="", font=("Segoe UI", 8),
                              fg=_P["text_dim"], bg=_P["bg_panel"])
        status_lbl.pack(side="right")

        # Toolbar Frame (Search + Select Controls)
        toolbar = tk.Frame(dlg, bg=_P["bg_panel"])
        toolbar.pack(fill="x", padx=15, pady=(2, 6))

        tk.Label(toolbar, text="🔍", font=("Segoe UI", 9), fg=_P["text_dim"], bg=_P["bg_panel"]).pack(side="left")
        search_var = tk.StringVar()
        search_entry = tk.Entry(toolbar, textvariable=search_var, font=("Segoe UI", 9),
                                bg=_P["bg_card"], fg=_P["text_hi"], bd=0, insertbackground=_P["accent"])
        search_entry.pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=3)

        list_frame = tk.Frame(dlg, bg="#161b22")
        list_frame.pack(fill="both", expand=True, padx=15, pady=4)

        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        lb = tk.Listbox(list_frame, font=("Consolas", 9),
                        bg="#161b22", fg="#f8fafc",
                        selectmode=tk.EXTENDED,
                        selectbackground=_P["accent"], selectforeground=_P["bg_root"],
                        yscrollcommand=scrollbar.set, bd=0, highlightthickness=0)
        scrollbar.config(command=lb.yview)
        scrollbar.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        current_records = []

        def _fmt_size(num_bytes):
            if num_bytes < 1024:
                return f"{num_bytes} B"
            elif num_bytes < 1024 * 1024:
                return f"{num_bytes / 1024:.1f} KB"
            else:
                return f"{num_bytes / (1024 * 1024):.1f} MB"

        def _update_selection_status():
            selected_indices = lb.curselection()
            total_items = len(current_records)
            if selected_indices:
                sel_count = len(selected_indices)
                sel_bytes = sum(current_records[i]["size_bytes"] for i in selected_indices if i < len(current_records))
                status_lbl.config(text=f"{sel_count}/{total_items} selected ({_fmt_size(sel_bytes)})")
            else:
                total_bytes = sum(r["size_bytes"] for r in current_records)
                status_lbl.config(text=f"{total_items} file(s) ({_fmt_size(total_bytes)}) | Double-click to play")

        def _select_all():
            lb.select_set(0, tk.END)
            _update_selection_status()

        def _deselect_all():
            lb.selection_clear(0, tk.END)
            _update_selection_status()

        if "Video" in title:
            def _rec_test_vid():
                def _bg():
                    if self.controller and hasattr(self.controller, "acoustic_engine"):
                        self.controller.acoustic_engine.record_emergency_video(duration_seconds=5)
                    time.sleep(5.5)
                    dlg.after(0, reload_list)
                threading.Thread(target=_bg, daemon=True).start()
                self.send_desktop_popup("📹 Video Recording", "Recording 5s test evidence video...")

            tk.Button(toolbar, text="🎥 Record 5s Video", font=("Segoe UI", 7, "bold"),
                      bg=_P["purple"], fg="#ffffff", bd=0, cursor="hand2",
                      command=_rec_test_vid, padx=6, pady=2).pack(side="left", padx=(4, 0))
        elif "Audio" in title:
            def _rec_test_aud():
                def _bg():
                    if self.controller and hasattr(self.controller, "acoustic_engine"):
                        self.controller.acoustic_engine.record_emergency_audio(duration_seconds=5)
                    time.sleep(5.5)
                    dlg.after(0, reload_list)
                threading.Thread(target=_bg, daemon=True).start()
                self.send_desktop_popup("🎙️ Audio Recording", "Recording 5s test evidence audio...")

            tk.Button(toolbar, text="🎙️ Record 5s Audio", font=("Segoe UI", 7, "bold"),
                      bg=_P["accent"], fg=_P["bg_root"], bd=0, cursor="hand2",
                      command=_rec_test_aud, padx=6, pady=2).pack(side="left", padx=(4, 0))

        tk.Button(toolbar, text="Select All", font=("Segoe UI", 7, "bold"),
                  bg=_P["bg_card2"], fg=_P["text_bright"], bd=0, cursor="hand2",
                  command=_select_all, padx=6, pady=2).pack(side="right", padx=(4, 0))
        tk.Button(toolbar, text="Deselect", font=("Segoe UI", 7),
                  bg=_P["bg_card2"], fg=_P["text_dim"], bd=0, cursor="hand2",
                  command=_deselect_all, padx=6, pady=2).pack(side="right")

        def reload_list(*args):
            nonlocal current_records
            lb.delete(0, tk.END)
            current_records = []
            filter_text = search_var.get().strip().lower()
            all_files = sorted(glob.glob(os.path.join(folder_path, "*.*")), reverse=True)

            for fpath in all_files:
                fname = os.path.basename(fpath)
                if filter_text and filter_text not in fname.lower():
                    continue
                try:
                    stat = os.stat(fpath)
                    size_b = stat.st_size
                    mtime_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    size_b = 0
                    mtime_str = "Unknown"

                current_records.append({
                    "path": fpath,
                    "name": fname,
                    "size_bytes": size_b,
                    "size_str": _fmt_size(size_b),
                    "mtime": mtime_str
                })

            for r in current_records:
                entry_line = f" {r['name']:<34}  {r['size_str']:>9}   {r['mtime']}"
                lb.insert(tk.END, entry_line)

            _update_selection_status()
            if hasattr(self, "refresh_media_lists"):
                threading.Thread(target=self.refresh_media_lists, daemon=True).start()

        search_var.trace_add("write", reload_list)
        lb.bind("<<ListboxSelect>>", lambda e: _update_selection_status())

        reload_list()

        bf = tk.Frame(dlg, bg=_P["bg_panel"])
        bf.pack(fill="x", padx=15, pady=12)

        def play():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Select File", "Please select a recording from the list to play, or double-click it.")
                return
            idx = sel[0]
            if idx >= len(current_records):
                return
            fpath = current_records[idx]["path"]
            fname = current_records[idx]["name"]
            if not os.path.exists(fpath):
                messagebox.showerror("File Missing", f"'{fname}' no longer exists.")
                reload_list()
                return

            lower = fname.lower()
            if lower.endswith((".avi", ".wmv", ".mp4", ".mov", ".mkv")):
                self.open_inapp_video_player(fpath, fname)
            elif lower.endswith((".wav", ".mp3", ".ogg")):
                self.open_inapp_audio_player(fpath, fname)
            else:
                try:
                    if platform.system() == "Windows":   os.startfile(fpath)
                    elif platform.system() == "Darwin": subprocess.Popen(["open", fpath])
                    else:                               subprocess.Popen(["xdg-open", fpath])
                except Exception as e:
                    messagebox.showerror("Playback Error", str(e))

        def delete_selected():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Select Files", "Please select one or more recordings from the list to delete.")
                return

            selected_items = [current_records[i] for i in sel if i < len(current_records)]
            count = len(selected_items)

            if count == 1:
                prompt = f"Permanently delete recording '{selected_items[0]['name']}'?"
            else:
                prompt = f"Permanently delete {count} selected recordings?\n\nThis cannot be undone."

            if messagebox.askyesno("🗑️ Delete Confirmation", prompt, icon="warning"):
                deleted_count = 0
                for item in selected_items:
                    fp = item["path"]
                    try:
                        if self.controller and hasattr(self.controller, "delete_recording"):
                            self.controller.delete_recording(fp)
                        else:
                            if os.path.exists(fp):
                                os.remove(fp)
                            if hasattr(self, "controller") and self.controller and hasattr(self.controller, "sync_manager"):
                                self.controller.sync_manager.remove_audio_from_queue(fp)
                        deleted_count += 1
                    except Exception as e:
                        print(f"[Delete Error] {e}")

                reload_list()
                self.send_desktop_popup("🗑️ Recording Deleted", f"Successfully removed {deleted_count} recording(s).")

        def delete_all():
            total = len(current_records)
            if total == 0:
                messagebox.showinfo("Empty Vault", f"There are no recordings to delete in {title}.")
                return

            if messagebox.askyesno(
                "⚠️ Delete All Evidence",
                f"Are you sure you want to permanently delete ALL {total} recordings in {title}?\n\nThis action cannot be undone!",
                icon="warning"
            ):
                deleted_count = 0
                for item in current_records:
                    fp = item["path"]
                    try:
                        if self.controller and hasattr(self.controller, "delete_recording"):
                            self.controller.delete_recording(fp)
                        else:
                            if os.path.exists(fp):
                                os.remove(fp)
                        deleted_count += 1
                    except Exception as e:
                        print(f"[Delete Error] {e}")

                if "Audio" in title and hasattr(self, "controller") and self.controller and hasattr(self.controller, "sync_manager"):
                    self.controller.sync_manager.clear_audio_queue()

                reload_list()
                self.send_desktop_popup("🗑️ Vault Cleared", f"Deleted all {deleted_count} recordings from {title}.")

        def open_folder():
            try:
                abs_dir = os.path.abspath(folder_path)
                os.makedirs(abs_dir, exist_ok=True)
                if platform.system() == "Windows":   os.startfile(abs_dir)
                elif platform.system() == "Darwin": subprocess.Popen(["open", abs_dir])
                else:                               subprocess.Popen(["xdg-open", abs_dir])
            except Exception as e:
                messagebox.showerror("Folder Error", f"Unable to open directory:\n{e}")

        # Right-Click Context Menu
        menu = tk.Menu(dlg, tearoff=0, bg=_P["bg_panel"], fg=_P["text_bright"],
                       activebackground=_P["accent"], activeforeground=_P["bg_root"])
        menu.add_command(label="▶  Play Recording", command=play)
        menu.add_separator()
        menu.add_command(label="🗑  Delete Selected", command=delete_selected)
        menu.add_command(label="⚠️  Delete All Recordings", command=delete_all)
        menu.add_separator()
        def _copy_path():
            sel = lb.curselection()
            if sel and sel[0] < len(current_records):
                self.root.clipboard_clear()
                self.root.clipboard_append(current_records[sel[0]]["path"])
        menu.add_command(label="📋  Copy File Path", command=_copy_path)
        menu.add_command(label="📂  Reveal in File Explorer", command=open_folder)

        def _popup_menu(event):
            clicked_idx = lb.nearest(event.y)
            if clicked_idx not in lb.curselection():
                lb.selection_clear(0, tk.END)
                lb.selection_set(clicked_idx)
                _update_selection_status()
            menu.tk_popup(event.x_root, event.y_root)

        lb.bind("<Button-3>", _popup_menu)
        lb.bind("<Double-Button-1>", lambda e: play())
        lb.bind("<Return>", lambda e: play())
        lb.bind("<Delete>", lambda e: delete_selected())
        lb.bind("<Control-a>", lambda e: (_select_all(), "break")[1])
        lb.bind("<Control-A>", lambda e: (_select_all(), "break")[1])

        # Bottom buttons
        tk.Button(bf, text="▶ Play", font=("Segoe UI", 9, "bold"),
                  bg=_P["accent"], fg=_P["bg_root"], cursor="hand2",
                  bd=0, command=play, width=9, padx=4, pady=3).pack(side="left", padx=3)
        tk.Button(bf, text="📂 Open Folder", font=("Segoe UI", 8, "bold"),
                  bg=_P["bg_card2"], fg=_P["text_bright"], cursor="hand2",
                  bd=0, command=open_folder, width=12, padx=4, pady=3).pack(side="left", padx=3)
        tk.Button(bf, text="🔄 Refresh", font=("Segoe UI", 8, "bold"),
                  bg=_P["bg_card2"], fg=_P["text_bright"], cursor="hand2",
                  bd=0, command=reload_list, width=9, padx=4, pady=3).pack(side="left", padx=3)

        tk.Button(bf, text="⚠️ Delete All", font=("Segoe UI", 8, "bold"),
                  bg="#7f1d1d", fg="#fca5a5", cursor="hand2",
                  bd=0, command=delete_all, width=11, padx=4, pady=3).pack(side="right", padx=3)
        tk.Button(bf, text="🗑 Delete Selected", font=("Segoe UI", 8, "bold"),
                  bg=_P["red"], fg="#fff", cursor="hand2",
                  bd=0, command=delete_selected, width=14, padx=4, pady=3).pack(side="right", padx=3)

    def open_inapp_video_player(self, fpath, fname):
        """Interactive in-app Evidence Video Player using OpenCV and Tkinter Canvas."""
        if not os.path.exists(fpath):
            messagebox.showerror("File Missing", f"'{fname}' no longer exists.")
            return

        v_dlg = tk.Toplevel(self.root)
        v_dlg.title(f"📹 Evidence Video Player — {fname}")
        v_dlg.geometry("680x560")
        v_dlg.minsize(500, 420)
        v_dlg.configure(bg=_P["bg_root"])
        v_dlg.transient(self.root)

        hdr = tk.Frame(v_dlg, bg=_P["bg_panel"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"📹 {fname}", font=("Segoe UI", 10, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(side="left")

        def _open_ext():
            try:
                self._arm_external_vault_playback(total_sec)
                if platform.system() == "Windows": os.startfile(fpath)
                elif platform.system() == "Darwin": subprocess.Popen(["open", fpath])
                else: subprocess.Popen(["xdg-open", fpath])
            except Exception as e:
                self._set_vault_playback(False)
                messagebox.showerror("External Player Error", str(e))

        tk.Button(hdr, text="↗️ Open in Windows Player", font=("Segoe UI", 8),
                  bg=_P["bg_card2"], fg=_P["text_bright"], bd=0, cursor="hand2", padx=6,
                  command=_open_ext).pack(side="right")

        canvas_w, canvas_h = 640, 400
        canvas = tk.Canvas(v_dlg, width=canvas_w, height=canvas_h, bg="#000000", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=12, pady=8)

        ctrl_frame = tk.Frame(v_dlg, bg=_P["bg_panel"], padx=12, pady=8)
        ctrl_frame.pack(fill="x")

        time_lbl = tk.Label(ctrl_frame, text="00:00 / 00:00", font=("Consolas", 9),
                            fg=_P["text_dim"], bg=_P["bg_panel"])
        time_lbl.pack(side="right", padx=(8, 0))

        cap = cv2.VideoCapture(fpath) if CV2_AVAILABLE else None
        fps = (cap.get(cv2.CAP_PROP_FPS) if cap else 20.0) or 20.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) if cap else 1) or 1
        total_sec = total_frames / fps

        is_playing = [True]
        curr_frame_idx = [0]
        timer_id = [None]

        def _fmt_t(s):
            m = int(s // 60)
            sec = int(s % 60)
            return f"{m:02d}:{sec:02d}"

        def _render_frame(frame_img):
            if frame_img is None:
                return
            h, w = frame_img.shape[:2]
            c_w = canvas.winfo_width() or canvas_w
            c_h = canvas.winfo_height() or canvas_h
            if c_w < 50 or c_h < 50:
                c_w, c_h = canvas_w, canvas_h
            scale = min(c_w / w, c_h / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            resized = cv2.resize(frame_img, (nw, nh))
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            if PIL_AVAILABLE:
                pi = ImageTk.PhotoImage(Image.fromarray(rgb))
                canvas.image = pi
                canvas.delete("all")
                canvas.create_image(c_w // 2, c_h // 2, image=pi, anchor="center")

        def _step():
            if not v_dlg.winfo_exists() or cap is None:
                return
            if is_playing[0]:
                ret, frame = cap.read()
                if ret:
                    curr_frame_idx[0] += 1
                    _render_frame(frame)
                    cur_sec = curr_frame_idx[0] / fps
                    time_lbl.config(text=f"{_fmt_t(cur_sec)} / {_fmt_t(total_sec)}")
                    timer_id[0] = v_dlg.after(int(1000 / fps), _step)
                else:
                    is_playing[0] = False
                    play_btn.config(text="🔄 Replay")

        def _toggle_play():
            if cap is None:
                return
            if curr_frame_idx[0] >= total_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                curr_frame_idx[0] = 0
                is_playing[0] = True
                play_btn.config(text="⏸ Pause")
                _step()
            elif is_playing[0]:
                is_playing[0] = False
                play_btn.config(text="▶ Play")
                if timer_id[0]:
                    v_dlg.after_cancel(timer_id[0])
            else:
                is_playing[0] = True
                play_btn.config(text="⏸ Pause")
                _step()

        play_btn = tk.Button(ctrl_frame, text="⏸ Pause", font=("Segoe UI", 9, "bold"),
                             bg=_P["purple"], fg="#ffffff", bd=0, cursor="hand2", padx=12, pady=4,
                             command=_toggle_play)
        play_btn.pack(side="left", padx=(0, 8))

        def _on_close():
            is_playing[0] = False
            if timer_id[0]:
                try: v_dlg.after_cancel(timer_id[0])
                except: pass
            if cap:
                try: cap.release()
                except: pass
            v_dlg.destroy()

        v_dlg.protocol("WM_DELETE_WINDOW", _on_close)
        _step()

    def open_inapp_audio_player(self, fpath, fname):
        """Interactive in-app Evidence Audio Player with waveform visualizer."""
        if not os.path.exists(fpath):
            messagebox.showerror("File Missing", f"'{fname}' no longer exists.")
            return

        a_dlg = tk.Toplevel(self.root)
        a_dlg.title(f"🎙️ Evidence Audio Player — {fname}")
        a_dlg.geometry("520x360")
        a_dlg.minsize(440, 300)
        a_dlg.configure(bg=_P["bg_root"])
        a_dlg.transient(self.root)

        hdr = tk.Frame(a_dlg, bg=_P["bg_panel"], padx=12, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"🎙️ {fname}", font=("Segoe UI", 10, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(side="left")

        def _open_ext():
            try:
                try:
                    with wave.open(fpath, "rb") as audio_file:
                        rate = audio_file.getframerate()
                        duration = audio_file.getnframes() / rate if rate else None
                except Exception:
                    duration = None
                if duration is None and PYGAME_AVAILABLE:
                    try:
                        duration = pygame.mixer.Sound(fpath).get_length()
                    except Exception:
                        duration = None
                self._arm_external_vault_playback(duration)
                if platform.system() == "Windows": os.startfile(fpath)
                elif platform.system() == "Darwin": subprocess.Popen(["open", fpath])
                else: subprocess.Popen(["xdg-open", fpath])
            except Exception as e:
                self._set_vault_playback(False)
                messagebox.showerror("External Player Error", str(e))

        tk.Button(hdr, text="↗️ Open in Windows Player", font=("Segoe UI", 8),
                  bg=_P["bg_card2"], fg=_P["text_bright"], bd=0, cursor="hand2", padx=6,
                  command=_open_ext).pack(side="right")

        vis_canvas = tk.Canvas(a_dlg, bg="#070c14", height=140, highlightthickness=0)
        vis_canvas.pack(fill="x", padx=16, pady=16)

        status_lbl = tk.Label(a_dlg, text="▶ Playing evidence audio...", font=("Segoe UI", 9, "bold"),
                              fg=_P["accent"], bg=_P["bg_root"])
        status_lbl.pack(pady=4)

        ctrl_frame = tk.Frame(a_dlg, bg=_P["bg_panel"], padx=12, pady=10)
        ctrl_frame.pack(fill="x", side="bottom")

        is_playing = [False]
        vis_active = [True]
        playback_job = [None]
        playback_deadline = [None]
        playback_backend = [None]

        def _duration_seconds():
            try:
                with wave.open(fpath, "rb") as audio_file:
                    rate = audio_file.getframerate()
                    return audio_file.getnframes() / rate if rate else None
            except Exception:
                if PYGAME_AVAILABLE:
                    try:
                        return pygame.mixer.Sound(fpath).get_length()
                    except Exception:
                        pass
            return None

        duration_seconds = _duration_seconds()

        def _end_playback(message="⏹ Playback stopped"):
            is_playing[0] = False
            if playback_job[0] is not None:
                try:
                    a_dlg.after_cancel(playback_job[0])
                except tk.TclError:
                    pass
                playback_job[0] = None
            _stop_audio()
            if a_dlg.winfo_exists():
                btn_play.config(text="▶ Play")
                status_lbl.config(text=message, fg=_P["text_dim"])

        def _watch_playback():
            playback_job[0] = None
            if not is_playing[0] or not a_dlg.winfo_exists():
                return
            if (playback_deadline[0] is not None
                    and time.monotonic() >= playback_deadline[0]):
                _end_playback("✅ Playback finished")
                return
            if playback_backend[0] == "pygame" and PYGAME_AVAILABLE and pygame.mixer.get_init():
                try:
                    if not pygame.mixer.music.get_busy():
                        _end_playback("✅ Playback finished")
                        return
                except Exception:
                    pass
            playback_job[0] = a_dlg.after(150, _watch_playback)

        def _animate_vis():
            if not vis_active[0] or not a_dlg.winfo_exists():
                return
            vis_canvas.delete("all")
            cw = vis_canvas.winfo_width() or 480
            ch = vis_canvas.winfo_height() or 140
            bars = 36
            bw = cw / (bars * 1.5)
            import random
            for b in range(bars):
                bx = b * (bw * 1.5) + bw / 2
                if is_playing[0]:
                    bh = random.randint(12, max(13, ch - 20))
                else:
                    bh = 4
                by1 = (ch - bh) / 2
                by2 = by1 + bh
                col = _P["accent"] if b % 2 == 0 else _P["accent2"]
                vis_canvas.create_rectangle(bx, by1, bx + bw, by2, fill=col, outline="")
            a_dlg.after(80, _animate_vis)

        def _start_audio():
            if platform.system() == "Windows" and WINSOUND_AVAILABLE:
                try:
                    self._set_vault_playback(True)
                    winsound.PlaySound(fpath, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    playback_backend[0] = "winsound"
                    is_playing[0] = True
                    playback_deadline[0] = (
                        time.monotonic() + duration_seconds if duration_seconds else None
                    )
                    playback_job[0] = a_dlg.after(150, _watch_playback)
                    return True
                except Exception as e:
                    print(f"Winsound error: {e}")
            elif PYGAME_AVAILABLE:
                try:
                    self._set_vault_playback(True)
                    pygame.mixer.music.load(fpath)
                    pygame.mixer.music.play()
                    playback_backend[0] = "pygame"
                    is_playing[0] = True
                    playback_deadline[0] = (
                        time.monotonic() + duration_seconds if duration_seconds else None
                    )
                    playback_job[0] = a_dlg.after(150, _watch_playback)
                    return True
                except Exception as e:
                    print(f"Pygame audio error: {e}")
            self._set_vault_playback(False)
            playback_backend[0] = None
            return False

        def _stop_audio():
            if platform.system() == "Windows" and WINSOUND_AVAILABLE:
                try:
                    winsound.PlaySound(None, winsound.SND_PURGE)
                except Exception:
                    pass
            elif PYGAME_AVAILABLE:
                try:
                    pygame.mixer.music.stop()
                except Exception:
                    pass
            if playback_job[0] is not None:
                try:
                    a_dlg.after_cancel(playback_job[0])
                except tk.TclError:
                    pass
                playback_job[0] = None
            playback_deadline[0] = None
            playback_backend[0] = None
            self._set_vault_playback(False)

        def _toggle_audio():
            if is_playing[0]:
                is_playing[0] = False
                _stop_audio()
                btn_play.config(text="▶ Play")
                status_lbl.config(text="⏸ Paused", fg=_P["text_dim"])
            else:
                if _start_audio():
                    btn_play.config(text="⏹ Stop")
                    status_lbl.config(text="▶ Playing evidence audio...", fg=_P["accent"])
                else:
                    status_lbl.config(text="⚠️ Unable to play this audio file", fg=_P["red"])

        btn_play = tk.Button(ctrl_frame, text="⏹ Stop", font=("Segoe UI", 9, "bold"),
                             bg=_P["accent"], fg=_P["bg_root"], bd=0, cursor="hand2", padx=14, pady=4,
                             command=_toggle_audio)
        btn_play.pack(side="left")

        def _on_close():
            is_playing[0] = False
            vis_active[0] = False
            _stop_audio()
            a_dlg.destroy()

        a_dlg.protocol("WM_DELETE_WINDOW", _on_close)
        if _start_audio():
            btn_play.config(text="⏹ Stop")
        _animate_vis()

    def open_purge_vaults_dialog(self):
        """Dedicated Evidence Vault Management and Bulk Deletion dialog."""
        dlg = tk.Toplevel(self.root)
        dlg.title("🗑️ Evidence Vault Purge & Storage Management")
        dlg.geometry("480x400")
        dlg.resizable(False, False)
        dlg.configure(bg=_P["bg_panel"])
        dlg.transient(self.root)
        dlg.grab_set()

        def _fmt(n):
            if n < 1024: return f"{n} B"
            if n < 1024 * 1024: return f"{n/1024:.1f} KB"
            return f"{n/(1024*1024):.1f} MB"

        tk.Label(dlg, text="🗑️ EVIDENCE VAULT MANAGEMENT",
                 font=("Segoe UI", 12, "bold"),
                 fg=_P["red"], bg=_P["bg_panel"]).pack(pady=(16, 4))

        tk.Label(dlg, text="Manage, delete, or bulk-purge recorded emergency evidence.",
                 font=("Segoe UI", 8), fg=_P["text_dim"], bg=_P["bg_panel"]).pack(pady=(0, 12))

        stat_card = tk.Frame(dlg, bg=_P["bg_card"], padx=16, pady=12,
                             highlightbackground=_P["border"], highlightthickness=1)
        stat_card.pack(fill="x", padx=20, pady=(0, 14))

        lbl_audio = tk.Label(stat_card, text="", font=("Segoe UI", 9),
                             fg=_P["accent"], bg=_P["bg_card"], anchor="w")
        lbl_audio.pack(fill="x", pady=2)

        lbl_video = tk.Label(stat_card, text="", font=("Segoe UI", 9),
                             fg=_P["purple"], bg=_P["bg_card"], anchor="w")
        lbl_video.pack(fill="x", pady=2)

        lbl_total = tk.Label(stat_card, text="", font=("Segoe UI", 9, "bold"),
                             fg=_P["text_hi"], bg=_P["bg_card"], anchor="w")
        lbl_total.pack(fill="x", pady=(6, 2))

        def _refresh_stats():
            a_files = self._vault_files("audio")
            v_files = self._vault_files("video")
            a_sz = sum(os.path.getsize(f) for f in a_files if os.path.isfile(f))
            v_sz = sum(os.path.getsize(f) for f in v_files if os.path.isfile(f))
            lbl_audio.config(text=f"🎙️ Audio Vault: {len(a_files)} file(s) ({_fmt(a_sz)})")
            lbl_video.config(text=f"📹 Video Vault: {len(v_files)} file(s) ({_fmt(v_sz)})")
            lbl_total.config(text=f"📦 Total Evidence: {len(a_files) + len(v_files)} files ({_fmt(a_sz + v_sz)})")
            if hasattr(self, "refresh_media_lists"):
                threading.Thread(target=self.refresh_media_lists, daemon=True).start()

        _refresh_stats()

        btn_box = tk.Frame(dlg, bg=_P["bg_panel"], padx=20)
        btn_box.pack(fill="both", expand=True)

        def _delete_audio_all():
            a_files = self._vault_files("audio")
            if not a_files:
                messagebox.showinfo("Audio Vault", "No audio recordings found to delete.")
                return
            if messagebox.askyesno("Delete Audio", f"Permanently delete all {len(a_files)} audio recordings?", icon="warning"):
                cnt = 0
                for f in a_files:
                    try:
                        os.remove(f)
                        cnt += 1
                    except Exception as exc:
                        print(f"[Vault Delete Error] {exc}")
                if self.controller and hasattr(self.controller, "sync_manager"):
                    self.controller.sync_manager.clear_audio_queue()
                _refresh_stats()
                self.send_desktop_popup("🗑️ Audio Vault Cleared", f"Deleted {cnt} audio file(s).")

        def _delete_video_all():
            v_files = self._vault_files("video")
            if not v_files:
                messagebox.showinfo("Video Vault", "No video recordings found to delete.")
                return
            if messagebox.askyesno("Delete Video", f"Permanently delete all {len(v_files)} video recordings?", icon="warning"):
                cnt = 0
                for f in v_files:
                    try:
                        os.remove(f)
                        cnt += 1
                    except Exception as exc:
                        print(f"[Vault Delete Error] {exc}")
                _refresh_stats()
                self.send_desktop_popup("🗑️ Video Vault Cleared", f"Deleted {cnt} video file(s).")

        def _delete_everything():
            a_files = self._vault_files("audio")
            v_files = self._vault_files("video")
            total = len(a_files) + len(v_files)
            if total == 0:
                messagebox.showinfo("Evidence Vault", "No recordings found to delete.")
                return
            if messagebox.askyesno("⚠️ Purge All Evidence",
                                   f"Are you sure you want to permanently DELETE ALL {total} audio and video recordings?\n\nThis cannot be undone!",
                                   icon="warning"):
                cnt = 0
                for f in a_files + v_files:
                    try:
                        os.remove(f)
                        cnt += 1
                    except Exception as exc:
                        print(f"[Vault Delete Error] {exc}")
                if self.controller and hasattr(self.controller, "sync_manager"):
                    self.controller.sync_manager.clear_audio_queue()
                _refresh_stats()
                self.send_desktop_popup("⚠️ Evidence Purged", f"Deleted all {cnt} audio and video recordings.")

        tk.Button(btn_box, text="🎙️ Delete All Audio Recordings", font=("Segoe UI", 9, "bold"),
                  bg=_P["bg_card2"], fg=_P["accent"], bd=0, cursor="hand2",
                  command=_delete_audio_all).pack(fill="x", pady=3, ipady=5)

        tk.Button(btn_box, text="📹 Delete All Video Recordings", font=("Segoe UI", 9, "bold"),
                  bg=_P["bg_card2"], fg=_P["purple"], bd=0, cursor="hand2",
                  command=_delete_video_all).pack(fill="x", pady=3, ipady=5)

        tk.Button(btn_box, text="⚠️ Purge All Evidence (Audio & Video)", font=("Segoe UI", 9, "bold"),
                  bg=_P["red"], fg="#fff", bd=0, cursor="hand2",
                  command=_delete_everything).pack(fill="x", pady=(8, 3), ipady=6)

        tk.Button(btn_box, text="Close", font=("Segoe UI", 8),
                  bg=_P["bg_panel"], fg=_P["text_dim"], bd=0, cursor="hand2",
                  command=dlg.destroy).pack(side="bottom", pady=6)

    def refresh_media_lists(self):
        a_files = self._vault_files("audio")
        v_files = self._vault_files("video")
        a_cnt = len(a_files)
        v_cnt = len(v_files)

        def _fmt(n):
            if n < 1024: return f"{n} B"
            if n < 1024 * 1024: return f"{n/1024:.1f} KB"
            return f"{n/(1024*1024):.1f} MB"

        a_sz = sum(os.path.getsize(f) for f in a_files if os.path.isfile(f))
        v_sz = sum(os.path.getsize(f) for f in v_files if os.path.isfile(f))

        def _upd():
            try:
                if hasattr(self, "a_count_lbl") and self.a_count_lbl.winfo_exists():
                    self.a_count_lbl.config(text=f"Audio Vault: {a_cnt} file(s)")
                if hasattr(self, "v_count_lbl") and self.v_count_lbl.winfo_exists():
                    self.v_count_lbl.config(text=f"Video Vault: {v_cnt} file(s)")
            except Exception:
                pass
        try:
            self.root.after(0, _upd)
        except Exception:
            pass


    # ═══════════════════════════════════════════════════════════════════════════
    # PREDEFINED DESTINATION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    def open_destination_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("🎯 Set Predefined Trip Destination")
        dlg.geometry("380x370")
        dlg.configure(bg=_P["bg_panel"])
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="🎯 TRIP DESTINATION",
                 font=("Segoe UI", 12, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(pady=(16, 8))

        frm = tk.Frame(dlg, bg=_P["bg_panel"], padx=20)
        frm.pack(fill="both", expand=True)

        name_hdr = tk.Frame(frm, bg=_P["bg_panel"])
        name_hdr.pack(fill="x", pady=(4, 2))
        tk.Label(name_hdr, text="PLACE OR DESTINATION NAME", font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(side="left")

        name_input_row = tk.Frame(frm, bg=_P["bg_panel"])
        name_input_row.pack(fill="x", pady=(0, 6))

        e_name = tk.Entry(name_input_row, font=("Segoe UI", 10), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        curr = self.controller.get_trip_destination() if self.controller else None
        e_name.insert(0, curr["name"] if curr else "Campus Sanctuary")
        e_name.pack(side="left", fill="x", expand=True, ipady=5)

        tk.Label(frm, text="LATITUDE", font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w", pady=(2, 2))
        e_lat = tk.Entry(frm, font=("Segoe UI", 10), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        e_lat.insert(0, str(curr["latitude"]) if curr else "11.49250")
        e_lat.pack(fill="x", ipady=5, pady=(0, 6))

        tk.Label(frm, text="LONGITUDE", font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w", pady=(2, 2))
        e_lon = tk.Entry(frm, font=("Segoe UI", 10), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        e_lon.insert(0, str(curr["longitude"]) if curr else "79.75800")
        e_lon.pack(fill="x", ipady=5, pady=(0, 8))

        def _set_preset(nm, lt, ln):
            e_name.delete(0, "end"); e_name.insert(0, nm)
            e_lat.delete(0, "end"); e_lat.insert(0, f"{lt:.5f}")
            e_lon.delete(0, "end"); e_lon.insert(0, f"{ln:.5f}")

        def _lookup_place_by_name():
            query = e_name.get().strip()
            if not query:
                messagebox.showwarning("Search", "Please type a place name to search.")
                return

            local_places = {
                "campus": ("Campus Sanctuary", 11.49420, 79.76100),
                "university": ("Annamalai University", 11.49250, 79.75800),
                "metro": ("Metro Station", 11.48150, 79.74200),
                "home": ("Home Sanctuary", 11.48896, 79.75388),
                "railway": ("Railway Station", 11.48200, 79.74100),
                "bus stand": ("Central Bus Stand", 11.47900, 79.73800),
                "hospital": ("City Hospital", 11.48500, 79.74900),
                "temple": ("Nataraja Temple", 11.3992, 79.6934),
            }

            q_lower = query.lower()
            for key, (p_name, p_lat, p_lon) in local_places.items():
                if key in q_lower or q_lower in p_name.lower():
                    _set_preset(p_name, p_lat, p_lon)
                    self.send_desktop_popup("📍 Place Found", f"Loaded coordinates for '{p_name}'")
                    return

            # OpenStreetMap Nominatim Search
            def _async_geo():
                try:
                    import urllib.parse
                    import urllib.request
                    encoded = urllib.parse.quote(query)
                    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
                    req = urllib.request.Request(url, headers={"User-Agent": "AURA-Safety-System/2.0"})
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
                        data = json.loads(resp.read().decode())
                        if data and len(data) > 0:
                            lat = float(data[0]["lat"])
                            lon = float(data[0]["lon"])
                            disp_name = data[0]["display_name"].split(",")[0]
                            dlg.after(0, lambda: _set_preset(disp_name, lat, lon))
                            self.send_desktop_popup("📍 Place Found", f"Found '{disp_name}' ({lat:.4f}, {lon:.4f})")
                            return
                except Exception as e:
                    print(f"[Geocoding Search Error] {e}")

                dlg.after(0, lambda: messagebox.showinfo(
                    "Place Search",
                    f"Could not find coordinates for '{query}'.\nYou can enter the Latitude and Longitude manually or pick a preset."
                ))

            threading.Thread(target=_async_geo, daemon=True).start()

        tk.Button(name_input_row, text="🔍 Find Coordinates", font=("Segoe UI", 8, "bold"),
                  bg=_P["accent"], fg=_P["bg_root"], bd=0, cursor="hand2", padx=10,
                  command=_lookup_place_by_name).pack(side="right", padx=(6, 0), ipady=4)

        # Quick preset buttons
        preset_row = tk.Frame(frm, bg=_P["bg_panel"])
        preset_row.pack(fill="x", pady=(0, 10))
        tk.Label(preset_row, text="Presets: ", font=("Segoe UI", 8), fg=_P["text_mid"], bg=_P["bg_panel"]).pack(side="left")

        tk.Button(preset_row, text="Campus", font=("Segoe UI", 7), bg="#334155", fg="#fff", bd=0,
                  command=lambda: _set_preset("Campus Sanctuary", 11.49420, 79.76100)).pack(side="left", padx=2)
        tk.Button(preset_row, text="Metro Station", font=("Segoe UI", 7), bg="#334155", fg="#fff", bd=0,
                  command=lambda: _set_preset("Metro Station", 11.48150, 79.74200)).pack(side="left", padx=2)
        tk.Button(preset_row, text="Home", font=("Segoe UI", 7), bg="#334155", fg="#fff", bd=0,
                  command=lambda: _set_preset("Home Sanctuary", 11.48896, 79.75388)).pack(side="left", padx=2)

        def _save_dest():
            nm = e_name.get().strip()
            try:
                lt = float(e_lat.get().strip())
                ln = float(e_lon.get().strip())
            except ValueError:
                messagebox.showerror("Error", "Latitude and Longitude must be valid decimal numbers.")
                return
            if not nm:
                messagebox.showerror("Error", "Destination name is required.")
                return

            if self.controller:
                self.controller.set_trip_destination(nm, lt, ln)
            self.destination_status_str.set(f"🏁 Target: {nm}")
            self._tele_dest.set(nm)
            dlg.destroy()
            self.send_desktop_popup("🎯 Destination Set", f"Current trip reference: '{nm}' ({lt:.5f}, {ln:.5f})")
            if hasattr(self, "clear_map_display"):
                self.clear_map_display()

        tk.Button(frm, text="SAVE TRIP DESTINATION",
                  font=("Segoe UI", 9, "bold"),
                  bg=_P["green"], fg="#ffffff", bd=0, cursor="hand2",
                  command=_save_dest).pack(fill="x", ipady=8, pady=(4, 8))

    def clear_trip_destination(self):
        if self.controller:
            self.controller.clear_trip_destination()
        self.destination_status_str.set("🏁 Target: None")
        self._tele_dest.set("None")
        if hasattr(self, "map_canvas") and self.map_canvas.winfo_exists():
            self.map_canvas.delete("dest_marker")
        self.send_desktop_popup("Destination Cleared", "Trip destination has been removed.")

    # ═══════════════════════════════════════════════════════════════════════════
    # FREE ROAM & REAL-TIME INTERACTIVE MAP MOVEMENT
    # ═══════════════════════════════════════════════════════════════════════════

    def toggle_live_movement_demo(self):
        if not self.demo_mode_active:
            self.demo_mode_active = True
            if self.controller:
                self.controller.set_demo_mode(True)
            self.demo_status_str.set("🎮 Free Roam: ACTIVE")
            if hasattr(self, "demo_toggle_btn") and self.demo_toggle_btn.winfo_exists():
                self.demo_toggle_btn.config(text="🎮 Free Roam: ON", bg=_P["accent"])
            self.send_desktop_popup("🎮 Free Roam Active", "Click or drag anywhere on map to move freely.")
        else:
            self.demo_mode_active = False
            if self.controller:
                self.controller.set_demo_mode(False)
            self.demo_status_str.set("🎮 Mode: Real GPS")
            if hasattr(self, "demo_toggle_btn") and self.demo_toggle_btn.winfo_exists():
                self.demo_toggle_btn.config(text="🎮 Free Roam: OFF", bg="#334155")
            self.send_desktop_popup("Free Roam Paused", "Reverted to Real GPS mode.")

    def set_movement_pace(self, mode, speed):
        """Switches movement pace between Walking and Driving."""
        self.roam_pace_mode = mode
        self.demo_speed = speed
        if hasattr(self, "btn_pace_walk") and hasattr(self, "btn_pace_drive"):
            if mode == "Walk":
                self.btn_pace_walk.config(bg=_P["accent"], fg=_P["bg_root"])
                self.btn_pace_drive.config(bg="#1e293b", fg=_P["amber"])
            else:
                self.btn_pace_walk.config(bg="#1e293b", fg=_P["accent"])
                self.btn_pace_drive.config(bg=_P["amber"], fg=_P["bg_root"])
        self.demo_status_str.set(f"🎮 Pace: {mode} ({speed:.0f} km/h)")
        if self.controller:
            self.controller.demo_speed = speed

    def recenter_map_on_user(self):
        """Snaps map focus back to current user position."""
        lat = self.current_sim_lat or map_config.CENTER_LAT
        lon = self.current_sim_lon or map_config.CENTER_LON
        map_config.set_map_center(lat, lon)
        self._map_base_drawn = False
        self._draw_realistic_map()
        self.update_map_canvas(lat, lon, None, self.demo_speed, False)
        self.send_desktop_popup("🎯 Map Recentered", f"Centered on ({lat:.5f}, {lon:.5f})")

    @staticmethod
    def _haversine_dist(lat1, lon1, lat2, lon2):
        """Calculates distance in meters between two lat/lon points."""
        R = 6371000.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2.0) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2)
        return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    def get_nearest_street_name(self, lat, lon):
        """Returns the nearest road or sector name for realistic HUD display."""
        landmarks = [
            ("NH-45 Express Arterial", 11.458, 79.720),
            ("Sanctuary Boulevard", 11.462, 79.728),
            ("Campus Avenue", 11.485, 79.725),
            ("Metro Transit Corridor", 11.445, 79.708),
            ("Riverfront Promenade", 11.470, 79.742),
            ("Botanical Garden Way", 11.492, 79.755),
            ("Commerce District", 11.450, 79.712),
            ("Central Square", 11.465, 79.725),
        ]
        best_name = "Sanctuary Way"
        min_d = float("inf")
        for name, s_lat, s_lon in landmarks:
            d = self._haversine_dist(lat, lon, s_lat, s_lon)
            if d < min_d:
                min_d = d
                best_name = name
        return best_name

    def move_user_to_latlon(self, target_lat, target_lon, speed_kmh=None):
        """
        Moves user freely to given coordinates.
        Calculates visual velocity and heading, updates the demo marker and
        breadcrumb trail. Manual map input is never treated as GPS evidence.
        """
        if not self.demo_mode_active:
            self.demo_mode_active = True
            if self.controller:
                self.controller.set_demo_mode(True)
            if hasattr(self, "demo_toggle_btn") and self.demo_toggle_btn.winfo_exists():
                self.demo_toggle_btn.config(text="🎮 Free Roam: ON", bg=_P["accent"])

        prev_lat = self.current_sim_lat or map_config.CENTER_LAT
        prev_lon = self.current_sim_lon or map_config.CENTER_LON

        d_m = self._haversine_dist(prev_lat, prev_lon, target_lat, target_lon)

        # Calculate heading angle in degrees (0=North, 90=East, 180=South, 270=West)
        dy = target_lat - prev_lat
        dx = (target_lon - prev_lon) * math.cos(math.radians(prev_lat))
        if abs(dy) > 1e-6 or abs(dx) > 1e-6:
            self.user_heading_deg = math.degrees(math.atan2(dx, dy)) % 360

        now = time.time()
        dt = max(now - getattr(self, "_last_move_time", now - 1.0), 0.1)
        self._last_move_time = now

        if speed_kmh is not None:
            calc_speed = float(speed_kmh)
        else:
            # The simulator represents cursor movement as travel. Use each drag
            # segment's elapsed time so speed responds immediately at every point.
            calc_speed = min(250.0, max(0.0, (d_m / dt) * 3.6))

        self.current_sim_lat = round(target_lat, 6)
        self.current_sim_lon = round(target_lon, 6)
        self.demo_speed = round(calc_speed, 1)
        print(
            f"🛰️ [Map Simulator] Lat: {self.current_sim_lat:.6f}, "
            f"Lon: {self.current_sim_lon:.6f}"
        )

        # This method is driven by canvas dragging, clicks, and directional UI
        # controls. Treat it as visual simulator input only; never as GPS data.
        is_threat = False
        if self.controller:
            self.controller.update_demo_position(target_lat, target_lon, speed_kmh=calc_speed)

        nearest_st = self.get_nearest_street_name(target_lat, target_lon)
        self.demo_status_str.set(f"📍 {nearest_st} • ({target_lat:.5f}, {target_lon:.5f})")
        self.update_map_canvas(target_lat, target_lon, None, self.demo_speed, is_threat)

    def step_user_direction(self, heading_deg, step_meters=None):
        """Moves user by a directional step (North, South, East, West)."""
        if step_meters is None:
            step_meters = 20.0 if getattr(self, "roam_pace_mode", "Drive") == "Walk" else 60.0

        cur_lat = self.current_sim_lat or map_config.CENTER_LAT
        cur_lon = self.current_sim_lon or map_config.CENTER_LON

        rad = math.radians(heading_deg)
        d_lat = (step_meters * math.cos(rad)) / 111000.0
        d_lon = (step_meters * math.sin(rad)) / (111000.0 * math.cos(math.radians(cur_lat)))

        new_lat = round(cur_lat + d_lat, 6)
        new_lon = round(cur_lon + d_lon, 6)
        self.user_heading_deg = heading_deg

        spd = 5.0 if getattr(self, "roam_pace_mode", "Drive") == "Walk" else 45.0
        self.move_user_to_latlon(new_lat, new_lon, speed_kmh=spd)

    def on_map_keyboard_move(self, heading_deg):
        """Handles keyboard arrow navigation, ignoring when focused on inputs."""
        try:
            focused = self.root.focus_get()
            if isinstance(focused, (tk.Entry, tk.Spinbox, tk.Text)):
                return
        except Exception:
            pass
        self.step_user_direction(heading_deg)

    def _on_map_click(self, event):
        """Handles click on realistic map canvas or on-screen D-Pad."""
        if not hasattr(self, "map_canvas") or not self.map_canvas.winfo_exists():
            return
        w = self.map_canvas.winfo_width() or 480
        h = self.map_canvas.winfo_height() or 280

        # Check if click landed on the on-canvas D-Pad in the bottom-right corner
        cx, cy = w - 46, h - 46
        dx = event.x - cx
        dy = event.y - cy
        dist = math.hypot(dx, dy)
        if dist <= 38:
            angle = (math.degrees(math.atan2(dx, -dy))) % 360  # 0=North, 90=East, 180=South, 270=West
            if 315 <= angle or angle < 45:
                self.step_user_direction(0)   # North
            elif 45 <= angle < 135:
                self.step_user_direction(90)  # East
            elif 135 <= angle < 225:
                self.step_user_direction(180) # South
            else:
                self.step_user_direction(270) # West
            return

        # Regular terrain click: freely move user marker to clicked location
        self.is_dragging_user = True
        self._last_move_time = time.time()
        lat, lon = map_config.pixel_to_latlon(event.x, event.y, (w, h))
        self.move_user_to_latlon(lat, lon)

    def _on_map_drag(self, event):
        """Continuously moves user along mouse drag trajectory with throttling."""
        if not hasattr(self, "map_canvas") or not self.map_canvas.winfo_exists():
            return
        now = time.time()
        if (now - getattr(self, "_last_drag_time", 0.0)) < 0.05:
            return
        self._last_drag_time = now

        w = self.map_canvas.winfo_width() or 480
        h = self.map_canvas.winfo_height() or 280
        if event.x >= w - 85 and event.y >= h - 85:
            return
        lat, lon = map_config.pixel_to_latlon(event.x, event.y, (w, h))
        self.move_user_to_latlon(lat, lon)

    def _on_map_release(self, event):
        self.is_dragging_user = False

    def _update_display_only_btn(self):
        if not hasattr(self, "btn_display_only") or not self.btn_display_only.winfo_exists():
            return
        if getattr(self, "display_only_mode", False):
            self.btn_display_only.config(
                text="👁️ DISPLAY ONLY (SHOW ONLY)",
                bg="#0284c7", fg="#ffffff", activebackground="#0369a1", activeforeground="#ffffff"
            )
        else:
            self.btn_display_only.config(
                text="🛡️ ACTION MODE: ARMED",
                bg="#991b1b", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # AUTOMATED CALL ESCALATION UI
    # ═══════════════════════════════════════════════════════════════════════════

    def update_escalation_state(self, message: str):
        self.escalation_status_str.set(message)
        if hasattr(self, "escalation_lbl") and self.escalation_lbl.winfo_exists():
            self.escalation_lbl.config(text=message)
        if hasattr(self, "escalation_frame") and self.escalation_frame.winfo_exists():
            if message and not message.startswith("✅"):
                self.escalation_frame.pack(fill="x", pady=(0, 6))
            elif message.startswith("✅"):
                self.root.after(3000, lambda: self.escalation_frame.pack_forget() if hasattr(self, "escalation_frame") and self.escalation_frame.winfo_exists() else None)

    def cancel_escalation_by_user(self):
        if self.controller:
            self.controller.cancel_call_escalation()
        self.update_escalation_state("✅ Call Escalation Cancelled by User")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6 – SETTINGS DIALOG
    # ═══════════════════════════════════════════════════════════════════════════

    def save_dashboard_contacts(self):
        raw_p1 = self.p1_entry.get().strip() if hasattr(self, "p1_entry") else ""
        raw_p2 = self.p2_entry.get().strip() if hasattr(self, "p2_entry") else ""
        p1 = self.clean_phone(raw_p1)
        p2 = self.clean_phone(raw_p2)

        if not p1:
            messagebox.showwarning("Input Error", "Please enter at least a primary 10-digit number for P1.")
            return

        if not self.is_valid_phone(p1):
            messagebox.showerror("Validation Error", "P1 must be a valid 10-digit mobile number.")
            return

        if p2 and not self.is_valid_phone(p2):
            messagebox.showerror("Validation Error", "P2 must be a valid 10-digit mobile number.")
            return

        # Update self.contacts
        if len(self.contacts) > 0:
            self.contacts[0]["phone"] = p1
        else:
            self.contacts.append({"name": "Guardian 1", "phone": p1})

        if p2:
            if len(self.contacts) > 1:
                self.contacts[1]["phone"] = p2
            else:
                self.contacts.append({"name": "Guardian 2", "phone": p2})
        elif len(self.contacts) > 1:
            self.contacts.pop(1)

        self.save_user_profile()
        if self.controller:
            self.controller.set_emergency_contacts([c["phone"] for c in self.contacts])

        self.send_desktop_popup("✅ Numbers Saved", f"Saved P1: {p1}" + (f", P2: {p2}" if p2 else ""))
        self.build_modern_dashboard(pending_notification=f"✅ Emergency numbers saved! P1: {p1}" + (f", P2: {p2}" if p2 else ""))

    def open_settings_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("⚙️ AURA System Settings")
        dlg.geometry("580x700")
        dlg.configure(bg=_P["bg_panel"])
        dlg.transient(self.root)
        dlg.grab_set()

        title_row = tk.Frame(dlg, bg=_P["bg_panel"])
        title_row.pack(pady=12)
        tb = tk.Canvas(title_row, width=28, height=28,
                       bg=_P["bg_panel"], highlightthickness=0)
        tb.pack(side="left", padx=(0, 8))
        tb.create_oval(1, 1, 27, 27, fill=_P["accent"], outline="")
        tb.create_text(14, 14, text="⚙️", font=("Segoe UI", 12))
        tk.Label(title_row, text="SYSTEM CONFIGURATION",
                 font=("Segoe UI", 12, "bold"),
                 fg=_P["accent"], bg=_P["bg_panel"]).pack(side="left")

        frm = tk.Frame(dlg, bg=_P["bg_panel"], padx=22)
        frm.pack(fill="both", expand=True)

        usr_row = tk.Frame(frm, bg=_P["bg_panel"])
        usr_row.pack(fill="x", pady=(2, 8))

        # User Name col
        u_col = tk.Frame(usr_row, bg=_P["bg_panel"])
        u_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Label(u_col, text="REGISTERED USER NAME",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
        e_name = tk.Entry(u_col, font=("Segoe UI", 10),
                          bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        e_name.insert(0, self.user_name)
        e_name.pack(fill="x", ipady=5, pady=(2, 0))

        # User Phone col
        p_col = tk.Frame(usr_row, bg=_P["bg_panel"])
        p_col.pack(side="right", fill="x", expand=True, padx=(6, 0))
        tk.Label(p_col, text="USER PHONE (10-DIGIT)",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
        e_phone = tk.Entry(p_col, font=("Segoe UI", 10),
                           bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        e_phone.insert(0, self.user_phone)
        e_phone.pack(fill="x", ipady=5, pady=(2, 0))

        # SOS timer row
        sec_row = tk.Frame(frm, bg=_P["bg_panel"])
        sec_row.pack(fill="x", pady=(2, 6))

        # Timer col
        tmr_col = tk.Frame(sec_row, bg=_P["bg_panel"])
        tmr_col.pack(side="left", fill="x", expand=True)
        tk.Label(tmr_col, text="SOS COUNTDOWN",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["amber"], bg=_P["bg_panel"]).pack(anchor="w")
        spin = tk.Spinbox(tmr_col, from_=5, to=60, increment=5,
                          textvariable=self.delay_timer,
                          font=("Segoe UI", 10, "bold"),
                          bg=_P["bg_input"], fg=_P["amber"], bd=0)
        spin.pack(fill="x", ipady=5, pady=(2, 0))

        # Safety thresholds row (Escalation timeout + Speed threshold)
        thresh_row = tk.Frame(frm, bg=_P["bg_panel"])
        thresh_row.pack(fill="x", pady=(2, 10))

        escl_col = tk.Frame(thresh_row, bg=_P["bg_panel"])
        escl_col.pack(side="left", fill="x", expand=True, padx=(0, 4))
        tk.Label(escl_col, text="CALL RETRY INTERVAL (20-120s)",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
        spin_escl = tk.Spinbox(escl_col, from_=20, to=120, increment=10,
                               textvariable=self.escalation_timeout_var,
                               font=("Segoe UI", 10, "bold"),
                               bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        spin_escl.pack(fill="x", ipady=5, pady=(2, 0))

        speed_col = tk.Frame(thresh_row, bg=_P["bg_panel"])
        speed_col.pack(side="right", fill="x", expand=True, padx=(4, 0))
        tk.Label(speed_col, text="SPEED THRESHOLD (KM/H)",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
        spin_spd = tk.Spinbox(speed_col, from_=20, to=140, increment=5,
                              textvariable=self.speed_threshold_var,
                              font=("Segoe UI", 10, "bold"),
                              bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
        spin_spd.pack(fill="x", ipady=5, pady=(2, 0))

        # Contacts
        contacts_hdr = tk.Frame(frm, bg=_P["bg_panel"])
        contacts_hdr.pack(fill="x", pady=(6, 6))
        tk.Label(contacts_hdr,
                 text="EMERGENCY CONTACTS  (top = highest priority)",
                 font=("Segoe UI", 8, "bold"),
                 fg=_P["accent2"], bg=_P["bg_panel"]).pack(side="left")

        contacts_frame = tk.Frame(frm, bg=_P["bg_panel"])
        contacts_frame.pack(fill="x", pady=4)

        settings_contact_rows = []

        def render_contact_rows():
            for r in settings_contact_rows:
                r["frame"].destroy()
            settings_contact_rows.clear()
            for idx, contact in enumerate(self.contacts):
                row = tk.Frame(contacts_frame, bg=_P["bg_card2"])
                row.pack(fill="x", pady=3, ipady=3)
                tk.Label(row, text=f"P{idx+1}",
                         font=("Segoe UI", 9, "bold"),
                         fg=_P["amber"], bg=_P["bg_card2"],
                         width=3).pack(side="left", padx=(4, 4))
                ec_n = tk.Entry(row, font=("Segoe UI", 9),
                                bg=_P["bg_input"], fg=_P["text_hi"],
                                bd=0, width=16)
                ec_n.insert(0, contact["name"])
                ec_n.pack(side="left", padx=2, ipady=4)
                ec_p = tk.Entry(row, font=("Segoe UI", 9),
                                bg=_P["bg_input"], fg=_P["text_hi"],
                                bd=0, width=13)
                ec_p.insert(0, contact["phone"])
                ec_p.pack(side="left", padx=2, ipady=4)

                def _up(i=idx):
                    if i > 0:
                        self.contacts[i-1], self.contacts[i] = \
                            self.contacts[i], self.contacts[i-1]
                        render_contact_rows()

                def _down(i=idx):
                    if i < len(self.contacts) - 1:
                        self.contacts[i+1], self.contacts[i] = \
                            self.contacts[i], self.contacts[i+1]
                        render_contact_rows()

                def _del(i=idx):
                    self.contacts.pop(i)
                    render_contact_rows()

                for lbl, cmd in [("↑", _up), ("↓", _down), ("✖", _del)]:
                    bg = _P["red"] if lbl == "✖" else "#334155"
                    tk.Button(row, text=lbl,
                              font=("Segoe UI", 8, "bold"),
                              bg=bg, fg="#fff", bd=0, width=2,
                              command=cmd).pack(side="left", padx=1)

                settings_contact_rows.append({
                    "frame": row, "name_entry": ec_n, "phone_entry": ec_p})

        render_contact_rows()

        tk.Button(frm, text="+ Add Emergency Contact",
                  font=("Segoe UI", 8, "bold"),
                  bg=_P["accent"], fg=_P["bg_root"],
                  bd=0, cursor="hand2", pady=4,
                  command=lambda: [
                      self.contacts.append({"name": "New Contact",
                                            "phone": "0000000000"}),
                      render_contact_rows()
                  ]).pack(fill="x", pady=(4, 16))

        self._section_header(frm, "📍", "SAFE ZONES", _P["green"])
        zones_frame = tk.Frame(frm, bg=_P["bg_panel"])
        zones_frame.pack(fill="x", pady=(0, 5))

        def render_zone_rows():
            for w in zones_frame.winfo_children():
                w.destroy()
            zones = self.controller.safe_zones if self.controller else []
            if not zones:
                tk.Label(zones_frame,
                         text="No safe zones — geofencing OFF.",
                         font=("Segoe UI", 8), fg=_P["text_dim"],
                         bg=_P["bg_panel"]).pack(anchor="w")
                return
            for idx, zone in enumerate(zones):
                row = tk.Frame(zones_frame, bg=_P["bg_card2"])
                row.pack(fill="x", pady=2, ipady=3)
                tk.Label(row,
                         text=f"📍 {zone['name']} (r={zone['radius_km']} km)",
                         font=("Segoe UI", 8),
                         fg=_P["text_hi"], bg=_P["bg_card2"]).pack(side="left", padx=6)

                def _del_zone(i=idx):
                    self.controller.remove_safe_zone(i)
                    render_zone_rows()

                tk.Button(row, text="✖", font=("Segoe UI", 8, "bold"),
                          bg=_P["red"], fg="#fff", bd=0, width=2,
                          command=_del_zone).pack(side="right", padx=4)

        render_zone_rows()

        def _add_zone():
            if not self.controller:
                messagebox.showerror("Error", "Controller not connected.")
                return
            zd = tk.Toplevel(dlg)
            zd.title("Add Safe Zone")
            zd.geometry("310x230")
            zd.configure(bg=_P["bg_panel"])
            zd.transient(dlg)
            zd.grab_set()
            tk.Label(zd, text="ZONE NAME", font=("Segoe UI", 8, "bold"),
                     fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w", padx=16, pady=(14, 2))
            ez = tk.Entry(zd, font=("Segoe UI", 10),
                          bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
            ez.insert(0, "Home")
            ez.pack(fill="x", ipady=6, padx=16)
            tk.Label(zd, text="RADIUS (KM)", font=("Segoe UI", 8, "bold"),
                     fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w", padx=16, pady=(10, 2))
            er = tk.Entry(zd, font=("Segoe UI", 10),
                          bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
            er.insert(0, "0.5")
            er.pack(fill="x", ipady=6, padx=16)
            tk.Label(zd, text="Uses the current simulated location as zone centre.",
                     font=("Segoe UI", 7), fg=_P["text_dim"],
                     bg=_P["bg_panel"], justify="left").pack(anchor="w", padx=16, pady=6)

            def _confirm():
                name = ez.get().strip()
                try:
                    radius = float(er.get().strip())
                except ValueError:
                    messagebox.showerror("Error", "Radius must be a number.")
                    return
                if not name:
                    messagebox.showerror("Error", "Enter a zone name.")
                    return
                current = self.controller.location_engine.get_current_location()
                self.controller.add_safe_zone(
                    name, current["latitude"], current["longitude"], radius)
                zd.destroy()
                render_zone_rows()

            tk.Button(zd, text="ADD ZONE",
                      font=("Segoe UI", 9, "bold"),
                      bg=_P["green"], fg="#fff", bd=0,
                      command=_confirm).pack(fill="x", padx=16, pady=12, ipady=6)

        tk.Button(frm, text="+ Add Current Location as Safe Zone",
                  font=("Segoe UI", 8, "bold"),
                  bg=_P["accent"], fg=_P["bg_root"],
                  bd=0, cursor="hand2", pady=4,
                  command=_add_zone).pack(fill="x", pady=(4, 16))

        self._section_header(frm, "🧪", "DEMO / TESTING", _P["text_dim"])
        tk.Button(frm, text="🧪 Test SOS Alert",
                  font=("Segoe UI", 8),
                  bg="#334155", fg=_P["text_mid"],
                  bd=0, cursor="hand2", pady=4,
                  command=lambda: [dlg.destroy(),
                                   self.trigger_threat("💥 TEST ALERT")]).pack(
                  fill="x", pady=(0, 14))

        def _save():
            for i, row in enumerate(settings_contact_rows):
                nv = row["name_entry"].get().strip()
                raw_pv = row["phone_entry"].get().strip()
                pv = self.clean_phone(raw_pv)
                if not nv or not self.is_valid_phone(pv):
                    messagebox.showerror(
                        "Validation Error",
                        f"Row {i+1}: valid name and 10-digit number required.")
                    return
                self.contacts[i] = {"name": nv, "phone": pv}
            if not self.contacts:
                messagebox.showerror("Error", "Keep at least one contact.")
                return

            phone_val = self.clean_phone(e_phone.get().strip())
            if phone_val and not self.is_valid_phone(phone_val):
                messagebox.showerror("Validation Error", "User phone must be a valid 10-digit number.")
                return

            try:
                self.escalation_timeout_s = int(self.escalation_timeout_var.get())
                self.escalation_timeout_s = max(20, min(120, self.escalation_timeout_s))
            except Exception:
                self.escalation_timeout_s = 60
            self.escalation_timeout_var.set(self.escalation_timeout_s)
            try:
                self.speed_threshold_kmh = float(self.speed_threshold_var.get())
            except Exception:
                self.speed_threshold_kmh = 25.0

            self.user_name = e_name.get().strip()
            self.user_phone = phone_val
            if self.controller:
                self.controller.set_emergency_contacts(
                    [c["phone"] for c in self.contacts])
                self.controller.call_escalation_timeout_sec = self.escalation_timeout_s
                if hasattr(self.controller, "escalation_manager"):
                    self.controller.escalation_manager.timeout_s = self.escalation_timeout_s
                if hasattr(self.controller, "anomaly_engine"):
                    self.controller.anomaly_engine.speed_threshold_kmh = self.speed_threshold_kmh
            self.save_user_profile()
            dlg.destroy()
            self.build_modern_dashboard(
                pending_notification="⚙️ Settings Updated Successfully!")

        def _reset_profile():
            if messagebox.askyesno("Reset Profile", "Reset current profile and return to the initial setup screen?"):
                if os.path.exists(self.profile_path):
                    try:
                        os.remove(self.profile_path)
                    except Exception:
                        pass
                self.user_name = ""
                self.user_phone = ""
                self.contacts = []
                self.is_protection_active = False
                dlg.destroy()
                self.build_setup_screen()

        btn_frame = tk.Frame(dlg, bg=_P["bg_panel"])
        btn_frame.pack(fill="x", padx=22, pady=14)

        tk.Button(btn_frame, text="SAVE CHANGES",
                  font=("Segoe UI", 10, "bold"),
                  bg=_P["green"], fg="#fff", bd=0, cursor="hand2",
                  command=_save).pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 6))

        tk.Button(btn_frame, text="🔄 Return to First Page",
                  font=("Segoe UI", 9, "bold"),
                  bg=_P["bg_card2"], fg=_P["text_bright"], bd=0, cursor="hand2",
                  command=_reset_profile).pack(side="right", ipady=8, padx=(6, 0))

    # ═══════════════════════════════════════════════════════════════════════════
    # 7 – THREAT TIMER & SOS DISPATCH
    # ═══════════════════════════════════════════════════════════════════════════

    def trigger_threat(self, reason_msg="⚠️ CRITICAL THREAT DETECTED ⚠️"):
        normalized_reason = str(reason_msg or "").strip().lower().replace("_", " ").replace("-", " ")
        if any(marker in normalized_reason for marker in (
            "adb disconnected", "usb disconnected", "phone disconnected",
            "device disconnected", "connection lost", "adb offline",
            "usb offline", "phone offline",
        )):
            print(f"ℹ️ [GUI] Connectivity-only event does not trigger SOS: {reason_msg}")
            return

        now = time.time()
        # Suppress repeated triggers if threat screen is currently open
        if (
            getattr(self, "is_threat_active", False)
            or getattr(self, "popup_showing", False)
        ):
            print(f"ℹ️ [GUI] Threat trigger '{reason_msg}' debounced (threat screen active).")
            return

        # Debounce identical duplicate triggers in rapid succession (< 3 seconds)
        last_t = getattr(self, "_last_threat_trigger_time", 0.0)
        last_r = getattr(self, "_last_threat_reason", "")
        if (now - last_t) < 3.0 and last_r == reason_msg:
            print(f"ℹ️ [GUI] Duplicate threat trigger '{reason_msg}' debounced (<3s since identical trigger).")
            return
        self._last_threat_trigger_time = now
        self._last_threat_reason = reason_msg

        reason_upper = str(reason_msg).upper()

        # Route and speed anomalies give the user a short chance to cancel a false alarm.
        is_fall = any(k in reason_upper for k in ["FALL", "DROP", "IMPACT"])
        is_route_dev = any(k in reason_upper for k in ["DEVIATION", "ROUTE", "OFF_ROUTE", "WAYPOINT_DEVIATED"])
        is_speed_anomaly = any(k in reason_upper for k in ["SPEED", "OVERSPEED", "HIGH_SPEED"])
        is_combined_anomaly = is_route_dev and is_speed_anomaly

        should_countdown = is_fall or is_route_dev or is_speed_anomaly

        if not should_countdown:
            # The countdown should not appear when the alert is being dispatched, as it is already identified as an emergency.
            print(f"🚨 [INSTANT SOS DISPATCH] Confirmed emergency identified ({reason_msg}). Dispatching alert directly without countdown...")
            self.active_threat_reason = reason_msg
            self.dispatch_sos()
            return

        if self.is_threat_active:
            return
        if not self.is_protection_active:
            self.is_protection_active = True

        self.active_threat_reason = reason_msg
        self.trigger_emergency_ui_mode(
            reason=reason_msg,
            dispatch_status="SOS COUNTDOWN ACTIVE  •  SIREN / ALERT ARMED",
        )
        self.mark_emergency_active()

        self._start_popup_alert_chime()

        self.is_threat_active = True
        self.alert_active = True
        self.total_time = 10 if (is_route_dev or is_speed_anomaly) else self.delay_timer.get()
        self.time_left  = self.total_time

        self.threat_popup = tk.Toplevel(self.root)
        self.popup_showing = True
        self.threat_popup.title("Emergency Alert")
        self.threat_popup.configure(bg=_P["threat_bg"])
        self.threat_popup.resizable(False, False)
        self.threat_popup.transient(self.root)
        self.threat_popup.attributes("-topmost", True)
        self.threat_popup.protocol("WM_DELETE_WINDOW", lambda: self.dismiss_alarm("User cancelled alert"))
        popup_width, popup_height = 480, 500
        self.threat_popup.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - popup_width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - popup_height) // 2)
        self.threat_popup.geometry(f"{popup_width}x{popup_height}+{x}+{y}")

        center_box = tk.Frame(self.threat_popup, bg=_P["threat_bg"])
        center_box.place(relx=0.5, rely=0.5, anchor="center")

        if is_fall:
            hdr_text = "💥 PHONE FALL / DROP DETECTED!"
            sub_text = "Sudden drop / impact detected via mobile phone sensors.\nIf uncancelled, emergency SOS alert will be dispatched to all saved contacts."
            btn_text = "🛡️ I AM SAFE (CANCEL SOS)"
        elif is_combined_anomaly:
            hdr_text = "CRITICAL: Speed Anomaly & Route Deviation"
            sub_text = f"{reason_msg}\nBoth speed and route limits were exceeded.\nIf you do not cancel within 10 seconds, an emergency alert will be dispatched."
            btn_text = "I'M SAFE (CANCEL SOS)"
        elif is_route_dev:
            hdr_text = "Route Deviation Detected"
            sub_text = f"{reason_msg}\nYou are more than the route safety limit from the planned path.\nIf you do not cancel within 10 seconds, an emergency alert will be dispatched."
            btn_text = "I'M SAFE (CANCEL SOS)"
        elif is_speed_anomaly:
            hdr_text = "Speed Anomaly Detected"
            sub_text = f"{reason_msg}\nIf you do not cancel within 10 seconds, an emergency alert will be dispatched."
            btn_text = "CANCEL"
        else:
            hdr_text = "⚠️ ROUTE DEVIATION DETECTED!"
            sub_text = f"{reason_msg}\nTrip has deviated from the planned safe route.\nIf you do not cancel within 10 seconds, an emergency alert will be dispatched."
            btn_text = "CANCEL"

        tk.Label(center_box, text=hdr_text,
                 font=("Segoe UI", 18, "bold"),
                 fg=_P["threat_fg"], bg=_P["threat_bg"]).pack(pady=(0, 4))
        tk.Label(center_box, text=sub_text,
                 font=("Segoe UI", 10), justify="center",
                 fg=_P["text_hi"], bg=_P["threat_bg"]).pack(pady=(0, 10))

        self.canvas_size  = 240
        self.timer_canvas = tk.Canvas(
            center_box, width=self.canvas_size, height=self.canvas_size,
            bg=_P["threat_bg"], highlightthickness=0,
        )
        self.timer_canvas.pack(pady=10)

        action_row = tk.Frame(center_box, bg=_P["threat_bg"])
        action_row.pack(fill="x", pady=18)
        tk.Button(
            action_row, text="CANCEL / SAFE",
            font=("Segoe UI", 11, "bold"),
            bg=_P["green"], fg="#ffffff", bd=0, cursor="hand2",
            padx=14, command=lambda: self.dismiss_alarm("User confirmed safe"),
        ).pack(side="left", fill="x", expand=True, ipady=12, padx=(0, 6))
        tk.Button(
            action_row, text="CONFIRM EMERGENCY",
            font=("Segoe UI", 11, "bold"),
            bg=_P["red"], fg="#ffffff", bd=0, cursor="hand2",
            padx=14, command=self.dispatch_sos,
        ).pack(side="right", fill="x", expand=True, ipady=12, padx=(6, 0))

        self.threat_popup.grab_set()
        self.threat_popup.focus_force()

        if self.controller:
            self.controller.start_emergency_recording()

        self.run_timer()

    def execute_adb_sos_workflow(self):
        phone_numbers = []
        if hasattr(self, 'controller') and hasattr(self.controller, 'get_emergency_contacts'):
            phone_numbers = self.controller.get_emergency_contacts()
        elif hasattr(self, 'contacts'):
            phone_numbers = self.contacts

        if not phone_numbers:
            print("No emergency contacts found!")
            return

        location_link = ""
        if hasattr(self, 'controller') and hasattr(self.controller, 'get_current_location_link'):
            location_link = self.controller.get_current_location_link()
        elif hasattr(self, 'location_module') and hasattr(self.location_module, 'get_current_url'):
            location_link = self.location_module.get_current_url()
        elif hasattr(self, 'current_location_url'):
            location_link = self.current_location_url
        elif self.controller and hasattr(self.controller, "location_engine"):
            loc = self.controller.location_engine.get_current_location()
            lat, lon = loc.get("latitude", 0.0), loc.get("longitude", 0.0)
            location_link = f"https://maps.google.com/?q={lat},{lon}"

        message = (
            "🚨 AURA SOS EMERGENCY ALERT! 🚨\n"
            "User in danger !!!\n"
            f"Live Location:\n{location_link}"
        )
        dispatcher = getattr(self.controller, "usb_dispatcher", None) if self.controller else None
        if dispatcher is None:
            print("❌ [SOS] ADB dispatcher unavailable; unable to send SMS or calls.")
            return

        def _dispatch_in_background():
            lat = lon = 0.0
            if self.controller and hasattr(self.controller, "location_engine"):
                current = self.controller.location_engine.get_current_location()
                lat = current.get("latitude", 0.0)
                lon = current.get("longitude", 0.0)
            results = dispatcher.dispatch_threat_alert(
                phone_numbers, lat, lon, message_text=message
            )
            if self.controller and hasattr(self.controller, "start_call_escalation"):
                self.controller._call_escalation_deadline = (
                    results.get("sms_completed_at", time.monotonic())
                )
                self.controller.start_call_escalation(phone_numbers, "MANUAL_SOS")

        threading.Thread(
            target=_dispatch_in_background,
            name="AURA-legacy-sos-dispatch",
            daemon=True,
        ).start()

    def draw_circular_timer(self):
        self.timer_canvas.delete("all")
        cx = cy = self.canvas_size / 2
        r = 90

        # Outer glow ring
        for offset in (14, 10, 6):
            self.timer_canvas.create_oval(
                cx - r - offset, cy - r - offset,
                cx + r + offset, cy + r + offset,
                outline="#7f1d1d", width=1)

        # Background arc
        self.timer_canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline="#7f1d1d", width=14)

        # Countdown arc
        if self.total_time > 0:
            angle = (self.time_left / self.total_time) * 360
            self.timer_canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=90, extent=-angle,
                outline=_P["red"], width=14, style="arc")

        # Centre label
        self.timer_canvas.create_text(
            cx, cy, text=f"{self.time_left}s",
            font=("Segoe UI", 36, "bold"), fill="#ffffff")

    def run_timer(self):
        if not self.is_threat_active:
            return
        if self.time_left >= 0:
            self.draw_circular_timer()
            if self.time_left == 0:
                self.dispatch_sos()
            else:
                self.time_left -= 1
                self.timer_job = self.root.after(1000, self.run_timer)

    def dismiss_alarm(self, reason):
        was_active = bool(
            getattr(self, "is_emergency_active", False)
            or getattr(self, "is_threat_active", False)
            or getattr(self, "_emergency_banner_frame", None)
        )
        self.reset_emergency_ui_state()

        # Reset controller status & apply cooldown to prevent unwanted repeated triggers
        if self.controller:
            if hasattr(self.controller, "acknowledge_user_safe"):
                # Safe/cancel closes this incident and immediately re-arms
                # monitoring; the anomaly engine still requires fresh samples.
                self.controller.acknowledge_user_safe(cooldown_seconds=0.0)
            elif hasattr(self.controller, "anomaly_engine"):
                self.controller.anomaly_engine.acknowledge_safe(cooldown_seconds=0.0)
            if hasattr(self.controller, "cancel_call_escalation"):
                self.controller.cancel_call_escalation()

        if was_active and self.is_protection_active:
            self.send_desktop_popup("✅ Alarm Dismissed", "User confirmed safe.")
        print(f"✅ [GUI] Emergency countdown cancelled: {reason}")

    def mark_emergency_active(self):
        global is_emergency_active
        is_emergency_active = True
        self.is_emergency_active = True
        if self.controller:
            self.controller.is_emergency_active = True
        # Marking can originate on the Vosk worker thread; always marshal Tk
        # changes onto the UI loop.
        try:
            if threading.current_thread() is threading.main_thread():
                self.trigger_emergency_ui_mode()
            else:
                self.root.after(0, self.trigger_emergency_ui_mode)
        except (tk.TclError, RuntimeError):
            pass

    def trigger_emergency_ui_mode(self, reason=None, dispatch_status=None):
        """Show a persistent, flashing SOS state without blocking map updates."""
        if reason:
            self.active_threat_reason = str(reason)
        reason_text = self.active_threat_reason or "Emergency alert activated"
        status_text = dispatch_status or "SIREN / ALERT ACTIVE  •  CONTACTING EMERGENCY CONTACTS"
        if hasattr(self, "_tele_threat"):
            self._tele_threat.set("🚨 SOS ACTIVE • SIREN")
        threat_label = getattr(self, "_tele_threat_label", None)
        if threat_label is not None and threat_label.winfo_exists():
            threat_label.configure(fg=_P["red"])

        overlay = getattr(self, "_emergency_overlay", None)
        if overlay is None or not overlay.winfo_exists():
            overlay = tk.Frame(self.main_container, bg="#7f1d1d",
                               highlightbackground="#ef4444", highlightthickness=2)
            overlay.place(relx=0.5, y=8, anchor="n", relwidth=0.72)
            self._emergency_overlay = overlay
            self._emergency_overlay_reason = tk.StringVar(value="")
            self._emergency_overlay_status = tk.StringVar(value="")
            tk.Label(overlay, text="🚨  EMERGENCY ALERT ACTIVE  🚨",
                     font=("Segoe UI", 15, "bold"), fg="#ffffff",
                     bg="#7f1d1d").pack(pady=(6, 1))
            tk.Label(overlay, textvariable=self._emergency_overlay_reason,
                     font=("Segoe UI", 9, "bold"), fg="#fee2e2",
                     bg="#7f1d1d").pack()
            tk.Label(overlay, textvariable=self._emergency_overlay_status,
                     font=("Segoe UI", 9, "bold"), fg="#fde68a",
                     bg="#7f1d1d").pack(pady=(1, 6))

        self._emergency_overlay_reason.set(reason_text)
        self._emergency_overlay_status.set(status_text)
        self._emergency_flash_on = False
        if self._emergency_flash_job:
            try:
                self.root.after_cancel(self._emergency_flash_job)
            except tk.TclError:
                pass
            self._emergency_flash_job = None
        self._flash_emergency_overlay()

    def _flash_emergency_overlay(self):
        overlay = getattr(self, "_emergency_overlay", None)
        if not self.is_emergency_active or overlay is None or not overlay.winfo_exists():
            return
        self._emergency_flash_on = not self._emergency_flash_on
        color = "#b91c1c" if self._emergency_flash_on else "#7f1d1d"
        try:
            overlay.configure(bg=color)
            for child in overlay.winfo_children():
                child.configure(bg=color)
        except tk.TclError:
            return
        self._emergency_flash_job = self.root.after(500, self._flash_emergency_overlay)

    def clear_emergency_banner(self):
        if self._emergency_flash_job:
            try:
                self.root.after_cancel(self._emergency_flash_job)
            except tk.TclError:
                pass
            self._emergency_flash_job = None
        overlay = getattr(self, "_emergency_overlay", None)
        self._emergency_overlay = None
        if overlay is not None:
            try:
                if overlay.winfo_exists():
                    overlay.destroy()
            except tk.TclError:
                pass
        banner = getattr(self, "_emergency_banner_frame", None)
        self._emergency_banner_frame = None
        if banner is not None:
            try:
                if banner.winfo_exists():
                    banner.destroy()
            except tk.TclError:
                pass

    def _start_popup_alert_chime(self):
        """Run a cancellable alert chime without blocking Tk or map updates."""
        self._popup_chime_stop.set()
        stop_event = threading.Event()
        self._popup_chime_stop = stop_event

        def _play():
            if not WINSOUND_AVAILABLE:
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
                except Exception as exc:
                    print(f"⚠️ [Alert Chime] Playback failed: {exc}")
                    break
                if stop_event.wait(1.2):
                    break

        threading.Thread(target=_play, name="AURA-popup-alert-chime", daemon=True).start()

    def _stop_popup_alert_chime(self):
        event = getattr(self, "_popup_chime_stop", None)
        if event is not None:
            event.set()

    def reset_emergency_ui_state(self):
        """Clear all emergency UI state and any active map safety countdown."""
        self._stop_popup_alert_chime()
        global is_emergency_active
        is_emergency_active = False
        self.is_emergency_active = False
        if self.controller:
            self.controller.is_emergency_active = False
        self.is_threat_active = False
        self.alert_active = False
        self.popup_showing = False
        self.active_threat_reason = ""
        self._last_threat_reason = ""
        self._last_threat_trigger_time = 0.0
        self._last_sos_dispatch_time = 0.0
        if self.timer_job:
            try:
                self.root.after_cancel(self.timer_job)
            except tk.TclError:
                pass
            self.timer_job = None
        self._close_threat_popup()
        self.clear_emergency_banner()
        if hasattr(self, "_tele_threat"):
            self._tele_threat.set("● MONITORING")
        threat_label = getattr(self, "_tele_threat_label", None)
        if threat_label is not None:
            try:
                threat_label.configure(fg=_P["green"])
            except tk.TclError:
                pass

        sim = getattr(self.controller, "map_simulator_window", None) if self.controller else None
        if sim is not None:
            if sim.safety_popup is not None:
                sim._close_safety_popup()
            sim._sos_active = False
            sim.safety_monitor.sos_active = False
            sim.safety_monitor.resolve(0.0)
            sim.safety_reason = ""
            if "safety_status" in sim._tele_vars:
                sim._tele_vars["safety_status"].set("OK")
            sim._draw_map()

    def _close_threat_popup(self):
        self._stop_popup_alert_chime()
        # Also reset these flags on timer expiry / SOS dispatch, where the
        # popup closes but the separate emergency-dispatch state stays active.
        self.alert_active = False
        self.popup_showing = False
        popup = getattr(self, "threat_popup", None)
        if popup is not None:
            try:
                if popup.winfo_exists():
                    popup.grab_release()
                    popup.destroy()
            except tk.TclError:
                pass
        self.threat_popup = None
        self.timer_canvas = None

    def dispatch_sos(self):
        """
        Core SOS dispatch. The countdown does not appear as it is already identified as an emergency.
        Gathers ALL numbers saved in the UI and dispatches offline alert with current location link.
        """
        # The popup tone is tied to countdown lifetime, even if dispatch is
        # disabled or suppressed by the duplicate-dispatch cooldown.
        self._stop_popup_alert_chime()
        if not self.is_protection_active:
            return

        now = time.time()
        if (now - getattr(self, "_last_sos_dispatch_time", 0.0)) < 20.0:
            print("ℹ️ [GUI] SOS dispatch debounced (<20s since last dispatch).")
            return
        self._last_sos_dispatch_time = now

        # Cancel any active countdown timer immediately
        self.is_threat_active = False
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None
        self._close_threat_popup()

        # --- Resolve all numbers saved in the UI ---
        def _read_entry(entry_widget, fallback=""):
            if entry_widget is not None and hasattr(entry_widget, "get"):
                val = entry_widget.get().strip()
                if val and not getattr(entry_widget, "_is_placeholder", False):
                    return val
            return fallback

        saved_numbers = []
        for c in self.contacts:
            if isinstance(c, dict):
                p = self.clean_phone(c.get("phone", ""))
            else:
                p = self.clean_phone(str(c))
            if p and p not in saved_numbers:
                saved_numbers.append(p)

        # Also inspect dashboard P1 / P2 entries if present
        fb_p1 = saved_numbers[0] if saved_numbers else ""
        fb_p2 = saved_numbers[1] if len(saved_numbers) > 1 else ""
        p1_val = _read_entry(getattr(self, "p1_entry", None), fb_p1)
        p2_val = _read_entry(getattr(self, "p2_entry", None), fb_p2)
        if p1_val and p1_val != "Not configured":
            c_p1 = self.clean_phone(p1_val)
            if c_p1 and c_p1 not in saved_numbers:
                saved_numbers.insert(0, c_p1)
        if p2_val and p2_val != "Not configured":
            c_p2 = self.clean_phone(p2_val)
            if c_p2 and c_p2 not in saved_numbers:
                if len(saved_numbers) > 1:
                    saved_numbers.insert(1, c_p2)
                else:
                    saved_numbers.append(c_p2)

        if not saved_numbers:
            saved_numbers = ["9876543210"]

        self.mark_emergency_active()
        self.trigger_emergency_ui_mode(
            reason=getattr(self, "active_threat_reason", "SOS activated"),
            dispatch_status="SIREN ACTIVE  •  DISPATCH IN PROGRESS",
        )

        if self.controller:
            self.controller.set_emergency_contacts(saved_numbers)

        # --- Gather verified location and execute emergency sequence ---
        current_loc = {}
        if self.controller:
            try:
                current_loc = self.controller.location_engine.get_current_location()
                incident_desc = getattr(self, "active_threat_reason", "MANUAL_SOS_TRIGGER")
                # SMS and calls can take several seconds; perform the dispatch
                # workflow away from Tk so the dashboard remains responsive.
                threading.Thread(
                    target=self.controller.execute_emergency_sequence,
                    args=(incident_desc, current_loc),
                    kwargs={"adb_sms": True, "immediate_calls": False},
                    daemon=True,
                    name="AURA-confirmed-emergency-dispatch",
                ).start()
            except Exception as e:
                print(f"[SOS Dispatch Error] {e}")
        else:
            # --- USB / Termux Emergency Dispatch Fallback ---
            lat = current_loc.get("lat", 11.488) if isinstance(current_loc, dict) else 11.488
            lon = current_loc.get("lon", 79.753) if isinstance(current_loc, dict) else 79.753

            if hasattr(self.controller, "usb_dispatcher"):
                threading.Thread(
                    target=self.controller.usb_dispatcher.dispatch_threat_alert,
                    args=(saved_numbers, lat, lon),
                    daemon=True,
                    name="USB-SOS-Dispatch"
                ).start()

        # --- UI feedback: show dispatched dashboard directly without countdown ---
        if not getattr(self, "is_fake_shutdown", False):
            contact_str = ", ".join(saved_numbers[:3])
            if len(saved_numbers) > 3:
                contact_str += f" (+{len(saved_numbers)-3} more)"
            self.send_desktop_popup(
                "🚨 EMERGENCY DISPATCH STARTED",
                f"Background SMS and priority call attempts started for: {contact_str}")
            self.build_modern_dashboard(
                pending_notification=f"🚨 EMERGENCY DISPATCH STARTED FOR {len(saved_numbers)} CONTACT(S)",
                is_error=True)
            self.trigger_emergency_ui_mode(
                reason=getattr(self, "active_threat_reason", "Emergency alert dispatched"),
                dispatch_status=f"DISPATCH STARTED  •  SENDING TO {len(saved_numbers)} CONTACT(S)",
            )
            self.root.after(
                5000,
                lambda: threading.Thread(
                    target=self.refresh_media_lists, daemon=True).start())

    # ═══════════════════════════════════════════════════════════════════════════
    # 8 – FAKE SHUTDOWN DEMO (F8)
    # ═══════════════════════════════════════════════════════════════════════════

    def trigger_fake_shutdown(self, event=None):
        if getattr(self, "is_fake_shutdown", False):
            return
        self.is_fake_shutdown = True
        if self.controller:
            self.controller.stealth_active = True

        self.shutdown_win = tk.Toplevel(self.root)
        self.shutdown_win.attributes("-fullscreen", True)
        self.shutdown_win.configure(bg="black")
        self.shutdown_win.overrideredirect(True)
        self.shutdown_win.attributes("-topmost", True)
        self.shutdown_win.focus_force()

        msg_frame = tk.Frame(self.shutdown_win, bg="black")
        msg_frame.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(msg_frame, text="Shutting down…",
                 font=("Segoe UI", 22), fg="white", bg="black").pack()

        def _black():
            if hasattr(self, "shutdown_win") and self.shutdown_win \
                    and self.shutdown_win.winfo_exists():
                for w in msg_frame.winfo_children():
                    w.destroy()

        self.root.after(3000, _black)
        self.shutdown_win.bind_all("<F9>", lambda e: self.exit_fake_shutdown())
        print("[Fake Shutdown] Activated. Background SOS running.")
        self.dispatch_sos()

    def exit_fake_shutdown(self, event=None):
        if hasattr(self, "shutdown_win") and self.shutdown_win:
            try:
                self.shutdown_win.unbind_all("<F9>")
                self.shutdown_win.destroy()
            except Exception:
                pass
        self.shutdown_win    = None
        self.is_fake_shutdown = False
        if self.controller:
            self.controller.stealth_active = False
        print("[Fake Shutdown] Exit complete.")

    # ═══════════════════════════════════════════════════════════════════════════
    # 9 – VOICE HANDLER
    # ═══════════════════════════════════════════════════════════════════════════

    def handle_voice_event(self, action, word):
        if not self.is_protection_active:
            return
        if action == "TRIGGER":
            self.root.after(0, self.trigger_threat)
        elif action == "CANCEL":
            self.root.after(0, lambda: self._handle_voice_cancel(word))

    def cancel_active_safety_check(self, reason="User confirmed safe"):
        """Close the Map Simulator safety modal and resolve its countdown."""
        sim = getattr(self.controller, "map_simulator_window", None) if self.controller else None
        if sim is None or not sim._safety_popup_is_open():
            return False
        sim._cancel_safety_check()
        print(f"✅ [GUI] Safety Check dismissed: {reason}")
        return True

    def _handle_voice_cancel(self, word):
        reason = f"Voice ('{word}')"
        if self.cancel_active_safety_check(reason):
            return
        if self.is_threat_active:
            self.dismiss_alarm(reason)
        elif self.controller and hasattr(self.controller, "acknowledge_user_safe"):
            self.controller.acknowledge_user_safe()

    # ═══════════════════════════════════════════════════════════════════════════
    # 10 – REALISTIC VECTOR MAP RENDERING & DYNAMIC NAVIGATION HUD
    # ═══════════════════════════════════════════════════════════════════════════

    def _draw_map_grid(self):
        """Builds and paints the full realistic cartographic city map."""
        self._draw_realistic_map()

    def _draw_realistic_map(self):
        """Renders a realistic dark-mode cartographic vector city map."""
        if not hasattr(self, "map_canvas") or not self.map_canvas.winfo_exists():
            return
        self.map_canvas.update_idletasks()
        w = self.map_canvas.winfo_width()
        h = self.map_canvas.winfo_height()
        if w <= 1:
            w = 480
        if h <= 1:
            h = 280

        self.map_canvas.delete("map_base")
        sz = (w, h)

        # ── 1. Base Terrain Background (Deep Dark Slate Asphalt) ──────────────
        self.map_canvas.create_rectangle(0, 0, w, h, fill="#0c1322", outline="", tags="map_base")

        # ── 2. Natural Features: Waterway (Marina Riverfront Channel) ─────────
        river_latlons = [
            (11.515, 79.768),
            (11.495, 79.756),
            (11.472, 79.744),
            (11.450, 79.739),
            (11.428, 79.733),
            (11.405, 79.728)
        ]
        river_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in river_latlons]
        flat_river = [coord for pt in river_pixels for coord in pt]
        if len(flat_river) >= 4:
            # Shoreline outline bank
            self.map_canvas.create_line(flat_river, fill="#0284c7", width=34, smooth=True, tags="map_base")
            # Inner deep marine water body
            self.map_canvas.create_line(flat_river, fill="#034b75", width=28, smooth=True, tags="map_base")
            # Waterway name label
            mid_x, mid_y = river_pixels[len(river_pixels) // 2]
            self.map_canvas.create_text(
                mid_x + 18, mid_y - 12, text="≋ Riverfront Marine Channel ≋",
                fill="#38bdf8", font=("Segoe UI", 7, "italic"), tags="map_base"
            )

        # ── 3. Ecological Green Reserves & Public Parks ───────────────────────
        # Sanctuary Nature Reserve (North-West)
        p1 = map_config.latlon_to_pixel(11.508, 79.675, sz)
        p2 = map_config.latlon_to_pixel(11.478, 79.715, sz)
        self.map_canvas.create_rectangle(p1[0], p1[1], p2[0], p2[1], fill="#064e3b", outline="#059669", width=1, tags="map_base")
        self.map_canvas.create_text((p1[0] + p2[0]) / 2, p1[1] + 14, text="🌲 Sanctuary Nature Reserve", fill="#34d399", font=("Segoe UI", 8, "bold"), tags="map_base")

        # University Botanical Campus Gardens (North-East)
        bg1 = map_config.latlon_to_pixel(11.502, 79.750, sz)
        bg2 = map_config.latlon_to_pixel(11.486, 79.775, sz)
        self.map_canvas.create_rectangle(bg1[0], bg1[1], bg2[0], bg2[1], fill="#064e3b", outline="#059669", width=1, tags="map_base")
        self.map_canvas.create_text((bg1[0] + bg2[0]) / 2, bg1[1] + 12, text="🌿 Botanical Campus Gardens", fill="#34d399", font=("Segoe UI", 7, "bold"), tags="map_base")

        # ── 4. Urban Parcels & City District Blocks ───────────────────────────
        city_blocks = [
            (11.458, 79.702, 11.442, 79.720, "Commerce District"),
            (11.474, 79.702, 11.464, 79.720, "Tech Sector"),
            (11.458, 79.728, 11.442, 79.742, "Medical Enclave"),
            (11.438, 79.702, 11.422, 79.722, "South Residential"),
            (11.438, 79.742, 11.422, 79.760, "Harbor Quarters"),
        ]
        for b_n, b_w, b_s, b_e, b_name in city_blocks:
            bp1 = map_config.latlon_to_pixel(b_n, b_w, sz)
            bp2 = map_config.latlon_to_pixel(b_s, b_e, sz)
            self.map_canvas.create_rectangle(bp1[0], bp1[1], bp2[0], bp2[1], fill="#151f32", outline="#253349", width=1, tags="map_base")

        # ── 5. Multi-Tier Road Network ────────────────────────────────────────
        # Secondary streets (Local Grid)
        for s_lat in [11.438, 11.480, 11.498]:
            sp1 = map_config.latlon_to_pixel(s_lat, 79.670, sz)
            sp2 = map_config.latlon_to_pixel(s_lat, 79.780, sz)
            self.map_canvas.create_line(sp1[0], sp1[1], sp2[0], sp2[1], fill="#27364b", width=3, tags="map_base")

        for s_lon in [79.692, 79.750, 79.768]:
            sp1 = map_config.latlon_to_pixel(11.410, s_lon, sz)
            sp2 = map_config.latlon_to_pixel(11.515, s_lon, sz)
            self.map_canvas.create_line(sp1[0], sp1[1], sp2[0], sp2[1], fill="#27364b", width=3, tags="map_base")

        # Primary Avenues: Sanctuary Boulevard (East-West)
        sb1 = map_config.latlon_to_pixel(11.462, 79.670, sz)
        sb2 = map_config.latlon_to_pixel(11.462, 79.780, sz)
        self.map_canvas.create_line(sb1[0], sb1[1], sb2[0], sb2[1], fill="#1e293b", width=8, tags="map_base")
        self.map_canvas.create_line(sb1[0], sb1[1], sb2[0], sb2[1], fill="#475569", width=5, tags="map_base")
        self.map_canvas.create_text(w * 0.22, sb1[1] - 8, text="Sanctuary Blvd", fill="#94a3b8", font=("Segoe UI", 7, "bold"), tags="map_base")

        # Primary Avenues: Campus Avenue (North-South)
        ca1 = map_config.latlon_to_pixel(11.410, 79.725, sz)
        ca2 = map_config.latlon_to_pixel(11.515, 79.725, sz)
        self.map_canvas.create_line(ca1[0], ca1[1], ca2[0], ca2[1], fill="#1e293b", width=8, tags="map_base")
        self.map_canvas.create_line(ca1[0], ca1[1], ca2[0], ca2[1], fill="#475569", width=5, tags="map_base")
        self.map_canvas.create_text(ca1[0] + 32, h * 0.18, text="Campus Ave", fill="#94a3b8", font=("Segoe UI", 7, "bold"), tags="map_base")

        # Metro Transit Way
        mw1 = map_config.latlon_to_pixel(11.442, 79.705, sz)
        mw2 = map_config.latlon_to_pixel(11.462, 79.725, sz)
        self.map_canvas.create_line(mw1[0], mw1[1], mw2[0], mw2[1], fill="#1e293b", width=7, tags="map_base")
        self.map_canvas.create_line(mw1[0], mw1[1], mw2[0], mw2[1], fill="#64748b", width=4, tags="map_base")
        self.map_canvas.create_text((mw1[0] + mw2[0]) / 2 - 10, (mw1[1] + mw2[1]) / 2 + 10, text="Metro Corridor", fill="#94a3b8", font=("Segoe UI", 6), tags="map_base")

        # Major Expressway: NH-45 Express Arterial (Diagonal with center divider)
        hway_pts = [
            (11.410, 79.670),
            (11.435, 79.700),
            (11.460, 79.728),
            (11.485, 79.752),
            (11.515, 79.780)
        ]
        hw_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in hway_pts]
        flat_hw = [coord for pt in hw_pixels for coord in pt]
        # Highway dark casing
        self.map_canvas.create_line(flat_hw, fill="#0b101d", width=14, smooth=True, tags="map_base")
        # Highway amber roadbed
        self.map_canvas.create_line(flat_hw, fill="#f59e0b", width=9, smooth=True, tags="map_base")
        # Dashed white center line
        self.map_canvas.create_line(flat_hw, fill="#ffffff", width=1.5, dash=(6, 4), smooth=True, tags="map_base")
        # Route Shield Badge
        hw_x, hw_y = hw_pixels[len(hw_pixels) // 2]
        self.map_canvas.create_rectangle(hw_x - 32, hw_y - 18, hw_x + 32, hw_y - 4, fill="#1e3a8a", outline="#3b82f6", width=1, tags="map_base")
        self.map_canvas.create_text(hw_x, hw_y - 11, text="NH-45 EXPRESS", fill="#ffffff", font=("Segoe UI", 6, "bold"), tags="map_base")

        # ── 6. Dynamic Safe Zones ─────────────────────────────────────────────
        landmarks = map_config.get_dynamic_landmarks(self.controller)
        for label, (llat, llon) in landmarks.items():
            lx, ly = map_config.latlon_to_pixel(llat, llon, sz)
            self.map_canvas.create_oval(lx - 24, ly - 24, lx + 24, ly + 24, outline="#10b981", width=1.5, dash=(4, 3), fill="#064e3b", stipple="gray25", tags="map_base")
            self.map_canvas.create_oval(lx - 5, ly - 5, lx + 5, ly + 5, fill="#10b981", outline="#ffffff", width=1, tags="map_base")
            self.map_canvas.create_text(lx, ly - 14, text=label, fill="#6ee7b7", font=("Segoe UI", 7, "bold"), tags="map_base")

        # ── 7. City Key Landmarks ─────────────────────────────────────────────
        city_pois = [
            ("🚇 Metro Central", 11.445, 79.708, "#38bdf8"),
            ("🏥 Apex Hospital", 11.455, 79.738, "#f43f5e"),
            ("👮 Police Precinct 4", 11.468, 79.712, "#60a5fa"),
            ("🎓 Annamalai University", 11.4925, 79.758, "#c084fc"),
        ]
        for poi_name, p_lat, p_lon, poi_color in city_pois:
            px, py = map_config.latlon_to_pixel(p_lat, p_lon, sz)
            self.map_canvas.create_oval(px - 4, py - 4, px + 4, py + 4, fill=poi_color, outline="#ffffff", width=1, tags="map_base")
            self.map_canvas.create_text(px, py + 11, text=poi_name, fill=poi_color, font=("Segoe UI", 7, "bold"), tags="map_base")

        # ── 8. Draw Planned Safe Corridor Route ───────────────────────────────
        self._draw_safe_route_corridor(sz)

        # ── 9. On-Canvas Interactive D-Pad ────────────────────────────────────
        self._draw_canvas_dpad(w, h)

        self._map_base_drawn = True

    def _draw_safe_route_corridor(self, sz):
        self.map_canvas.delete("route_corridor")
        corridor_pts = self._get_safe_route_corridor_points()
        if self.controller and hasattr(self.controller, "anomaly_engine"):
            dense_route = []
            for start, end in zip(corridor_pts, corridor_pts[1:]):
                for step in range(25):
                    fraction = step / 25
                    dense_route.append((
                        start[0] + (end[0] - start[0]) * fraction,
                        start[1] + (end[1] - start[1]) * fraction,
                    ))
            dense_route.append(corridor_pts[-1])
            self.controller.anomaly_engine.set_planned_route(dense_route)

        corr_pixels = [map_config.latlon_to_pixel(lat, lon, sz) for lat, lon in corridor_pts]
        flat_corr = [coord for pt in corr_pixels for coord in pt]

        # 1. Translucent Safe Corridor Buffer
        self.map_canvas.create_line(flat_corr, fill="#065f46", width=26, smooth=True, tags="route_corridor")
        # 2. Glowing Navigation Line
        self.map_canvas.create_line(flat_corr, fill="#10b981", width=4, smooth=True, tags="route_corridor")

        # 3. Waypoint heading chevrons along path
        for i in range(len(corr_pixels) - 1):
            mx = (corr_pixels[i][0] + corr_pixels[i+1][0]) / 2
            my = (corr_pixels[i][1] + corr_pixels[i+1][1]) / 2
            self.map_canvas.create_text(mx, my, text="▶", fill="#ffffff", font=("Segoe UI", 7, "bold"), tags="route_corridor")

        # 4. Destination Flag Marker
        dx, dy = corr_pixels[-1]
        dest = getattr(self.controller, "trip_destination", None) if self.controller else None
        dest_name = dest.get("name", "Campus Sanctuary") if dest else "Campus Sanctuary"
        self.map_canvas.create_oval(dx - 9, dy - 9, dx + 9, dy + 9, fill="#a855f7", outline="#ffffff", width=2, tags="route_corridor")
        self.map_canvas.create_text(dx, dy - 16, text=f"🏁 {dest_name}", fill="#e9d5ff", font=("Segoe UI", 8, "bold"), tags="route_corridor")

    def _get_safe_route_corridor_points(self):
        dest = getattr(self.controller, "trip_destination", None) if self.controller else None
        if dest and dest.get("latitude") and dest.get("longitude"):
            dest_lat = dest["latitude"]
            dest_lon = dest["longitude"]
        else:
            dest_lat = 11.49250
            dest_lon = 79.75800

        start_lat = 11.45800
        start_lon = 79.72000

        return [
            (start_lat, start_lon),
            (11.46200, 79.72500),
            (11.47500, 79.73800),
            (11.48500, 79.75000),
            (dest_lat, dest_lon)
        ]

    def _draw_canvas_dpad(self, w, h):
        self.map_canvas.delete("dpad")
        cx, cy = w - 46, h - 46
        r = 34
        # Semi-transparent background disc
        self.map_canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#0f172a", outline="#334155", width=1.5, tags="dpad")

        # Arrow buttons
        self.map_canvas.create_text(cx, cy - 20, text="▲", fill="#38bdf8", font=("Segoe UI", 10, "bold"), tags="dpad")
        self.map_canvas.create_text(cx, cy + 20, text="▼", fill="#38bdf8", font=("Segoe UI", 10, "bold"), tags="dpad")
        self.map_canvas.create_text(cx - 20, cy, text="◀", fill="#38bdf8", font=("Segoe UI", 10, "bold"), tags="dpad")
        self.map_canvas.create_text(cx + 20, cy, text="▶", fill="#38bdf8", font=("Segoe UI", 10, "bold"), tags="dpad")
        self.map_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill="#0284c7", outline="", tags="dpad")

    def update_map_canvas(self, lat, lon, predicted, speed_kmh, is_threat):
        """
        Called by main_controller every safety cycle and during free roaming.
        Updates GPS puck, heading arrow, live HUD tag, breadcrumb trail, and telemetry.
        """
        if not hasattr(self, "map_canvas") or not self.map_canvas.winfo_exists():
            return

        w = self.map_canvas.winfo_width()
        h = self.map_canvas.winfo_height()
        if w <= 1:
            w = 480
        if h <= 1:
            h = 280
        if not getattr(self, "_map_base_drawn", False):
            self._draw_realistic_map()

        sz = (w, h)
        if lat and lon and not (lat == 0.0 and lon == 0.0):
            self.current_sim_lat = lat
            self.current_sim_lon = lon
            if abs(lat - map_config.CENTER_LAT) > map_config.ZOOM_SPAN_DEGREES * 0.45 or \
               abs(lon - map_config.CENTER_LON) > map_config.ZOOM_SPAN_DEGREES * 0.45:
                map_config.set_map_center(lat, lon)
                self._draw_realistic_map()
                self.map_trail_points = []

        px, py = map_config.latlon_to_pixel(lat, lon, sz)

        # ── Breadcrumb trail (smooth, continuous segments only) ───────────────
        if self.map_trail_points:
            lx, ly = self.map_trail_points[-1]
            dist = math.hypot(px - lx, py - ly)
            # Only connect points within reasonable movement threshold (< 65px)
            # Route resets, teleports, or recentering jumps do NOT draw connecting lines
            if 2.0 <= dist <= 65.0:
                trail_color = "#f43f5e" if is_threat else "#38bdf8"
                self.map_canvas.create_line(lx, ly, px, py, fill=trail_color, width=2.5, tags="trail")
        self.map_trail_points.append((px, py))
        if len(self.map_trail_points) > 100:
            self.map_trail_points.pop(0)

        # ── Predicted Marker ──────────────────────────────────────────────────
        self.map_canvas.delete("predicted_marker")
        if predicted:
            p_lat = predicted.get("predicted_latitude") or predicted.get("latitude")
            p_lon = predicted.get("predicted_longitude") or predicted.get("longitude")
            if p_lat is not None and p_lon is not None and (p_lat != 0.0 or p_lon != 0.0):
                ppx, ppy = map_config.latlon_to_pixel(p_lat, p_lon, sz)
                self.map_canvas.create_oval(ppx - 8, ppy - 8, ppx + 8, ppy + 8, outline=_P["predict"], width=2, tags="predicted_marker")

        # ── Live User GPS Navigation Puck (Realistic Puck with Heading Pointer)
        self.map_canvas.delete("current_marker", "radar_ping", "hud_marker")

        # Radar ripple rings
        pulse_color = "#f43f5e" if is_threat else "#38bdf8"
        for r_off, w_stroke in [(22, 1), (15, 1.5)]:
            self.map_canvas.create_oval(px - r_off, py - r_off, px + r_off, py + r_off, outline=pulse_color, width=w_stroke, tags="radar_ping")

        # Drop shadow
        self.map_canvas.create_oval(px - 9, py - 7, px + 9, py + 11, fill="#020617", outline="", tags="current_marker")

        # Outer high-contrast white ring
        self.map_canvas.create_oval(px - 10, py - 10, px + 10, py + 10, fill="#ffffff", outline="", tags="current_marker")

        # Inner vibrant GPS disc
        disc_color = _P["red"] if is_threat else "#0284c7"
        self.map_canvas.create_oval(px - 8, py - 8, px + 8, py + 8, fill=disc_color, outline="", tags="current_marker")

        # Heading Direction Arrowhead
        heading = getattr(self, "user_heading_deg", 45.0)
        rad = math.radians(heading)
        tip_x = px + 7 * math.sin(rad)
        tip_y = py - 7 * math.cos(rad)
        base_left_x  = px + 5 * math.sin(rad + 2.5)
        base_left_y  = py - 5 * math.cos(rad + 2.5)
        base_right_x = px + 5 * math.sin(rad - 2.5)
        base_right_y = py - 5 * math.cos(rad - 2.5)
        self.map_canvas.create_polygon(tip_x, tip_y, base_left_x, base_left_y, px, py, base_right_x, base_right_y, fill="#ffffff", outline="", tags="current_marker")

        # Live HUD Callout Pill Tag
        st_name = self.get_nearest_street_name(lat, lon)
        is_disp_only = getattr(self, "display_only_mode", True)
        if is_threat:
            if is_disp_only:
                hud_text = f"📍 YOU • {speed_kmh:.0f} km/h • {st_name} (SHOW ONLY)"
                hud_bg = "#0f172a"
                hud_fg = "#38bdf8"
                hud_border = "#0284c7"
            else:
                hud_text = f"📍 YOU • {speed_kmh:.0f} km/h • {st_name}"
                hud_bg = "#7f1d1d"
                hud_fg = "#fecaca"
                hud_border = "#ef4444"
        else:
            hud_text = f"📍 YOU • {speed_kmh:.0f} km/h • {st_name}"
            hud_bg = "#0f172a"
            hud_fg = "#38bdf8"
            hud_border = "#0284c7"

        tag_y = py - 24 if py > 45 else py + 26
        box_w = max(len(hud_text) * 3.6 + 10, 50)
        self.map_canvas.create_rectangle(px - box_w, tag_y - 8, px + box_w, tag_y + 8, fill=hud_bg, outline=hud_border, width=1.5, tags="hud_marker")
        self.map_canvas.create_text(px, tag_y, text=hud_text, fill=hud_fg, font=("Segoe UI", 7, "bold"), tags="hud_marker")

        # ── Telemetry Sidebar Live Update ─────────────────────────────────────
        self.speed_indicator_str.set(f"Speed: {speed_kmh} km/h")
        self._tele_speed.set(f"{speed_kmh} km/h")

        if hasattr(self, "_tele_dest"):
            if self.controller and getattr(self.controller, "trip_destination", None):
                d = self.controller.trip_destination
                dest_name = d.get('name', 'Active')
                self._tele_dest.set(dest_name)
                if hasattr(self, "destination_status_str"):
                    self.destination_status_str.set(f"🏁 Target: {dest_name}")
            else:
                self._tele_dest.set("None set")
                if hasattr(self, "destination_status_str"):
                    self.destination_status_str.set("🏁 Destination: None")

        if self.controller and hasattr(self.controller, "predictor"):
            try:
                summary = self.controller.predictor.get_history_summary()
                self._tele_seg_dist.set(f"{summary.get('last_segment_m', 0.0):.1f} m")
                self._tele_cum_dist.set(f"{summary.get('cumulative_distance_km', 0.0):.3f} km")
                pred2 = self.controller.predictor.predict_next_coordinate()
                if pred2:
                    self._tele_pred_lat.set(f"{pred2.get('predicted_latitude', '--'):.5f}")
                    self._tele_pred_lon.set(f"{pred2.get('predicted_longitude', '--'):.5f}")
                    self._tele_conf.set(f"{pred2.get('confidence', 0.0) * 100:.1f}%")
            except Exception:
                pass

        if is_threat:
            self._tele_threat.set("👁️ DEVIATION (SHOW ONLY)" if is_disp_only else "● THREAT ⚠️")
        else:
            self._tele_threat.set("👁️ SHOW ONLY" if is_disp_only else "● MONITORING")

        if self.controller and hasattr(self.controller, "phone_has_signal"):
            sig = self.controller.phone_has_signal
            self._tele_network.set("Connected ✅" if sig else "Offline ⚠️")

    def clear_map_display(self):
        if hasattr(self, "map_canvas") and self.map_canvas.winfo_exists():
            self.map_canvas.delete("trail", "current_marker", "predicted_marker", "hud_marker", "radar_ping")
        self.map_trail_points = []
        self.speed_indicator_str.set("Speed: -- km/h")
        self._tele_speed.set("-- km/h")

    def open_map_simulator(self):
        if not self.controller:
            messagebox.showwarning(
                "No Controller",
                "Map Simulator requires the Safety Controller to be running.")
            return
        try:
            map_simulator.open_map_simulator(self.root, self.controller)
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("Map Simulator Error",
                                 f"Could not open Map Simulator:\n\n{e}")


# ── Standalone test entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app  = ModernSafetyApp(root)
    root.mainloop()
