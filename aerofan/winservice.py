r"""
The Windows service. Runs as SYSTEM from boot, owns the driver, answers the pipe.

    tools\install.ps1        creates and starts it (elevated, once)
    tools\uninstall.ps1      removes it
    sc query AeroFan         is it running
    python -m aerofan.winservice run     the same engine in a console, for
                                         debugging, without installing anything

WHY A SERVICE AND NOT A SCHEDULED TASK

A scheduled task at logon would be simpler, and would also be wrong: the fans
would run on the EC's curve from power-on until somebody logged in, which on a
machine that suspends and resumes all day is most of its life. A service starts
before the logon screen, reads the profile you last chose out of state.json,
and applies it. That is requirement one, and it is the only structure that
satisfies it.

It also means the driver handle lives in exactly one process. Two processes
talking to the EC mailbox at once is the failure this whole project is careful
about - and on this machine there is no Access_EC mutant to arbitrate, so
"careful" is all we have.

NO pywin32

The service control protocol is three calls - StartServiceCtrlDispatcherW,
RegisterServiceCtrlHandlerExW, SetServiceStatus - and ctypes reaches them as
well as pywin32 does. Keeping the dependency list empty is worth more here than
the convenience: this has to install on a friend's laptop with nothing on it
but Python and PawnIO.

STARTING BEFORE THE DRIVER IS READY

The service does not fail to start if PawnIO is not up yet. It reports RUNNING,
answers the pipe, and retries the driver in the background. A service that
refuses to start at boot because a dependency was three seconds late is a
service you find out about when the laptop is already hot.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import logging.handlers
import os
import sys
import threading
import time

if __name__ == "__main__" and __package__ in (None, ""):
    # Services are launched by full path with System32 as the working
    # directory, so the package is not importable yet. Put the repo root on
    # sys.path and come back in through the package, which is what makes the
    # relative imports below work.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aerofan.winservice import main as _main

    raise SystemExit(_main())

from . import __version__
from . import cpufreq
from .control import Controller
from .cpufreq import CpuLimit
from .ec import EmbeddedController
from .ipc import PipeServer
from .pawnio import PawnIO, PawnIOUnavailable
from .state import (
    AUTO, SERVICE_NAME, config_file, ensure_data_dir, log_file,
    normalise_profile, profile_catalogue, read_state, write_state,
)
from .supervisor import DEFAULT_CONFIG, Supervisor

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

SERVICE_WIN32_OWN_PROCESS = 0x00000010

SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004

SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
SERVICE_ACCEPT_POWEREVENT = 0x00000040

SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_INTERROGATE = 0x00000004
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_CONTROL_POWEREVENT = 0x0000000D

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

NO_ERROR = 0
ERROR_CALL_NOT_IMPLEMENTED = 120


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wt.DWORD),
                ("dwCurrentState", wt.DWORD),
                ("dwControlsAccepted", wt.DWORD),
                ("dwWin32ExitCode", wt.DWORD),
                ("dwServiceSpecificExitCode", wt.DWORD),
                ("dwCheckPoint", wt.DWORD),
                ("dwWaitHint", wt.DWORD)]


SERVICE_MAIN_FUNCTION = ctypes.WINFUNCTYPE(
    None, wt.DWORD, ctypes.POINTER(wt.LPWSTR))
HANDLER_EX_FUNCTION = ctypes.WINFUNCTYPE(
    wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p)


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [("lpServiceName", wt.LPWSTR),
                ("lpServiceProc", SERVICE_MAIN_FUNCTION)]


advapi32.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
advapi32.RegisterServiceCtrlHandlerExW.argtypes = [
    wt.LPCWSTR, HANDLER_EX_FUNCTION, ctypes.c_void_p]
advapi32.SetServiceStatus.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
advapi32.StartServiceCtrlDispatcherW.argtypes = [
    ctypes.POINTER(SERVICE_TABLE_ENTRY)]


# -- logging -----------------------------------------------------------------


class _StreamToLog:
    """
    Somewhere for stray print() to go.

    pythonw.exe has no console, so sys.stdout is None and any print() anywhere
    in the codebase raises AttributeError. There are prints in sensors.py and
    control.py that are exactly right for a terminal, and this is what makes
    them harmless in a service instead of fatal.
    """

    def __init__(self, log: logging.Logger, level: int = logging.INFO):
        self._log = log
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._log.log(self._level, "%s", line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._log.log(self._level, "%s", self._buffer.strip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False


def build_logger(console: bool = False) -> logging.Logger:
    ensure_data_dir()
    log = logging.getLogger("aerofan")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)
    except OSError:
        pass  # a service with no log is still a service
    if console and sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        log.addHandler(stream)
    log.propagate = False
    return log


# Settings the service has and the control loop does not. The CPU cap is a
# Windows power-scheme setting with nothing to do with the embedded
# controller, so it stays out of the supervisor's config entirely.
SERVICE_DEFAULTS = {
    "cpu_max_choices": list(cpufreq.DEFAULT_CHOICES),
    "cpu_max_power_sources": list(cpufreq.DEFAULT_POWER_SOURCES),
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG, **SERVICE_DEFAULTS)
    path = config_file()
    try:
        # utf-8-sig so a config saved from Notepad or PowerShell - both of
        # which write a BOM - is read rather than rejected. This file is
        # meant to be hand-edited, so it has to survive the editors people
        # actually have.
        if path.is_file():
            config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        logging.getLogger("aerofan").warning(
            "ignoring %s: %s", path, exc)
    return config


# -- the engine, independent of how it was started ---------------------------


class Engine:
    """
    Everything the service does, minus the SCM plumbing.

    Separated so ``winservice run`` exercises the identical code path in a
    console. A service you can only test by installing it is a service you
    debug by reading logs and guessing.
    """

    def __init__(self, log: logging.Logger, dry_run: bool = False):
        self.log = log
        self.dry_run = dry_run
        self.config = load_config()
        self.remembered = read_state()["profile"]
        self.config["profile"] = self.remembered
        self.supervisor: Supervisor | None = None
        self.io = None
        self.driver_error: str | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.cpu = CpuLimit(
            choices=self.config.get("cpu_max_choices",
                                    cpufreq.DEFAULT_CHOICES),
            sources=self.config.get("cpu_max_power_sources",
                                    cpufreq.DEFAULT_POWER_SOURCES),
            log=log)
        self.pipe = PipeServer({
            "ping": self._cmd_ping,
            "status": self._cmd_status,
            "profiles": self._cmd_profiles,
            "set": self._cmd_set,
            "set_cpu_max": self._cmd_set_cpu_max,
        }, log)

    # -- pipe verbs ----------------------------------------------------------

    def _cmd_ping(self, _request: dict) -> dict:
        return {"service": SERVICE_NAME, "version": __version__,
                "pid": os.getpid()}

    def _cmd_status(self, _request: dict) -> dict:
        if self.supervisor is not None:
            data = self.supervisor.snapshot()
        else:
            data = {"profile": self.remembered, "effective": AUTO,
                    "error": self.driver_error or "starting up",
                    "custom": False, "degraded": True,
                    "updated": time.time()}
        # The CPU cap has no dependency on the EC or the driver, so it is
        # reported even while the fan half is unavailable.
        try:
            data["cpu"] = self.cpu.snapshot()
        except Exception as exc:
            self.log.debug("CPU limit unavailable: %s", exc)
            data["cpu"] = {"limit": None, "supported": False,
                           "error": str(exc), "choices": []}
        return data

    def _cmd_set_cpu_max(self, request: dict) -> dict:
        self.cpu.apply(request.get("mhz"))
        return self._cmd_status(request)

    def _cmd_profiles(self, _request: dict) -> list:
        return profile_catalogue()

    def _cmd_set(self, request: dict) -> dict:
        name = normalise_profile(request.get("profile", ""))
        if self.supervisor is not None:
            self.supervisor.request_profile(name)
            return self.supervisor.snapshot()
        # The driver is not up yet. Remember it anyway - the supervisor reads
        # state.json when it finally starts, so the choice is not lost.
        self.remembered = name
        self.config["profile"] = name
        write_state(name)
        return self._cmd_status(request)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.pipe.start()
        self._worker = threading.Thread(
            target=self._run_supervisor, name="aerofan-supervisor", daemon=True)
        self._worker.start()

    def _open_driver(self):
        """
        Open PawnIO, retrying while the machine finishes booting.

        At boot we can easily be running before the PawnIO driver has been
        started, even with a service dependency declared, because the driver is
        demand-started by its own library. Backing off and retrying is the
        difference between "fans managed from boot" and "fans managed from the
        next time you notice and restart the service".
        """
        delay = 2.0
        while not self._stop.is_set():
            try:
                io = PawnIO().open()
                self.driver_error = None
                self.log.info("PawnIO opened")
                return io
            except (PawnIOUnavailable, OSError) as exc:
                self.driver_error = str(exc)
                self.log.warning("PawnIO not available yet (%s) - retrying in"
                                 " %.0fs", exc, delay)
                if self._stop.wait(delay):
                    return None
                delay = min(delay * 2, 60.0)
        return None

    def _run_supervisor(self) -> None:
        self.io = self._open_driver()
        if self.io is None:
            return
        try:
            controller = Controller(EmbeddedController(self.io),
                                    dry_run=self.dry_run)
            self.supervisor = Supervisor(
                controller, self.config, self.log, dry_run=self.dry_run,
                on_profile_change=write_state)
            self.supervisor.run()
        except Exception:
            self.log.exception("the supervisor stopped with an exception")
        finally:
            try:
                if self.io is not None:
                    self.io.close()
            except Exception:
                pass
            self.log.info("supervisor thread finished")

    def stop(self) -> None:
        self._stop.set()
        if self.supervisor is not None:
            self.supervisor.stop()
        if self._worker is not None:
            self._worker.join(timeout=20.0)
        self.pipe.stop()

    # -- power management ----------------------------------------------------

    def on_power_event(self, event: int) -> None:
        if self.supervisor is None:
            return
        if event == PBT_APMSUSPEND:
            self.supervisor.note_suspend()
        elif event in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            self.supervisor.note_resume()


# -- SCM plumbing ------------------------------------------------------------

_status_handle = None
_status = SERVICE_STATUS()
_stop_event = threading.Event()
_engine: Engine | None = None
_log: logging.Logger | None = None
_checkpoint = 0
# WINFUNCTYPE wrappers are freed with the last Python reference to them. The
# SCM keeps calling them long after ServiceMain returns, so they are module
# level on purpose - a local would be a use-after-free with a very confusing
# crash dump.
_handler_ref = None
_service_main_ref = None


def _report(state: int, wait_hint: int = 0, exit_code: int = 0) -> None:
    global _checkpoint
    if _status_handle is None:
        return
    _status.dwServiceType = SERVICE_WIN32_OWN_PROCESS
    _status.dwCurrentState = state
    _status.dwControlsAccepted = (
        SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        | SERVICE_ACCEPT_POWEREVENT) if state == SERVICE_RUNNING else 0
    _status.dwWin32ExitCode = exit_code
    _status.dwServiceSpecificExitCode = 0
    if state in (SERVICE_START_PENDING, SERVICE_STOP_PENDING):
        _checkpoint += 1
        _status.dwCheckPoint = _checkpoint
    else:
        _checkpoint = 0
        _status.dwCheckPoint = 0
    _status.dwWaitHint = wait_hint
    advapi32.SetServiceStatus(_status_handle, ctypes.byref(_status))


def _handler(control: int, event_type: int, _event_data, _context) -> int:
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        if _log:
            _log.info("stop requested (control %d)", control)
        _report(SERVICE_STOP_PENDING, wait_hint=30000)
        _stop_event.set()
        return NO_ERROR
    if control == SERVICE_CONTROL_INTERROGATE:
        _report(_status.dwCurrentState)
        return NO_ERROR
    if control == SERVICE_CONTROL_POWEREVENT:
        if _engine is not None:
            try:
                _engine.on_power_event(event_type)
            except Exception:
                if _log:
                    _log.exception("power event handler failed")
        return NO_ERROR
    return ERROR_CALL_NOT_IMPLEMENTED


def _service_main(_argc, _argv) -> None:
    global _status_handle, _engine, _log
    _log = build_logger()
    sys.stdout = _StreamToLog(_log)
    sys.stderr = _StreamToLog(_log, logging.WARNING)

    _status_handle = advapi32.RegisterServiceCtrlHandlerExW(
        SERVICE_NAME, _handler_ref, None)
    if not _status_handle:
        _log.error("RegisterServiceCtrlHandlerExW failed: %s",
                   ctypes.WinError(ctypes.get_last_error()))
        return

    _report(SERVICE_START_PENDING, wait_hint=20000)
    try:
        _engine = Engine(_log)
        _engine.start()
    except Exception:
        _log.exception("failed to start")
        _report(SERVICE_STOPPED, exit_code=1)
        return

    _log.info("%s %s running as pid %d; remembered profile %s",
              SERVICE_NAME, __version__, os.getpid(), _engine.remembered)
    _report(SERVICE_RUNNING)

    _stop_event.wait()

    _report(SERVICE_STOP_PENDING, wait_hint=30000)
    try:
        _engine.stop()
    except Exception:
        _log.exception("error while stopping")
    _log.info("stopped")
    _report(SERVICE_STOPPED)


def run_as_service() -> int:
    global _handler_ref, _service_main_ref
    _handler_ref = HANDLER_EX_FUNCTION(_handler)
    _service_main_ref = SERVICE_MAIN_FUNCTION(_service_main)
    # Two entries: ours, then the all-NULL terminator the SCM looks for. The
    # array arrives zeroed, so the terminator needs no filling in.
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = _service_main_ref
    if not advapi32.StartServiceCtrlDispatcherW(table):
        error = ctypes.get_last_error()
        # 1063: not started by the SCM. Almost always someone running this by
        # hand, so say the useful thing rather than the literal one. There may
        # be no console to say it on (pythonw), hence the guard.
        message = (
            "This is the service entry point. To run the same engine in a"
            " console:\n\n    python -m aerofan.winservice run\n"
            if error == 1063 else
            f"StartServiceCtrlDispatcherW failed: {ctypes.WinError(error)}")
        if sys.stdout is not None:
            print(message)
        return 1
    return 0


def run_in_console(dry_run: bool = False) -> int:
    """The service engine, in a terminal, with Ctrl-C to stop."""
    log = build_logger(console=True)
    engine = Engine(log, dry_run=dry_run)
    engine.start()
    log.info("console mode - Ctrl-C to stop. Pipe is live at %s",
             r"\\.\pipe\aerofan")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        engine.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform != "win32":
        print("Windows only.")
        return 1
    if not argv or argv[0] == "--service":
        return run_as_service()
    if argv[0] in ("run", "debug"):
        return run_in_console(dry_run="--dry-run" in argv)
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
