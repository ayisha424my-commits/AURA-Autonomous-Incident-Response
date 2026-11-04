import subprocess
import threading
import time
import socket


class NetworkMonitor:

    def __init__(self, controller=None, gui_app=None, poll_interval=5.0, target_host=None, ping_interval=None):
        self.controller = controller
        self.gui_app = gui_app
        self.target_host = target_host
        self.poll_interval = ping_interval if ping_interval is not None else poll_interval
        self.is_running = False
        self._stop_event = threading.Event()
        self.phone_has_signal = False
        self.was_connected = None  # None indicates initial probe pending
        self.alarm_active = False
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_alert_time = 0.0
        self._last_adb_recovery_time = 0.0
        self._adb_recovery_cooldown = 30.0
        self._last_transport_reconnect_time = 0.0
        self._transport_reconnect_cooldown = 5.0
        self._recovery_lock = threading.Lock()
        self._thread = None

    @property
    def is_connected(self):
        return self.phone_has_signal

    @is_connected.setter
    def is_connected(self, val):
        self.phone_has_signal = bool(val)
        self.was_connected = bool(val)

    def _handle_connection_transition(self, now_connected: bool):
        """
        Processes connection state transitions.
        Prevents repeated alarms when already disconnected.
        Returns event dict or None.
        """
        if self.was_connected is None:
            self.was_connected = now_connected
            self.phone_has_signal = now_connected
            return None

        if not now_connected and self.was_connected:
            self.was_connected = False
            self.phone_has_signal = False
            if not self.alarm_active:
                self.alarm_active = True
                self._handle_disconnection()
                return {"event": "CONNECTION_LOST"}
            return None
        elif not now_connected and not self.was_connected:
            # Already disconnected; avoid repeated alarm loop
            return None
        elif now_connected and not self.was_connected:
            self.was_connected = True
            self.phone_has_signal = True
            self.alarm_active = False
            self._handle_reconnection()
            return {"event": "CONNECTION_RESTORED"}
        return None

    def start(self):
        if self.is_running and self._thread and self._thread.is_alive():
            return
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="AURA-ADB-status-poll", daemon=True)
        self._thread.start()
        print("📶 [Network Monitor] Active - ADB health check every 5 seconds...")

    def _poll_loop(self):
        while self.is_running and not self._stop_event.is_set():
            poll_started = time.monotonic()
            raw_status, status_text, color = self._check_phone_network()
            if raw_status:
                self._consecutive_failures = 0
                self._consecutive_successes += 1
                self._last_adb_recovery_time = 0.0
                # GPSBridge listens on localhost:8082. Set up its ADB USB
                # forwarding asynchronously, without blocking this poll/UI.
                ensure_forward = getattr(self.controller, "ensure_gps_bridge_forward", None)
                if callable(ensure_forward):
                    ensure_forward()
            else:
                self._consecutive_failures += 1
                self._consecutive_successes = 0
                if self._consecutive_failures >= 3:
                    reset_forward = getattr(self.controller, "reset_gps_bridge_forward", None)
                    if callable(reset_forward):
                        reset_forward()

                # First try a lightweight transport reconnect. This runs on
                # this daemon monitor thread, never on Tk's event loop.
                now = time.monotonic()
                dispatcher = getattr(self.controller, "usb_dispatcher", None)
                if (dispatcher and hasattr(dispatcher, "reconnect_device")
                        and now - self._last_transport_reconnect_time >= self._transport_reconnect_cooldown):
                    self._last_transport_reconnect_time = now
                    self._start_adb_recovery(restart_server=False)

                # Restart the ADB server only after repeated heartbeat failures.
                if (not raw_status and self._consecutive_failures >= 3
                        and now - self._last_adb_recovery_time >= self._adb_recovery_cooldown):
                    if self._start_adb_recovery(restart_server=True):
                        self._last_adb_recovery_time = now

            # Determine debounced status
            if raw_status and self.was_connected is not True:
                self._handle_connection_transition(True)
            elif (not raw_status and self.was_connected is True
                  and self._consecutive_failures >= 3):
                self._handle_connection_transition(False)

            if self.controller:
                self.controller.phone_has_signal = self.phone_has_signal
                # Keep the shared device cache debounced; one missed ADB probe
                # must not flip voice dispatch or the UI to offline mode.
                self.controller.usb_device_connected = bool(self.phone_has_signal)

            self._update_gui_usb_status(self.phone_has_signal)

            if self.gui_app and self.is_running and not self._stop_event.is_set():
                try:
                    if hasattr(self.gui_app, "root") and self.gui_app.root.winfo_exists():
                        # Only update signal status if not overridden by phone ADB connection
                        p_adb = False
                        if self.controller and hasattr(self.controller, "telephony_manager"):
                            try:
                                dev_conn = getattr(self.controller.telephony_manager, "is_device_connected", False)
                                p_adb = bool(dev_conn)
                            except Exception:
                                pass
                        if not p_adb and hasattr(self.gui_app, "signal_status_str"):
                            curr_text = "📶 Signal: Connected" if self.phone_has_signal else "🚫 Signal: Disconnected"
                            self.gui_app.root.after(
                                0, lambda s=curr_text: self.gui_app.signal_status_str.set(s)
                            )
                        if hasattr(self.gui_app, "update_signal_label"):
                            curr_col = "#10b981" if self.phone_has_signal else "#ef4444"
                            curr_text = "📶 Signal: Connected" if self.phone_has_signal else "🚫 Signal: Disconnected"
                            self.gui_app.root.after(
                                0, lambda s=curr_text, c=curr_col: self.gui_app.update_signal_label(s, c)
                            )
                except Exception:
                    pass

            elapsed = time.monotonic() - poll_started
            # Keep checks at the configured five-second cadence without
            # blocking the UI during transport/server recovery.
            self._stop_event.wait(max(0.1, self.poll_interval - elapsed))

    def _start_adb_recovery(self, restart_server=False):
        """Run one serialized recovery attempt without delaying heartbeat polls."""
        if not self._recovery_lock.acquire(blocking=False):
            return False

        def _recover():
            try:
                if restart_server:
                    self._restart_adb_server()
                else:
                    dispatcher = getattr(self.controller, "usb_dispatcher", None)
                    if dispatcher:
                        if hasattr(dispatcher, "ensure_adb_server"):
                            dispatcher.ensure_adb_server()
                        if hasattr(dispatcher, "reconnect_device"):
                            dispatcher.reconnect_device()
            except Exception as exc:
                print(f"⚠️ [Network Monitor] Background ADB recovery failed: {exc}")
            finally:
                self._recovery_lock.release()

        threading.Thread(
            target=_recover,
            name="AURA-ADB-recovery",
            daemon=True,
        ).start()
        return True

    def _update_gui_usb_status(self, connected):
        gui = self.gui_app
        if not gui or not hasattr(gui, "root"):
            return

        def _apply_status(state=bool(connected)):
            try:
                gui.update_usb_dispatch_status(state)
            except Exception:
                pass

        try:
            gui.root.after(0, _apply_status)
        except Exception:
            pass

    def _handle_disconnection(self):
        """Record connectivity state only; a USB disconnect is not an SOS."""
        self.alarm_active = False
        print("ℹ️ [NetworkMonitor] ADB phone disconnected; offline mode enabled (no emergency alarm).")

    def _handle_reconnection(self):
        """Fires once upon successful reconnection."""
        self.alarm_active = False
        print("✅ [NetworkMonitor] Hotspot / Network reconnected successfully.")

        # Brief positive chime
        def _sound_restored():
            try:
                import winsound
                winsound.Beep(1200, 100)
                winsound.Beep(1600, 150)
            except Exception:
                pass

        threading.Thread(target=_sound_restored, daemon=True).start()

        if self.gui_app and hasattr(self.gui_app, "root") and self.gui_app.root.winfo_exists():
            self.gui_app.root.after(
                0,
                lambda: self.gui_app.send_desktop_popup(
                    "✅ Connectivity Restored",
                    "Phone Hotspot / Network has reconnected successfully."
                )
            )

        if self.controller and hasattr(self.controller, "log_blackbox_event"):
            loc = self.controller.location_engine.get_current_location() if hasattr(self.controller, "location_engine") else {"latitude": 0.0, "longitude": 0.0}
            self.controller.log_blackbox_event(
                loc.get("latitude", 0.0),
                loc.get("longitude", 0.0),
                "HOTSPOT_RECONNECTED",
                details="Network connection to gateway restored"
            )

    def _get_default_gateway(self):
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            res = subprocess.run(["ipconfig"], capture_output=True, text=True, startupinfo=startupinfo)

            for line in res.stdout.splitlines():
                if "Default Gateway" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        val = parts[1].strip()
                        if val and not val.startswith("fe80") and val != "::":
                            return val

            # Dynamic fallback: check local interface routing dynamically
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                sub = ip.rsplit(".", 1)[0]
                return f"{sub}.1"
        except Exception:
            return "127.0.0.1"

    def _check_phone_network(self):
        """USB ADB presence only. Does not alarm on ordinary LAN/hotspot state."""
        dispatcher = getattr(self.controller, "usb_dispatcher", None)
        if dispatcher and hasattr(dispatcher, "check_phone_connection"):
            info = dispatcher.check_phone_connection()
            if info.get("device_connected"):
                return True, "🟢 Online / Dispatch Ready", "#22c55e"
            error = info.get("error")
            if error:
                return False, f"🚫 USB Phone: {error}", "#ef4444"
            if info.get("device_state") == "unauthorized":
                return False, "⚠️ USB debugging authorization required", "#f59e0b"
            return False, "⚪ Offline / Local Siren Mode", "#64748b"

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            adb_path = getattr(getattr(self.controller, "usb_dispatcher", None), "adb_path", "adb")
            res = subprocess.run(
                [adb_path, "devices"],
                capture_output=True, text=True, timeout=2, startupinfo=startupinfo,
            )
            if res.returncode != 0:
                return False, "🚫 USB Phone: ADB command failed", "#ef4444"
            lines = [ln.strip() for ln in (res.stdout or "").splitlines()[1:] if ln.strip()]
            target_serial = "10MECPFE0900011"
            connected = any(
                len(parts := ln.split()) >= 2
                and parts[0] == target_serial
                and parts[1] == "device"
                for ln in lines
            )
            if connected:
                return True, "🟢 Online / Dispatch Ready", "#22c55e"
            return False, "⚪ Offline / Local Siren Mode", "#64748b"
        except FileNotFoundError:
            return False, "🚫 USB Phone: ADB missing", "#ef4444"
        except Exception:
            return False, "🚫 USB Phone: Disconnected", "#ef4444"

    def _restart_adb_server(self):
        """Restart ADB in-process, with a fallback for older controller objects."""
        dispatcher = getattr(self.controller, "usb_dispatcher", None)
        if dispatcher and hasattr(dispatcher, "restart_adb_server"):
            return bool(dispatcher.restart_adb_server())

        adb_path = getattr(dispatcher, "adb_path", "adb")
        adb_env = getattr(dispatcher, "adb_env", None)
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            for action in ("kill-server", "start-server"):
                result = subprocess.run(
                    [adb_path, action], capture_output=True, text=True,
                    timeout=5, startupinfo=startupinfo,
                    env=adb_env,
                )
                if result.returncode != 0:
                    print(f"⚠️ [Network Monitor] adb {action} failed: {(result.stderr or '').strip()}")
                    return False
            print("✅ [Network Monitor] ADB server restarted; retrying device detection.")
            return True
        except Exception as exc:
            print(f"❌ [Network Monitor] ADB server restart failed: {exc}")
            return False

    def stop(self):
        self.is_running = False
        self._stop_event.set()


# Module-level alias for backward compatibility and test suite
NetworkConnectivityMonitor = NetworkMonitor
