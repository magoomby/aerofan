"""
ctypes binding to PawnIOLib, the user-mode half of the PawnIO driver.

Why PawnIO rather than WinRing0: this machine runs with Memory Integrity (HVCI)
enabled and Microsoft's vulnerable-driver blocklist on. WinRing0 has been on that
blocklist since March 2025 and will not load. PawnIO is signed and current, and -
more usefully - it does not expose a raw "write any port" primitive. It runs a
sandboxed bytecode module, and the module we load (LpcACPIEC) rejects every port
except 0x62 and 0x66.

The DLL is a small C API. Everything returns an HRESULT:

    HRESULT pawnio_version(UINT *version);
    HRESULT pawnio_open(HANDLE *handle);
    HRESULT pawnio_load(HANDLE handle, const UCHAR *blob, SIZE_T size);
    HRESULT pawnio_execute(HANDLE handle, const char *name,
                           const ULONG64 *in,  SIZE_T in_size,
                           ULONG64 *out, SIZE_T out_size,
                           SIZE_T *return_size);
    HRESULT pawnio_close(HANDLE handle);

Opening the driver needs administrator rights. That is why the daemon runs as
SYSTEM and everything else talks to it over a pipe.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
from pathlib import Path

MODULE_NAME = "LpcACPIEC.bin"

# Where PawnIO's installer and its module pack tend to put things.
_SEARCH_DIRS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PawnIO",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PawnIO" / "Modules",
    Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "PawnIO",
    Path(__file__).resolve().parent.parent / "modules",
)


class PawnIOError(RuntimeError):
    """Anything the driver or the loader refused to do."""


class PawnIOUnavailable(PawnIOError):
    """PawnIO is not installed, or we are not elevated enough to open it."""


def _hresult_ok(code: int) -> bool:
    # HRESULT is signed; success is >= 0.
    return ctypes.c_long(code).value >= 0


def _describe(code: int) -> str:
    value = ctypes.c_ulong(code).value
    known = {
        0x80070005: "access denied - the process is not running elevated",
        0x80070002: "file not found - is PawnIO installed?",
        0x8007007E: "module not found",
        0xC0000022: "STATUS_ACCESS_DENIED from the module",
    }
    return known.get(value, f"HRESULT 0x{value:08X}")


def find_module_blob(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate LpcACPIEC.bin, the compiled Pawn module we execute in ring 0."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise PawnIOUnavailable(f"module blob not found at {path}")
        return path
    for directory in _SEARCH_DIRS:
        candidate = directory / MODULE_NAME
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(d) for d in _SEARCH_DIRS)
    raise PawnIOUnavailable(
        f"could not find {MODULE_NAME}. Looked in:\n  {searched}\n"
        "Download it from https://github.com/namazso/PawnIO.Modules/releases "
        "and drop it in the aerofan 'modules' folder."
    )


class PawnIO:
    """
    One open handle to the driver with one module loaded.

    Use as a context manager. The handle is process-wide state in the driver, so
    exactly one of these should exist per process - the daemon owns it.
    """

    def __init__(self, module_path: str | os.PathLike[str] | None = None):
        if sys.platform != "win32":
            raise PawnIOUnavailable("Windows only.")
        self._lib = self._load_library()
        self._bind()
        self._handle = wt.HANDLE()
        self._module_path = find_module_blob(module_path)
        self._open = False

    # -- setup ---------------------------------------------------------------

    @staticmethod
    def _load_library() -> ctypes.WinDLL:
        for name in ("PawnIOLib.dll", "PawnIOLib"):
            try:
                return ctypes.WinDLL(name)
            except OSError:
                continue
        # Fall back to the install directory, in case it is not on PATH.
        for directory in _SEARCH_DIRS:
            candidate = directory / "PawnIOLib.dll"
            if candidate.is_file():
                try:
                    return ctypes.WinDLL(str(candidate))
                except OSError:
                    pass
        raise PawnIOUnavailable(
            "PawnIOLib.dll not found. Install PawnIO from https://pawnio.eu/ "
            "and reopen the shell so PATH refreshes."
        )

    def _bind(self) -> None:
        lib = self._lib
        lib.pawnio_version.argtypes = [ctypes.POINTER(wt.UINT)]
        lib.pawnio_version.restype = ctypes.c_long

        lib.pawnio_open.argtypes = [ctypes.POINTER(wt.HANDLE)]
        lib.pawnio_open.restype = ctypes.c_long

        lib.pawnio_load.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_ubyte),
                                    ctypes.c_size_t]
        lib.pawnio_load.restype = ctypes.c_long

        lib.pawnio_execute.argtypes = [
            wt.HANDLE,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.pawnio_execute.restype = ctypes.c_long

        lib.pawnio_close.argtypes = [wt.HANDLE]
        lib.pawnio_close.restype = ctypes.c_long

    # -- lifecycle -----------------------------------------------------------

    def version(self) -> tuple[int, int, int]:
        raw = wt.UINT()
        code = self._lib.pawnio_version(ctypes.byref(raw))
        if not _hresult_ok(code):
            raise PawnIOError(f"pawnio_version failed: {_describe(code)}")
        value = raw.value
        return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF

    def open(self) -> "PawnIO":
        code = self._lib.pawnio_open(ctypes.byref(self._handle))
        if not _hresult_ok(code):
            raise PawnIOUnavailable(f"pawnio_open failed: {_describe(code)}")
        self._open = True
        blob = self._module_path.read_bytes()
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        code = self._lib.pawnio_load(self._handle, buffer, len(blob))
        if not _hresult_ok(code):
            self.close()
            raise PawnIOError(
                f"pawnio_load of {self._module_path.name} failed: {_describe(code)}"
            )
        return self

    def close(self) -> None:
        if self._open:
            self._lib.pawnio_close(self._handle)
            self._open = False

    def __enter__(self) -> "PawnIO":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- calls ---------------------------------------------------------------

    def execute(self, name: str, inputs: list[int], out_count: int) -> list[int]:
        """Call an exported ioctl_ function in the loaded module."""
        if not self._open:
            raise PawnIOError("driver handle is not open")
        in_array = (ctypes.c_ulonglong * max(len(inputs), 1))(*inputs)
        out_array = (ctypes.c_ulonglong * max(out_count, 1))()
        returned = ctypes.c_size_t(0)
        code = self._lib.pawnio_execute(
            self._handle,
            name.encode("ascii"),
            in_array, len(inputs),
            out_array, out_count,
            ctypes.byref(returned),
        )
        if not _hresult_ok(code):
            raise PawnIOError(f"{name} failed: {_describe(code)}")
        return list(out_array[: returned.value])

    # -- the two things LpcACPIEC gives us -----------------------------------

    def port_read(self, port: int) -> int:
        """Read one byte from an EC port. The module allows only 0x62 / 0x66."""
        return self.execute("ioctl_pio_read", [port], 1)[0] & 0xFF

    def port_write(self, port: int, value: int) -> None:
        """Write one byte to an EC port. The module allows only 0x62 / 0x66."""
        self.execute("ioctl_pio_write", [port, value & 0xFF], 0)


def is_elevated() -> bool:
    """True if this process can realistically open the driver."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
