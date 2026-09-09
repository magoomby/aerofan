r"""
The named pipe between the service and everything else.

    \\.\pipe\aerofan     one JSON object in, one JSON object out, per connect.

WHY A PIPE AND NOT A PORT

The service runs as SYSTEM because opening the PawnIO driver is an
administrator operation. The tray icon runs as you, unelevated, because a tray
icon has to live in your desktop session and asking for a UAC prompt at every
login is not a thing anyone would keep. So the two halves are in different
security contexts and need something between them.

A named pipe is the right shape for that: it is a kernel object with an ACL, so
"who may change the fan profile" is answered by the security descriptor below
rather than by hoping nothing else on the machine finds the port. A loopback
socket has no equivalent - anything running as anybody could connect to it.

WHAT THE PIPE WILL DO

Deliberately, almost nothing: report status, list profiles, select a profile.
There is no "write EC register" verb, and there never should be. Everything on
the far side of this pipe is unprivileged, so the API it can reach is the
narrowest one that still makes the tray useful. The register whitelist in
registers.py guards the same boundary one layer down.

    D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)

SYSTEM and Administrators get everything; authenticated users get read and
write, which is what lets the tray connect. On a single-user laptop that is
the same person either way; the point is that it is not "everyone", and not
anonymous.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import threading
import time

from .state import PIPE_NAME

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255

ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_MORE_DATA = 234
ERROR_PIPE_CONNECTED = 535

BUFFER_BYTES = 16 * 1024

SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;AU)"


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


kernel32.CreateNamedPipeW.restype = wt.HANDLE
kernel32.CreateNamedPipeW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
    ctypes.POINTER(SECURITY_ATTRIBUTES)]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
    wt.HANDLE]
kernel32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
kernel32.FlushFileBuffers.argtypes = [wt.HANDLE]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
kernel32.SetNamedPipeHandleState.argtypes = [
    wt.HANDLE, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
    ctypes.POINTER(wt.DWORD)]
kernel32.ReadFile.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p]
kernel32.WriteFile.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p]

advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wt.ULONG)]


class ServiceUnavailable(RuntimeError):
    """The service is not running, or is not answering its pipe."""


class ProtocolError(RuntimeError):
    """The other end said something that is not our protocol."""


def _security_attributes() -> SECURITY_ATTRIBUTES:
    descriptor = ctypes.c_void_p()
    size = wt.ULONG()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            SDDL, 1, ctypes.byref(descriptor), ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    attributes.lpSecurityDescriptor = descriptor.value
    attributes.bInheritHandle = False
    # The descriptor is LocalAlloc'd by the API and we never free it, so the
    # raw pointer above stays valid for the life of the process. Holding the
    # reference anyway keeps that intentional rather than incidental.
    attributes._descriptor = descriptor
    return attributes


def _read_message(handle) -> bytes:
    chunks = []
    buffer = ctypes.create_string_buffer(BUFFER_BYTES)
    read = wt.DWORD(0)
    while True:
        ok = kernel32.ReadFile(handle, buffer, BUFFER_BYTES,
                               ctypes.byref(read), None)
        error = ctypes.get_last_error()
        if ok:
            chunks.append(buffer.raw[:read.value])
            break
        if error == ERROR_MORE_DATA:
            chunks.append(buffer.raw[:read.value])
            continue
        if error == ERROR_BROKEN_PIPE:
            break
        raise ctypes.WinError(error)
    return b"".join(chunks)


def _write_message(handle, payload: bytes) -> None:
    written = wt.DWORD(0)
    if not kernel32.WriteFile(handle, payload, len(payload),
                              ctypes.byref(written), None):
        raise ctypes.WinError(ctypes.get_last_error())


# -- server ------------------------------------------------------------------


class PipeServer:
    """
    Several accept threads, each with its own instance of the pipe.

    WHY MORE THAN ONE

    The first version had a single accept loop: create an instance, wait for a
    client, hand the connected handle to a worker, go round and create the
    next. That leaves a gap. If the worker finishes and closes its handle
    before the loop has created the replacement, there is briefly no instance
    of the pipe at all - and a named pipe with no instances does not exist, so
    a client arriving in that window gets ERROR_FILE_NOT_FOUND and concludes
    the service is not running.

    That is not theoretical. Every `aerofan.cli` command opens two connections
    back to back - a ping to decide whether the service is up, then the command
    itself - and the second one lands in the gap often enough to fail roughly
    one time in three.

    So instead: a small pool of threads, each looping create -> connect ->
    serve -> close. While any one of them is busy the others are still
    listening, so the name never disappears. Serving inline in the accept
    thread rather than spawning a worker also means a client that connects and
    never sends costs one instance out of the pool instead of wedging
    everything, which was the reason for the worker thread in the first place.
    """

    INSTANCES = 4

    def __init__(self, handlers: dict, log, name: str = PIPE_NAME,
                 instances: int = INSTANCES):
        self.handlers = handlers
        self.log = log
        self.name = name
        self.instances = max(1, instances)
        self.running = False
        self._threads: list[threading.Thread] = []
        self._attributes = None

    def start(self) -> None:
        self.running = True
        self._attributes = _security_attributes()
        for index in range(self.instances):
            thread = threading.Thread(
                target=self._accept_loop, name=f"aerofan-pipe-{index}",
                daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        """
        Stop accepting.

        ConnectNamedPipe blocks with no timeout and no way to cancel from
        another thread, so the shutdown is a connection to our own pipe: it
        satisfies one wait, that thread sees running is False and leaves. One
        connection per instance in the pool, and a couple spare in case a
        connection lands on a thread that is already busy.
        """
        if not self.running:
            return
        self.running = False
        for _ in range(self.instances + 2):
            handle = kernel32.CreateFileW(
                self.name, GENERIC_READ | GENERIC_WRITE, 0, None,
                OPEN_EXISTING, 0, None)
            if handle and handle != INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(wt.HANDLE(handle))
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()
        self.log.info("pipe server stopped")

    def _accept_loop(self) -> None:
        while self.running:
            handle = kernel32.CreateNamedPipeW(
                self.name,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT
                | PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_UNLIMITED_INSTANCES,
                BUFFER_BYTES, BUFFER_BYTES, 0,
                ctypes.byref(self._attributes))
            if not handle or handle == INVALID_HANDLE_VALUE:
                self.log.error("could not create the pipe: %s",
                               ctypes.WinError(ctypes.get_last_error()))
                time.sleep(2.0)
                continue

            handle = wt.HANDLE(handle)
            connected = kernel32.ConnectNamedPipe(handle, None)
            if not connected and \
                    ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                kernel32.CloseHandle(handle)
                continue
            if not self.running:
                kernel32.DisconnectNamedPipe(handle)
                kernel32.CloseHandle(handle)
                break
            self._serve(handle)

    def _serve(self, handle) -> None:
        try:
            raw = _read_message(handle)
            if not raw:
                return
            response = self.dispatch(raw)
            _write_message(handle, response)
            kernel32.FlushFileBuffers(handle)
        except OSError as exc:
            self.log.debug("pipe client went away: %s", exc)
        except Exception:
            self.log.exception("pipe handler blew up")
        finally:
            kernel32.DisconnectNamedPipe(handle)
            kernel32.CloseHandle(handle)

    def dispatch(self, raw: bytes) -> bytes:
        try:
            request = json.loads(raw.decode("utf-8"))
            command = request.get("cmd")
        except (ValueError, AttributeError, UnicodeDecodeError):
            return json.dumps({"ok": False, "error": "malformed request"}
                              ).encode("utf-8")
        handler = self.handlers.get(command)
        if handler is None:
            return json.dumps(
                {"ok": False, "error": f"unknown command {command!r}"}
            ).encode("utf-8")
        try:
            data = handler(request)
            payload = {"ok": True, "data": data}
        except Exception as exc:
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return json.dumps(payload, default=str).encode("utf-8")


# -- client ------------------------------------------------------------------


def request(payload: dict, timeout_ms: int = 3000) -> dict:
    """
    One round trip. Raises ServiceUnavailable if the service is not there.

    Callers treat "not there" as a normal state, not an error: the CLI falls
    back to driving the EC itself, and the tray says so in its tooltip.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    handle = None
    absences = 0
    while True:
        raw_handle = kernel32.CreateFileW(
            PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING,
            0, None)
        if raw_handle and raw_handle != INVALID_HANDLE_VALUE:
            handle = wt.HANDLE(raw_handle)
            break
        error = ctypes.get_last_error()
        if error == ERROR_PIPE_BUSY and time.monotonic() < deadline:
            # Every instance is mid-conversation. Wait for one to free up.
            kernel32.WaitNamedPipeW(PIPE_NAME, 500)
            continue
        if error == ERROR_FILE_NOT_FOUND:
            # Usually this really does mean the service is not running. It can
            # also mean we arrived in the instant between the server's last
            # instance closing and its replacement being created, so try a few
            # more times before believing it - the pool makes that window
            # vanishingly small, and a few hundred milliseconds is a cheap
            # price for never reporting a running service as absent.
            absences += 1
            if absences <= 6 and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise ServiceUnavailable(
                "the AeroFan service is not running (no pipe to connect to)")
        raise ServiceUnavailable(str(ctypes.WinError(error)))

    try:
        mode = wt.DWORD(PIPE_READMODE_MESSAGE)
        kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
        _write_message(handle, json.dumps(payload).encode("utf-8"))
        raw = _read_message(handle)
    except OSError as exc:
        raise ServiceUnavailable(f"the service dropped the connection: {exc}")
    finally:
        kernel32.CloseHandle(handle)

    if not raw:
        raise ServiceUnavailable("the service closed the pipe without replying")
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ProtocolError("the service sent something that is not JSON")
    if not reply.get("ok"):
        raise ProtocolError(reply.get("error", "unspecified failure"))
    return reply.get("data")


# -- the three verbs ---------------------------------------------------------


def status(timeout_ms: int = 3000) -> dict:
    return request({"cmd": "status"}, timeout_ms)


def set_profile(name: str, timeout_ms: int = 5000) -> dict:
    return request({"cmd": "set", "profile": name}, timeout_ms)


def profiles(timeout_ms: int = 3000) -> list:
    return request({"cmd": "profiles"}, timeout_ms)


def set_cpu_max(mhz, timeout_ms: int = 8000) -> dict:
    """
    Cap the CPU. Slower than the others - PowerSetActiveScheme re-applies the
    whole scheme - so it gets a longer timeout than a fan profile change.
    """
    return request({"cmd": "set_cpu_max", "mhz": mhz}, timeout_ms)


def is_running(timeout_ms: int = 1000) -> bool:
    try:
        request({"cmd": "ping"}, timeout_ms)
        return True
    except (ServiceUnavailable, ProtocolError, OSError):
        return False
