r"""
The CPU maximum frequency cap - the other half of "make this laptop quieter".

Equivalent to what you would type by hand:

    powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 2300
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX 2300
    powercfg /setactive SCHEME_CURRENT

except through powrprof.dll directly, which is the API powercfg itself is a
front end for. No process to spawn, real error codes back, and the same ctypes
habit as the rest of the project. 0 means no cap.

THE THIRD LINE IS NOT OPTIONAL

`setacvalueindex` writes the value into the power scheme and does nothing else.
Until the scheme is made active again the CPU carries on exactly as before.
That is the usual reason a hand-typed powercfg cap appears to do nothing, so
`apply_limit` always finishes with PowerSetActiveScheme.

AC AND DC TOGETHER

A menu item that says "CPU max 2.3 GHz" should mean 2.3 GHz, so both the
plugged-in and the on-battery index are written. Setting only AC - which is
what you get from `setacvalueindex` alone - means the cap silently disappears
when you unplug, while the tick in the menu goes on claiming it is there.
Configurable via `cpu_max_power_sources` if you want the old behaviour.

EFFICIENCY CLASS 1

On a hybrid CPU the E-cores are a second setting, PROCFREQMAX1, at the same
GUID with the last byte incremented. This machine is an i7-10875H - Comet Lake,
eight identical cores - and does not have it: it is not in SUB_PROCESSOR at
all, which is why `powercfg ... PROCFREQMAX1 2500` has never done anything
here. It is still written where the system has it, and its absence is not an
error.

THE CAP IS WINDOWS' TO KEEP

Unlike the fan profile, nothing here is remembered or re-applied by aerofan.
The value lives in the power scheme and survives reboots on its own. The
consequence worth knowing: it is attached to *a* scheme, so if something
switches schemes - Gigabyte's Smart Manager does, and the active one here is
called "Smartmanager High performance" - the cap does not follow. Everything
in this file reads the live value rather than a remembered one, so when that
happens the menu tells you the truth instead of a comfortable fiction.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_SUCCESS = 0

# Universal Windows power-setting GUIDs, not machine specific.
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
PROCFREQMAX = "75b0ae3f-bce0-45a7-8c89-c9611c25e100"
PROCFREQMAX1 = "75b0ae3f-bce0-45a7-8c89-c9611c25e101"

UNLIMITED = 0

# What the tray menu offers. Override with "cpu_max_choices" in aerofan.json.
# 1400 is well under the 2300 MHz base clock: a deliberate "I am taking notes"
# setting rather than a performance one.
DEFAULT_CHOICES = (1400, 2300, 3000, 4000, UNLIMITED)

DEFAULT_POWER_SOURCES = ("ac", "dc")

# A cap below this is not a quieter laptop, it is a brick. Windows clamps to
# the processor's own minimum anyway, but refusing here means a typo cannot
# get that far, and the pipe is reachable by any signed-in user.
MINIMUM_MHZ = 400
# Comfortably above any real part, and low enough that a fat-fingered "42"
# (which the GHz heuristic below turns into 42000) is caught rather than
# written into the power scheme.
MAXIMUM_MHZ = 10_000

# How long a reading stays fresh. The tray asks for status every couple of
# seconds and the answer only changes when somebody changes it, so re-reading
# on every poll would be three registry hits a second for no new information.
CACHE_SECONDS = 30.0


class CpuFreqError(RuntimeError):
    """powrprof refused something."""


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    raw = bytes.fromhex(text.replace("-", ""))
    value = GUID()
    value.Data1 = int.from_bytes(raw[0:4], "big")
    value.Data2 = int.from_bytes(raw[4:6], "big")
    value.Data3 = int.from_bytes(raw[6:8], "big")
    for index in range(8):
        value.Data4[index] = raw[8 + index]
    return value


_SUBGROUP = _guid(SUB_PROCESSOR)
_MAX = _guid(PROCFREQMAX)
_MAX1 = _guid(PROCFREQMAX1)

powrprof.PowerGetActiveScheme.argtypes = [
    wt.HKEY, ctypes.POINTER(ctypes.POINTER(GUID))]
powrprof.PowerGetActiveScheme.restype = wt.DWORD
powrprof.PowerSetActiveScheme.argtypes = [wt.HKEY, ctypes.POINTER(GUID)]
powrprof.PowerSetActiveScheme.restype = wt.DWORD
for _name in ("PowerReadACValueIndex", "PowerReadDCValueIndex"):
    getattr(powrprof, _name).argtypes = [
        wt.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID),
        ctypes.POINTER(GUID), ctypes.POINTER(wt.DWORD)]
    getattr(powrprof, _name).restype = wt.DWORD
for _name in ("PowerWriteACValueIndex", "PowerWriteDCValueIndex"):
    getattr(powrprof, _name).argtypes = [
        wt.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID),
        ctypes.POINTER(GUID), wt.DWORD]
    getattr(powrprof, _name).restype = wt.DWORD
kernel32.LocalFree.argtypes = [wt.HGLOBAL]


# -- presentation ------------------------------------------------------------


def describe(mhz) -> str:
    """2300 -> '2.3 GHz', 3000 -> '3 GHz', 0 -> 'unlimited'."""
    if mhz in (None, "", UNLIMITED):
        return "unlimited"
    ghz = mhz / 1000.0
    return f"{ghz:.0f} GHz" if float(ghz).is_integer() else f"{ghz:.1f} GHz"


def menu_label(mhz) -> str:
    return f"CPU max  {describe(mhz)}"


def validate(mhz) -> int:
    """
    Accept what a caller could reasonably send, and nothing else.

    This is reachable over the pipe by any signed-in user, so the range check
    is a boundary, not a convenience.
    """
    if isinstance(mhz, str):
        text = mhz.strip().lower().replace(" ", "")
        if text in ("0", "none", "off", "unlimited", "uncapped", "full"):
            return UNLIMITED
        try:
            if text.endswith("ghz"):
                value = float(text[:-3]) * 1000
            elif text.endswith("mhz"):
                value = float(text[:-3])
            else:
                value = float(text)
        except ValueError:
            raise ValueError(
                f"not a frequency: {mhz!r}. Try 2300, 2.3ghz, or unlimited.")
    elif isinstance(mhz, bool):
        raise ValueError(f"not a frequency: {mhz!r}")
    elif isinstance(mhz, (int, float)):
        value = float(mhz)
    else:
        raise ValueError(f"not a frequency: {mhz!r}")

    # Nobody caps a CPU at three megahertz, so a small number is GHz. This is
    # what lets "aerofan cpu 2.3" and "aerofan cpu 2300" both mean the same.
    if 0 < value < 100:
        value *= 1000
    mhz = int(round(value))
    if mhz == UNLIMITED:
        return UNLIMITED
    if not MINIMUM_MHZ <= mhz <= MAXIMUM_MHZ:
        raise ValueError(
            f"{mhz} MHz is outside the allowed range "
            f"({MINIMUM_MHZ}-{MAXIMUM_MHZ} MHz, or 0 for unlimited)")
    return mhz


# -- the scheme --------------------------------------------------------------


class _ActiveScheme:
    """PowerGetActiveScheme hands back memory we have to give back."""

    def __enter__(self) -> ctypes.POINTER(GUID):
        self._pointer = ctypes.POINTER(GUID)()
        status = powrprof.PowerGetActiveScheme(None, ctypes.byref(self._pointer))
        if status != ERROR_SUCCESS:
            raise CpuFreqError(
                f"PowerGetActiveScheme failed: {ctypes.WinError(status)}")
        return self._pointer

    def __exit__(self, *exc) -> None:
        if self._pointer:
            kernel32.LocalFree(ctypes.cast(self._pointer, wt.HGLOBAL))
            self._pointer = None


def _read(scheme, setting, dc: bool) -> int | None:
    function = (powrprof.PowerReadDCValueIndex if dc
                else powrprof.PowerReadACValueIndex)
    value = wt.DWORD(0)
    status = function(None, scheme, ctypes.byref(_SUBGROUP),
                      ctypes.byref(setting), ctypes.byref(value))
    if status != ERROR_SUCCESS:
        return None
    return int(value.value)


def _write(scheme, setting, dc: bool, mhz: int) -> bool:
    function = (powrprof.PowerWriteDCValueIndex if dc
                else powrprof.PowerWriteACValueIndex)
    status = function(None, scheme, ctypes.byref(_SUBGROUP),
                      ctypes.byref(setting), wt.DWORD(mhz))
    return status == ERROR_SUCCESS


def read_limit() -> dict:
    """
    What the cap is right now, straight out of the active scheme.

    ``limit`` is the single number worth showing: the AC and DC indexes when
    they agree, and None when they do not, because there is no honest single
    answer in that case and a tick mark should not invent one.
    """
    # There is no "is this CPU hybrid" field here, deliberately. The obvious
    # test - read PROCFREQMAX1 and see whether it fails - does not work: it
    # returns success on this i7-10875H, which has no E-cores and does not
    # list the setting in SUB_PROCESSOR at all. Reporting a guess as a fact is
    # worse than not reporting it, and nothing needs the answer, because the
    # class-1 write is best effort either way.
    with _ActiveScheme() as scheme:
        ac = _read(scheme, _MAX, dc=False)
        dc = _read(scheme, _MAX, dc=True)
    limit = ac if (ac is not None and ac == dc) else None
    return {
        "ac": ac,
        "dc": dc,
        "limit": limit,
        "agrees": ac == dc,
        "supported": ac is not None,
    }


def apply_limit(mhz, sources=DEFAULT_POWER_SOURCES) -> dict:
    """
    Set the cap and make it take effect. Needs administrator or SYSTEM.

    Best effort per setting, but not per power source: if the AC write lands
    and the DC one does not, that is reported rather than swallowed, because
    the difference is exactly the "cap vanishes when you unplug" surprise this
    is trying to avoid.
    """
    mhz = validate(mhz)
    sources = tuple(s.lower() for s in sources) or DEFAULT_POWER_SOURCES
    written, failed = [], []
    with _ActiveScheme() as scheme:
        for source in ("ac", "dc"):
            if source not in sources:
                continue
            dc = source == "dc"
            if _write(scheme, _MAX, dc, mhz):
                written.append(source)
            else:
                failed.append(source)
            # Class 1 is the E-cores. Absent on a non-hybrid CPU, and its
            # absence is not a failure of anything.
            _write(scheme, _MAX1, dc, mhz)

        # Without this the scheme holds the new value and the CPU ignores it.
        status = powrprof.PowerSetActiveScheme(None, scheme)
        if status != ERROR_SUCCESS:
            raise CpuFreqError(
                f"PowerSetActiveScheme failed: {ctypes.WinError(status)}")

    if failed:
        raise CpuFreqError(
            f"could not write the {'/'.join(failed)} limit "
            f"(this needs administrator; the service runs as SYSTEM)")
    result = read_limit()
    result["written"] = written
    return result


class CpuLimit:
    """
    A cached reader, so the tray polling status does not re-read the registry
    three times a second for an answer that changes twice a week.
    """

    def __init__(self, choices=DEFAULT_CHOICES,
                 sources=DEFAULT_POWER_SOURCES, log=None):
        self.choices = [validate(choice) for choice in choices]
        self.sources = tuple(sources)
        self.log = log
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._read_at = 0.0

    def snapshot(self, force: bool = False) -> dict:
        with self._lock:
            fresh = time.monotonic() - self._read_at < CACHE_SECONDS
            if self._value is not None and fresh and not force:
                return dict(self._value, choices=list(self.choices))
            try:
                value = read_limit()
                value["error"] = None
            except (CpuFreqError, OSError) as exc:
                if self.log:
                    self.log.warning("could not read the CPU limit: %s", exc)
                value = {"ac": None, "dc": None, "limit": None, "agrees": True,
                         "supported": False, "error": str(exc)}
            self._value = value
            self._read_at = time.monotonic()
            return dict(value, choices=list(self.choices))

    def apply(self, mhz) -> dict:
        value = apply_limit(mhz, self.sources)
        with self._lock:
            value["error"] = None
            self._value = value
            self._read_at = time.monotonic()
        if self.log:
            self.log.info("CPU maximum frequency set to %s (%s)",
                          describe(value.get("limit")),
                          "/".join(value.get("written") or ()) or "nothing")
        return dict(value, choices=list(self.choices))
