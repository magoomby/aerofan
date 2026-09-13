"""
Where aerofan keeps things that have to outlive a process.

Three files, all under ``%ProgramData%\\aerofan``:

    state.json    the profile the user last chose. This is the whole point of
                  the service: at boot, before anyone logs in, it reads this
                  and applies it.
    aerofan.json  optional tuning (poll interval, temperature registers). Never
                  written by us - it is yours to edit.
    aerofan.log   the service log, rotated.

ProgramData rather than the user profile because the service runs as SYSTEM and
the tray runs as you, and both need to agree on what is current. It is also the
only location that still exists at boot before a profile is loaded.

PROFILE NAMES

The curve profiles come from ``curve.PROFILES``. Two pseudo-profiles sit
alongside them and are not curves at all:

    auto        give the fans back to the EC's own firmware curve. This is the
                resting state, the state we fail into, and what "Exit" on the
                tray icon means.
    max         both fans pinned at 100%.

and one parameterised form, ``fixed:NN``, which is ``aerofan set NN`` routed
through the service so the CLI still works while the service holds the driver.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .curve import PROFILES

APP_NAME = "aerofan"
SERVICE_NAME = "AeroFan"
SERVICE_DISPLAY_NAME = "AeroFan Fan Control"

PIPE_NAME = r"\\.\pipe\aerofan"

AUTO = "auto"
MAX = "max"
FIXED_PREFIX = "fixed:"

DEFAULT_PROFILE = AUTO

_FIXED_RE = re.compile(r"^fixed:(\d{1,3}(?:\.\d+)?)$")


def data_dir() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP_NAME


def state_file() -> Path:
    return data_dir() / "state.json"


def config_file() -> Path:
    return data_dir() / "aerofan.json"


def log_file() -> Path:
    return data_dir() / "aerofan.log"


def tray_log_file() -> Path:
    """
    The tray's own log, in the user's profile rather than ProgramData.

    Separate from the service log for two reasons. ProgramData\\aerofan is
    created by the installer running as administrator, so an unelevated tray
    cannot reliably write there; and the tray is one process per logon session
    while the service is one per machine, so sharing a file would interleave
    them. It exists at all because a GUI launched by pythonw.exe has no
    console: without a log, a tray icon that misbehaves offers nothing to look
    at, which is exactly the hole this was written to fill.
    """
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(root) / APP_NAME / "tray.log"


def ensure_tray_log_dir() -> Path:
    directory = tray_log_file().parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_data_dir() -> Path:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- profile names -----------------------------------------------------------


def fixed_percent(name: str) -> float | None:
    """Return the duty in ``fixed:NN``, or None if this is not a fixed profile."""
    match = _FIXED_RE.match(name or "")
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 100 else None


def is_valid_profile(name: str) -> bool:
    if name in (AUTO, MAX) or name in PROFILES:
        return True
    return fixed_percent(name) is not None


def normalise_profile(name: str) -> str:
    """
    Accept what a human would type and return a canonical profile name.

    Raises ValueError rather than guessing: a typo that silently selected the
    wrong curve would be a fan speed nobody asked for.
    """
    candidate = (name or "").strip().lower()
    if not candidate:
        raise ValueError("no profile given")
    if candidate in ("ec", "off", "release", "default"):
        candidate = AUTO
    if candidate in ("full", "100", "100%"):
        candidate = MAX
    percent = fixed_percent(candidate)
    if percent is not None:
        # Canonicalise fixed:85.0 -> fixed:85
        trimmed = int(percent) if float(percent).is_integer() else percent
        return f"{FIXED_PREFIX}{trimmed}"
    if not is_valid_profile(candidate):
        raise ValueError(
            f"unknown profile {name!r}. Known: "
            + ", ".join(profile_names())
            + ", or fixed:NN"
        )
    return candidate


def profile_names() -> list[str]:
    """Selectable profiles, in the order the tray menu should show them."""
    return [AUTO, "quiet", "balanced", "aggressive", MAX]


def describe_profile(name: str) -> str:
    if name == AUTO:
        return "The EC's own firmware curve. aerofan does not touch the fans."
    if name == MAX:
        return "Both fans at 100%. The pre-game button."
    percent = fixed_percent(name)
    if percent is not None:
        return f"Both fans held at {percent:g}%."
    profile = PROFILES.get(name)
    return profile["description"] if profile else name


def profile_catalogue() -> list[dict]:
    """What the tray menu is built from."""
    return [
        {"name": name, "description": describe_profile(name)}
        for name in profile_names()
        if name in (AUTO, MAX) or name in PROFILES
    ]


# -- persisted state ---------------------------------------------------------


def read_state() -> dict:
    """
    The last profile the user chose, or the safe default.

    Any problem reading it - missing, truncated by a power cut mid-write,
    hand-edited into nonsense - resolves to ``auto``. Falling back to the EC's
    own curve is always safe; falling back to a remembered 100% is not.
    """
    default = {"profile": DEFAULT_PROFILE, "updated": None}
    try:
        # utf-8-sig, not utf-8: Notepad and PowerShell's Set-Content both
        # write a UTF-8 byte order mark by default, and json.loads rejects
        # it outright. The installer writes this file from PowerShell, so
        # plain utf-8 here meant a hand-installed profile was silently
        # ignored at first boot and quietly replaced by the default.
        raw = json.loads(state_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    profile = raw.get("profile")
    if not isinstance(profile, str) or not is_valid_profile(profile):
        return default
    return {"profile": profile, "updated": raw.get("updated")}


def write_state(profile: str) -> None:
    """
    Record the active profile, atomically.

    Written on every change, so a crash or a power cut cannot leave the file
    describing a profile that was never applied. os.replace is atomic on NTFS,
    which is what stops a half-written file from being read at next boot.
    """
    ensure_data_dir()
    payload = {
        "profile": profile,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    target = state_file()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)
