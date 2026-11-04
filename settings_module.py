import os
import json
import tkinter as tk
from tkinter import messagebox

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PROFILE_PATH = os.path.join(DATA_DIR, "user_profile.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULT_PROFILE = {
    "user_name": "",
    "user_phone": "",
    "contacts": [],
    "delay_timer": 15,
    "escalation_timeout": 60,
    "speed_threshold": 25,
    "is_protection_active": False,
}

PERSISTED_CONFIG_KEYS = ("delay_timer", "escalation_timeout", "speed_threshold")


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_user_profile(path=None):
    """Load settings from local JSON. Returns a complete dict with defaults."""
    _ensure_data_dir()
    target = path or PROFILE_PATH
    if not os.path.exists(target) and os.path.exists(CONFIG_PATH):
        target = CONFIG_PATH
    data = dict(DEFAULT_PROFILE)
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data.update({key: raw[key] for key in DEFAULT_PROFILE if key in raw})
        except Exception as exc:
            print(f"[Settings] Profile load notice: {exc}")
    # config.json is authoritative for operational settings. This lets changes
    # persist even when a saved user profile predates the settings file.
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            if isinstance(config, dict):
                for key in PERSISTED_CONFIG_KEYS:
                    if key in config:
                        data[key] = config[key]
        except Exception as exc:
            print(f"[Settings] Config load notice: {exc}")
    if not isinstance(data.get("contacts"), list):
        data["contacts"] = []
    return data


def save_user_profile(payload, path=None):
    """Overwrite local JSON settings file."""
    _ensure_data_dir()
    target = path or PROFILE_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    merged = dict(DEFAULT_PROFILE)
    if isinstance(payload, dict):
        merged.update(payload)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, ensure_ascii=False)
    save_config(merged)
    return merged


def save_config(payload, path=None):
    """Persist operational settings in data/config.json."""
    _ensure_data_dir()
    target = path or CONFIG_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    loaded_config = {}
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                loaded_config.update(loaded)
        except Exception as exc:
            print(f"[Settings] Config read notice: {exc}")
    current = {key: loaded_config[key] for key in PERSISTED_CONFIG_KEYS if key in loaded_config}
    for key in PERSISTED_CONFIG_KEYS:
        if isinstance(payload, dict) and key in payload:
            current[key] = payload[key]
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
    return current


def apply_profile_to_app(app, profile=None):
    """Copy JSON fields onto the GUI app without filling registration widgets."""
    p = profile if profile is not None else load_user_profile(getattr(app, "profile_path", PROFILE_PATH))
    app.user_name = p.get("user_name", "") or ""
    app.user_phone = p.get("user_phone", "") or ""
    app.contacts = list(p.get("contacts") or [])
    dt = p.get("delay_timer", 15)
    try:
        app.delay_timer.set(dt if dt in (15, 30) else int(dt))
    except Exception:
        pass
    try:
        app.escalation_timeout_var.set(max(20, min(120, int(p.get("escalation_timeout", 60)))))
        app.speed_threshold_var.set(int(p.get("speed_threshold", 25)))
    except Exception:
        pass
    app.is_protection_active = bool(p.get("is_protection_active", False))
    if app.contacts and app.controller:
        app.controller.set_emergency_contacts([c.get("phone") for c in app.contacts if c.get("phone")])
        app.controller.call_escalation_timeout_sec = app.escalation_timeout_var.get()
        if hasattr(app.controller, "anomaly_engine"):
            app.controller.anomaly_engine.set_speed_threshold(app.speed_threshold_var.get())
    print(f"[Settings] Loaded offline profile ({len(app.contacts)} contacts) from JSON.")
    return p


def collect_profile_from_app(app):
    return {
        "user_name": getattr(app, "user_name", ""),
        "user_phone": getattr(app, "user_phone", ""),
        "contacts": list(getattr(app, "contacts", []) or []),
        "delay_timer": app.delay_timer.get() if hasattr(app, "delay_timer") else 15,
        "escalation_timeout": app.escalation_timeout_var.get() if hasattr(app, "escalation_timeout_var") else 60,
        "speed_threshold": app.speed_threshold_var.get() if hasattr(app, "speed_threshold_var") else 25,
        "is_protection_active": bool(getattr(app, "is_protection_active", False)),
    }


def open_settings_dialog(app):
    """Settings UI: identity, contacts, timers. No PIN fields or Safe Zone editor."""
    from gui_module import _P

    dlg = tk.Toplevel(app.root)
    dlg.title("AURA System Settings")
    dlg.geometry("560x620")
    dlg.configure(bg=_P["bg_panel"])
    dlg.transient(app.root)
    dlg.grab_set()

    title_row = tk.Frame(dlg, bg=_P["bg_panel"])
    title_row.pack(pady=12)
    tk.Label(title_row, text="SYSTEM CONFIGURATION (OFFLINE JSON)",
             font=("Segoe UI", 12, "bold"),
             fg=_P["accent"], bg=_P["bg_panel"]).pack()
    tk.Label(title_row, text="Saved locally to data/user_profile.json",
             font=("Segoe UI", 8), fg=_P["text_dim"], bg=_P["bg_panel"]).pack()

    frm = tk.Frame(dlg, bg=_P["bg_panel"], padx=22)
    frm.pack(fill="both", expand=True)

    usr_row = tk.Frame(frm, bg=_P["bg_panel"])
    usr_row.pack(fill="x", pady=(2, 8))

    u_col = tk.Frame(usr_row, bg=_P["bg_panel"])
    u_col.pack(side="left", fill="x", expand=True, padx=(0, 6))
    tk.Label(u_col, text="REGISTERED USER NAME", font=("Segoe UI", 8, "bold"),
             fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
    e_name = tk.Entry(u_col, font=("Segoe UI", 10), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
    e_name.insert(0, app.user_name)
    e_name.pack(fill="x", ipady=5, pady=(2, 0))

    p_col = tk.Frame(usr_row, bg=_P["bg_panel"])
    p_col.pack(side="right", fill="x", expand=True, padx=(6, 0))
    tk.Label(p_col, text="USER PHONE (10-DIGIT)", font=("Segoe UI", 8, "bold"),
             fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
    e_phone = tk.Entry(p_col, font=("Segoe UI", 10), bg=_P["bg_input"], fg=_P["text_hi"], bd=0)
    e_phone.insert(0, app.user_phone)
    e_phone.pack(fill="x", ipady=5, pady=(2, 0))

    thresh_row = tk.Frame(frm, bg=_P["bg_panel"])
    thresh_row.pack(fill="x", pady=(2, 10))

    tmr_col = tk.Frame(thresh_row, bg=_P["bg_panel"])
    tmr_col.pack(side="left", fill="x", expand=True, padx=(0, 4))
    tk.Label(tmr_col, text="SOS COUNTDOWN", font=("Segoe UI", 8, "bold"),
             fg=_P["amber"], bg=_P["bg_panel"]).pack(anchor="w")
    tk.Spinbox(tmr_col, from_=5, to=60, increment=5, textvariable=app.delay_timer,
               font=("Segoe UI", 10, "bold"), bg=_P["bg_input"], fg=_P["amber"], bd=0).pack(fill="x", ipady=5)

    escl_col = tk.Frame(thresh_row, bg=_P["bg_panel"])
    escl_col.pack(side="left", fill="x", expand=True, padx=4)
    tk.Label(escl_col, text="CALL RETRY INTERVAL (20-120s)", font=("Segoe UI", 8, "bold"),
             fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
    tk.Spinbox(escl_col, from_=20, to=120, increment=10, textvariable=app.escalation_timeout_var,
               font=("Segoe UI", 10, "bold"), bg=_P["bg_input"], fg=_P["text_hi"], bd=0).pack(fill="x", ipady=5)

    speed_col = tk.Frame(thresh_row, bg=_P["bg_panel"])
    speed_col.pack(side="right", fill="x", expand=True, padx=(4, 0))
    tk.Label(speed_col, text="SPEED THRESHOLD (KM/H)", font=("Segoe UI", 8, "bold"),
             fg=_P["accent2"], bg=_P["bg_panel"]).pack(anchor="w")
    tk.Spinbox(speed_col, from_=20, to=140, increment=5, textvariable=app.speed_threshold_var,
               font=("Segoe UI", 10, "bold"), bg=_P["bg_input"], fg=_P["text_hi"], bd=0).pack(fill="x", ipady=5)

    contacts_hdr = tk.Frame(frm, bg=_P["bg_panel"])
    contacts_hdr.pack(fill="x", pady=(6, 6))
    tk.Label(contacts_hdr, text="EMERGENCY CONTACTS  (top = highest priority)",
             font=("Segoe UI", 8, "bold"), fg=_P["accent2"], bg=_P["bg_panel"]).pack(side="left")

    contacts_frame = tk.Frame(frm, bg=_P["bg_panel"])
    contacts_frame.pack(fill="x", pady=4)
    settings_contact_rows = []

    def render_contact_rows():
        for r in settings_contact_rows:
            r["frame"].destroy()
        settings_contact_rows.clear()
        for idx, contact in enumerate(app.contacts):
            row = tk.Frame(contacts_frame, bg=_P["bg_card2"])
            row.pack(fill="x", pady=3, ipady=3)
            tk.Label(row, text=f"P{idx+1}", font=("Segoe UI", 9, "bold"),
                     fg=_P["amber"], bg=_P["bg_card2"], width=3).pack(side="left", padx=(4, 4))
            ec_n = tk.Entry(row, font=("Segoe UI", 9), bg=_P["bg_input"], fg=_P["text_hi"], bd=0, width=16)
            ec_n.insert(0, contact.get("name", ""))
            ec_n.pack(side="left", padx=2, ipady=4)
            ec_p = tk.Entry(row, font=("Segoe UI", 9), bg=_P["bg_input"], fg=_P["text_hi"], bd=0, width=13)
            ec_p.insert(0, contact.get("phone", ""))
            ec_p.pack(side="left", padx=2, ipady=4)

            def _up(i=idx):
                if i > 0:
                    app.contacts[i - 1], app.contacts[i] = app.contacts[i], app.contacts[i - 1]
                    render_contact_rows()

            def _down(i=idx):
                if i < len(app.contacts) - 1:
                    app.contacts[i + 1], app.contacts[i] = app.contacts[i], app.contacts[i + 1]
                    render_contact_rows()

            def _del(i=idx):
                app.contacts.pop(i)
                render_contact_rows()

            for lbl, cmd in [("↑", _up), ("↓", _down), ("✖", _del)]:
                bg = _P["red"] if lbl == "✖" else "#334155"
                tk.Button(row, text=lbl, font=("Segoe UI", 8, "bold"),
                          bg=bg, fg="#fff", bd=0, width=2, command=cmd).pack(side="left", padx=1)
            settings_contact_rows.append({"frame": row, "name_entry": ec_n, "phone_entry": ec_p})

    render_contact_rows()

    tk.Button(frm, text="+ Add Emergency Contact", font=("Segoe UI", 8, "bold"),
              bg=_P["accent"], fg=_P["bg_root"], bd=0, cursor="hand2", pady=4,
              command=lambda: [app.contacts.append({"name": "New Contact", "phone": "0000000000"}),
                               render_contact_rows()]).pack(fill="x", pady=(4, 16))

    def _save():
        name = e_name.get().strip()
        phone = e_phone.get().strip()
        if not name:
            messagebox.showerror("Settings", "User name is required.")
            return
        if not app.is_valid_phone(phone):
            messagebox.showerror("Settings", "Enter a valid 10-digit user phone number.")
            return
        parsed = []
        for row in settings_contact_rows:
            cn = row["name_entry"].get().strip()
            cp = app.clean_phone(row["phone_entry"].get().strip())
            if cn and cp:
                if not app.is_valid_phone(cp):
                    messagebox.showerror("Settings", f"Contact '{cn}' must have a 10-digit number.")
                    return
                parsed.append({"name": cn, "phone": cp})
        if not parsed:
            messagebox.showerror("Settings", "Add at least one emergency contact.")
            return
        try:
            escalation_timeout = max(20, min(120, int(app.escalation_timeout_var.get())))
        except (TypeError, ValueError):
            escalation_timeout = 60
        app.escalation_timeout_var.set(escalation_timeout)
        app.user_name = name
        app.user_phone = phone
        app.contacts = parsed
        if app.controller:
            app.controller.set_emergency_contacts([c["phone"] for c in parsed])
            app.controller.call_escalation_timeout_sec = escalation_timeout
            if hasattr(app.controller, "escalation_manager"):
                app.controller.escalation_manager.timeout_s = escalation_timeout
            if hasattr(app.controller, "anomaly_engine"):
                app.controller.anomaly_engine.set_speed_threshold(app.speed_threshold_var.get())
        app.save_user_profile()
        dlg.destroy()
        app.send_desktop_popup("Settings Saved", "Offline JSON profile updated.")

    def _reset():
        if messagebox.askyesno("Reset Profile", "Reset current profile and return to the initial setup screen?"):
            app.is_protection_active = False
            app.user_name = ""
            app.user_phone = ""
            app.contacts = []
            app.save_user_profile()
            dlg.destroy()
            app.build_setup_screen()

    tk.Button(frm, text="SAVE SETTINGS TO JSON", font=("Segoe UI", 10, "bold"),
              bg=_P["green"], fg="#ffffff", bd=0, cursor="hand2",
              command=_save).pack(fill="x", ipady=10, pady=(4, 6))
    tk.Button(frm, text="Reset Profile / Re-run Setup", font=("Segoe UI", 8),
              bg=_P["bg_card2"], fg=_P["text_hi"], bd=0, cursor="hand2",
              command=_reset).pack(fill="x", ipady=6, pady=(0, 16))
