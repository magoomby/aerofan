r"""
The tray icon application. Runs as you, unelevated, one per logon session.

    pythonw.exe C:\code\aerofan\aerofan\tray.py

It holds no driver handle and does no EC work at all. Everything it shows comes
from one ``status`` call to the service's pipe every couple of seconds, and
everything it changes goes back the same way. That is what lets it run without
a UAC prompt: the privileged half is already running as SYSTEM, and this is a
window onto it.

WHAT HOVER SHOWS

    aerofan - aggressive
    CPU  72C  fan 90% (20/23)
    GPU  68C  fan 90% (21/23)
    CPU max 2.3 GHz

The percentages are the *applied* duty out of the EC (0xB3/0xB4), not what we
asked for, so in auto they show the firmware's own decision. The bracketed
numbers are the real tachometers on their 0-23 scale. The fourth line appears
only when the CPU is actually capped.

THE MENU

Three blocks, separated: the fan profiles, the CPU frequency cap, then the
housekeeping and Exit. The two halves are unrelated - one is the embedded
controller, the other is a Windows power scheme - and they are next to each
other because they are the two knobs for the same problem, which is a laptop
that is louder and hotter than you want it to be.

EXIT

Exit sets the profile to auto before it closes the window, and waits for the
service to confirm it. Leaving the fans on a curve nobody is watching over is
the one outcome worth blocking a shutdown for a second to avoid - and if the
service cannot be reached, there is nothing holding the fans anyway.
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aerofan.tray import main as _main

    raise SystemExit(_main())

from . import __version__
from . import cpufreq
from . import gpu as gpudev
from . import ipc
from .icons import colour_for
from .state import (
    AUTO, SERVICE_NAME, data_dir, describe_profile, ensure_tray_log_dir,
    profile_names, tray_log_file,
)
from .trayicon import MenuItem, TrayIcon, separator

POLL_SECONDS = 2.0
MUTEX_NAME = "Local\\aerofan-tray-single-instance"
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

MENU_LABELS = {
    "auto": "Auto  (EC's own curve)",
    "quiet": "Quiet",
    "balanced": "Balanced",
    "aggressive": "Aggressive",
    "max": "Max  (100%)",
}


def build_logger(verbose: bool = False) -> logging.Logger:
    """
    A log for a program with nowhere to print.

    pythonw.exe gives the tray no console and no stderr, so before this every
    failure in it was silent - including the one where clicking a menu item
    did nothing at all, which took a service-side log and a process of
    elimination to find. Menu opens and command dispatches are logged at INFO
    because they are rare and they are exactly what you want to see; the
    two-second status poll is not logged at all.
    """
    log = logging.getLogger("aerofan.tray")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    log.propagate = False
    try:
        ensure_tray_log_dir()
        handler = logging.handlers.RotatingFileHandler(
            tray_log_file(), maxBytes=500_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    return log


def _already_running() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if handle and error == ERROR_ALREADY_EXISTS:
        return True
    if not handle and error == ERROR_ACCESS_DENIED:
        # The mutex exists but belongs to a process at a higher integrity
        # level - an elevated tray started by the installer, most likely.
        # Access denied here still means one is already running.
        return True
    # The handle is deliberately leaked: it must live as long as the process,
    # and the process exiting is what releases it.
    return False


def _elevate(command: str, arguments: str) -> None:
    """Run something that needs admin, via the UAC prompt."""
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", command, arguments, None, 0)


def _temperature(value) -> str:
    return f"{value:.0f}C" if isinstance(value, (int, float)) else " --"


def _fan(percent, tach, tach_max) -> str:
    if not isinstance(percent, (int, float)):
        return "fan   --"
    text = f"fan {percent:>3.0f}%"
    if isinstance(tach, (int, float)):
        text += f" ({tach:.0f}/{tach_max or 23:.0f})"
    return text


class TrayApp:
    def __init__(self, log: logging.Logger | None = None) -> None:
        self.log = log or logging.getLogger("aerofan.tray")
        self.snapshot: dict = {}
        self.online = False
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.icon = TrayIcon(self.build_menu, self.on_command,
                             tooltip="aerofan - connecting...",
                             colour=colour_for(None, online=False),
                             log=self.log)

    # -- polling -------------------------------------------------------------

    def poll_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                data = ipc.status(timeout_ms=2000)
                with self.lock:
                    was = self.online
                    self.snapshot = data or {}
                    self.online = True
                if not was:
                    self.log.info("connected to the %s service", SERVICE_NAME)
            except (ipc.ServiceUnavailable, ipc.ProtocolError, OSError) as exc:
                with self.lock:
                    was = self.online
                    self.online = False
                if was:
                    self.log.warning("lost the service: %s", exc)
            except Exception:
                # The poll thread dying takes the whole display with it while
                # the icon sits there looking fine, so nothing gets out.
                self.log.exception("status poll failed")
            self.refresh()
            self.stopping.wait(POLL_SECONDS)

    def refresh(self) -> None:
        with self.lock:
            data = dict(self.snapshot)
            online = self.online
        profile = data.get("profile") or AUTO
        degraded = bool(data.get("degraded")) or bool(data.get("error"))

        if not online:
            tooltip = (f"aerofan - the {SERVICE_NAME} service is not running\n"
                       f"Right-click to start it.")
        else:
            heading = f"aerofan - {profile}"
            if data.get("dry_run"):
                heading += " (dry run)"
            elif degraded:
                heading += " (recovering)"
            lines = [
                heading,
                f"CPU {_temperature(data.get('cpu_c')):>5}  "
                f"{_fan(data.get('fan1_percent'), data.get('fan1_tach'), data.get('tach_max'))}",
                f"GPU {_temperature(data.get('gpu_c')):>5}  "
                f"{_fan(data.get('fan2_percent'), data.get('fan2_tach'), data.get('tach_max'))}",
            ]
            # A fourth line only when there is something to say. An uncapped
            # CPU is the normal state and the menu already shows it ticked;
            # spending a line of a 127-character tooltip on "unlimited" would
            # be spending it on nothing.
            cpu = data.get("cpu") or {}
            if cpu.get("supported"):
                if not cpu.get("agrees", True):
                    lines.append(
                        f"CPU max {cpufreq.describe(cpu.get('ac'))} plugged in,"
                        f" {cpufreq.describe(cpu.get('dc'))} on battery")
                elif cpu.get("limit"):
                    lines.append(f"CPU max {cpufreq.describe(cpu['limit'])}")
            device = data.get("gpu_device") or {}
            if device.get("present") and device.get("enabled") is False:
                # Worth a line: the GPU temperature above still comes from the
                # EC, so without this the tooltip would look completely normal
                # while the card is switched off.
                lines.append("discrete GPU off")
            tooltip = "\n".join(lines)
        self.icon.update(colour_for(profile, online=online, degraded=degraded),
                         tooltip)

    # -- menu ----------------------------------------------------------------

    def build_menu(self) -> list:
        with self.lock:
            data = dict(self.snapshot)
            online = self.online
        active = data.get("profile") or AUTO

        if not online:
            heading = f"aerofan {__version__} - service not running"
        elif data.get("error"):
            heading = f"aerofan {__version__} - {data['error']}"[:64]
        else:
            heading = f"aerofan {__version__} - {describe_profile(active)}"[:64]

        items = [MenuItem(None, heading, enabled=False), separator()]
        for name in profile_names():
            items.append(MenuItem(
                ("profile", name),
                MENU_LABELS.get(name, name.title()),
                checked=(name == active),
                enabled=online))
        if active.startswith("fixed:") and online:
            items.append(MenuItem(None, f"Fixed  ({active.split(':')[1]}%)",
                                  checked=True, enabled=False))

        items.append(separator())
        items.extend(self._cpu_items(data, online))

        items.append(separator())
        items.extend(self._gpu_items(data, online))

        items.append(separator())
        if not online:
            items.append(MenuItem(("start-service",),
                                  f"Start the {SERVICE_NAME} service..."))
        items.append(MenuItem(("log",), "Open the log folder"))
        items.append(separator())
        items.append(MenuItem(("exit",), "Exit  (fans back to auto)"))
        return items

    @staticmethod
    def _cpu_items(data: dict, online: bool) -> list:
        """
        The CPU frequency cap, as its own block between the fans and Exit.

        The tick has to be able to say nothing. The cap lives in the Windows
        power scheme, so it can be changed by powercfg, by another tool, or
        lost entirely when something switches schemes - and then the honest
        answer is a line saying what it actually is rather than a tick against
        the value we last set.
        """
        cpu = data.get("cpu") or {}
        if not online:
            return [MenuItem(None, "CPU max  (service not running)",
                             enabled=False)]
        if not cpu.get("supported"):
            return [MenuItem(None, "CPU max  not available on this machine",
                             enabled=False)]

        current = cpu.get("limit")
        choices = cpu.get("choices") or list(cpufreq.DEFAULT_CHOICES)
        items = [MenuItem(("cpu", mhz), cpufreq.menu_label(mhz),
                          checked=(cpu.get("agrees", True) and current == mhz))
                 for mhz in choices]

        if not cpu.get("agrees", True):
            items.append(MenuItem(
                None,
                f"currently {cpufreq.describe(cpu.get('ac'))} plugged in,"
                f" {cpufreq.describe(cpu.get('dc'))} on battery",
                checked=True, enabled=False))
        elif current not in choices:
            items.append(MenuItem(
                None, f"currently {cpufreq.describe(current)}  (set elsewhere)",
                checked=True, enabled=False))
        return items

    @staticmethod
    def _gpu_items(data: dict, online: bool) -> list:
        """
        One item whose wording is the state rather than the action's target.

        "Disable discrete GPU" when it is on, "Enable discrete GPU" when it is
        off. A checkbox would be ambiguous here - ticked could mean either
        "the GPU is on" or "this action is selected" - and the label cannot be.
        """
        state = data.get("gpu_device") or {}
        if not online:
            return [MenuItem(None, "Discrete GPU  (service not running)",
                             enabled=False)]
        if not state.get("present"):
            return [MenuItem(None, "No discrete GPU on this machine",
                             enabled=False)]

        enabled = bool(state.get("enabled"))
        # Disabling the last display adapter would leave a blank screen and no
        # way back, so the item is greyed rather than merely failing.
        allowed = enabled and not state.get("can_disable")
        item = MenuItem(("gpu", not enabled), gpudev.menu_label(state),
                        enabled=not allowed)
        if allowed:
            return [item, MenuItem(None, "   no other display adapter to fall"
                                         " back to", enabled=False)]
        if not enabled:
            return [item, MenuItem(None, "   off - saves about 5 W, and it"
                                         " stays off across reboots",
                                   enabled=False)]
        return [item]

    def on_command(self, action) -> None:
        self.log.info("command: %r", action)
        verb = action[0]
        if verb == "profile":
            threading.Thread(target=self._set_profile, args=(action[1],),
                             daemon=True).start()
        elif verb == "cpu":
            threading.Thread(target=self._set_cpu_max, args=(action[1],),
                             daemon=True).start()
        elif verb == "gpu":
            threading.Thread(target=self._set_gpu, args=(action[1],),
                             daemon=True).start()
        elif verb == "log":
            os.startfile(str(data_dir()))
        elif verb == "start-service":
            _elevate("sc.exe", f"start {SERVICE_NAME}")
        elif verb == "exit":
            self.quit()

    def _set_profile(self, name: str) -> None:
        self._send(f"profile {name}", lambda: ipc.set_profile(name))

    def _set_cpu_max(self, mhz) -> None:
        self._send(f"cpu max {mhz}", lambda: ipc.set_cpu_max(mhz))

    def _set_gpu(self, enabled: bool) -> None:
        self._send("gpu " + ("on" if enabled else "off"),
                   lambda: ipc.set_gpu(enabled))

    def _send(self, what: str, call) -> None:
        """
        Off the UI thread, always.

        Setting a CPU cap re-applies the whole power scheme and can take a
        second or two. Doing that inline would freeze the menu mid-click,
        which reads as a hung application.
        """
        try:
            data = call()
            with self.lock:
                self.snapshot = data or {}
                self.online = True
            self.log.info("%s: applied", what)
        except (ipc.ServiceUnavailable, ipc.ProtocolError, OSError) as exc:
            with self.lock:
                self.online = False
            self.log.error("%s: failed - %s", what, exc)
        except Exception:
            self.log.exception("%s: failed unexpectedly", what)
        # A set returns the supervisor's snapshot, which carries no CPU
        # section; poll once so the menu is not briefly missing it.
        try:
            data = ipc.status(timeout_ms=2000)
            with self.lock:
                self.snapshot = data or {}
                self.online = True
        except Exception:
            pass
        self.refresh()

    # -- shutdown ------------------------------------------------------------

    def quit(self) -> None:
        """
        Requirement five: leaving means the fans go back to the EC.

        Done here rather than left to the service, because the service is
        meant to keep running - the user is closing the tray, not uninstalling
        fan control. Failing to reach the service is not worth reporting: if
        the pipe is not answering, nothing is holding the fans anyway.
        """
        try:
            ipc.set_profile(AUTO, timeout_ms=5000)
            self.log.info("exit: fans returned to auto")
        except (ipc.ServiceUnavailable, ipc.ProtocolError, OSError) as exc:
            self.log.warning("exit: could not reach the service (%s); nothing"
                             " is holding the fans anyway", exc)
        self.stopping.set()
        self.icon.quit()

    def run(self) -> int:
        threading.Thread(target=self.poll_forever, name="aerofan-tray-poll",
                         daemon=True).start()
        return self.icon.run()


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 1
    verbose = "--verbose" in (argv if argv is not None else sys.argv[1:])
    log = build_logger(verbose)
    if _already_running():
        log.info("another tray icon is already running in this session")
        return 0  # a second icon for the same thing helps nobody
    log.info("aerofan tray %s starting, pid %d", __version__, os.getpid())
    # Give the service a moment at login; it is usually already up, but the
    # first status call landing in the gap looks like "not running" otherwise.
    for _ in range(10):
        if ipc.is_running(timeout_ms=500):
            break
        time.sleep(1.0)
    try:
        return TrayApp(log).run()
    except Exception:
        log.exception("the tray stopped with an exception")
        return 1
    finally:
        log.info("tray exited")


if __name__ == "__main__":
    raise SystemExit(main())
