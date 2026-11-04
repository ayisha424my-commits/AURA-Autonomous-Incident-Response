"""
main.py
────────────────────────────────────────────────────────────────────────────
AURA — AI-Powered Women Safety System Launcher.
Offline file-based runtime with local JSON storage.
"""

import sys
import tkinter as tk

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from gui_module import ModernSafetyApp
from main_controller import MainSafetyController


def main():
    print("=" * 60)
    print("AURA — AI Women Safety System (offline JSON mode)")
    print("Storage: local JSON files under data/")
    print("=" * 60)

    print("1. Initializing Master Safety Controller & Engines...")
    controller = MainSafetyController()

    print("2. Launching Modern Dashboard Interface...")
    root = tk.Tk()
    root.geometry("1280x760+24+28")
    app = ModernSafetyApp(root, controller=controller)
    controller.attach_gui(app)

    def _simulate_fall(event=None):
        print("[FallDetector] Ctrl+F received — simulating fall event.")
        if hasattr(controller, "fall_detector") and controller.fall_detector:
            controller.fall_detector.simulate_fall("Ctrl+F hotkey (main window)")
        elif hasattr(controller, "on_fall_detected"):
            controller.on_fall_detected()
        return "break"

    root.bind_all("<Control-f>", _simulate_fall)
    root.bind_all("<Control-F>", _simulate_fall)

    def on_closing():
        print("\nShutting down AURA Safety System cleanly...")
        try:
            controller.stop_all()
        except (Exception, KeyboardInterrupt):
            pass
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", on_closing)

    try:
        print("System Ready. Fall sim: Ctrl+F  |  USB siren: ADB disconnect only\n")
        root.mainloop()
    except KeyboardInterrupt:
        on_closing()
    finally:
        try:
            controller.stop_all()
        except Exception:
            pass


if __name__ == "__main__":
    main()
