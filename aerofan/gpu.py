r"""
Turning the discrete GPU off, which on an Optimus laptop is worth about 5 watts.

The RTX 2070 Super Max-Q in this machine idles at P8, 300 MHz, ~5.3 W and never
goes lower. It is not reaching RTD3 deep sleep, because a dozen ordinary
processes - explorer, Spotify, PowerToys, the terminal - each hold a handle on
it simply by enumerating DXGI adapters. Short of closing all of them, the only
way to get that 5 W back is to take the device away.

    pnputil /disable-device <instance id>
    pnputil /enable-device  <instance id>

No reboot. The device goes away and comes back within a second or two, and the
setting persists across reboots because it lives in the device's ConfigFlags -
which is the trap, and why uninstall.ps1 checks for it.

WHY pnputil AND NOT ctypes

The rest of this project talks to Win32 directly, and the equivalent here is
SetupDiSetClassInstallParams plus SetupDiCallClassInstaller with
DICS_DISABLE - perhaps 150 lines of ctypes, in which a mistake leaves a display
adapter in a state the user has to sort out in Device Manager. pnputil is a
Windows component, documented, supported, and it returns an exit code rather
than something to parse. Using it is the same call as using powercfg would have
been; the difference is that powrprof gave us a real API for the CPU cap and
there is no equally simple one here.

READING THE STATE IS A DIFFERENT PROBLEM

pnputil's *output* is localised - "Started", "Disabled" and the field labels
are all translated - so parsing it would work on this machine and quietly fail
on someone else's. The registry is not translated: every PnP device has a
ConfigFlags value under its Enum key, and bit 0 is CONFIGFLAG_DISABLED. So the
action goes through pnputil and the state comes from winreg, and nothing
depends on the language Windows happens to be in.

SAFETY

Disabling the only display adapter in a machine would leave you looking at a
blank screen with no way to undo it. This refuses to disable a GPU unless some
*other* display adapter is present and enabled. On this laptop the Intel UHD
drives the panel and the NVIDIA drives nothing - nvidia-smi reports
display_active: Disabled - so there is a real one to fall back to.
"""

from __future__ import annotations

import re
import subprocess
import winreg

ENUM_ROOT = r"SYSTEM\CurrentControlSet\Enum\PCI"

# {4d36e968-...} is the Display class. Compared case-insensitively.
DISPLAY_CLASS_GUID = "{4D36E968-E325-11CE-BFC1-08002BE10318}"

CONFIGFLAG_DISABLED = 0x00000001

VENDORS = {
    "VEN_10DE": "NVIDIA",
    "VEN_1002": "AMD",
    "VEN_8086": "Intel",
}

DISCRETE_VENDORS = ("VEN_10DE", "VEN_1002")

# pnputil exit codes worth distinguishing. 3010 is the standard
# "operation succeeded, reboot required" code across Windows tooling.
ERROR_SUCCESS_REBOOT_REQUIRED = 3010

_INSTANCE_RE = re.compile(r"^VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", re.I)


class GpuError(RuntimeError):
    """The device could not be enumerated or switched."""


def _no_window():
    """Keep pnputil from flashing a console window at a tray-triggered call."""
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info


def _adapter_from(vendor_key: str, instance_key: str) -> dict | None:
    path = rf"{ENUM_ROOT}\{vendor_key}\{instance_key}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            try:
                class_guid, _ = winreg.QueryValueEx(key, "ClassGUID")
            except FileNotFoundError:
                return None
            if (class_guid or "").upper() != DISPLAY_CLASS_GUID:
                return None
            try:
                description, _ = winreg.QueryValueEx(key, "DeviceDesc")
            except FileNotFoundError:
                description = vendor_key
            # DeviceDesc is usually "@oem18.inf,%nvidia_dev%;NVIDIA GeForce..."
            if ";" in description:
                description = description.rsplit(";", 1)[1]
            try:
                config_flags, _ = winreg.QueryValueEx(key, "ConfigFlags")
            except FileNotFoundError:
                config_flags = 0
    except OSError:
        return None

    match = _INSTANCE_RE.match(vendor_key)
    vendor_id = f"VEN_{match.group(1).upper()}" if match else ""
    return {
        "instance_id": rf"PCI\{vendor_key}\{instance_key}",
        "description": description,
        "vendor": VENDORS.get(vendor_id, vendor_id or "unknown"),
        "vendor_id": vendor_id,
        "discrete": vendor_id in DISCRETE_VENDORS,
        "enabled": not bool(int(config_flags) & CONFIGFLAG_DISABLED),
    }


def adapters() -> list[dict]:
    """Every PCI display adapter, with whether it is currently enabled."""
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ENUM_ROOT) as root:
            index = 0
            while True:
                try:
                    vendor_key = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not _INSTANCE_RE.match(vendor_key):
                    continue
                try:
                    with winreg.OpenKey(root, vendor_key) as vendor:
                        inner = 0
                        while True:
                            try:
                                instance_key = winreg.EnumKey(vendor, inner)
                            except OSError:
                                break
                            inner += 1
                            adapter = _adapter_from(vendor_key, instance_key)
                            if adapter:
                                found.append(adapter)
                except OSError:
                    continue
    except OSError as exc:
        raise GpuError(f"could not read the PCI device list: {exc}")
    return found


def discrete() -> dict | None:
    """The discrete GPU, or None on a machine that has only integrated graphics."""
    for adapter in adapters():
        if adapter["discrete"]:
            return adapter
    return None


def snapshot() -> dict:
    """
    What the tray and the CLI show. Never raises - a machine with no discrete
    GPU is a normal machine, not an error.
    """
    try:
        everything = adapters()
    except GpuError as exc:
        return {"present": False, "enabled": None, "error": str(exc),
                "description": None, "can_disable": False}

    target = next((a for a in everything if a["discrete"]), None)
    if target is None:
        return {"present": False, "enabled": None, "error": None,
                "description": None, "can_disable": False}

    others = [a for a in everything
              if a["instance_id"] != target["instance_id"] and a["enabled"]]
    return {
        "present": True,
        "enabled": target["enabled"],
        "description": target["description"],
        "instance_id": target["instance_id"],
        # Refusing to disable the last display adapter is the one safety rule
        # here, and it is checked for display rather than only at the moment of
        # switching, so the menu item can be greyed out instead of failing.
        "can_disable": bool(others),
        "fallback": others[0]["description"] if others else None,
        "error": None,
    }


def set_enabled(enabled: bool) -> dict:
    """
    Disable or enable the discrete GPU. Needs administrator or SYSTEM.

    Returns the snapshot afterwards. Raises GpuError with something readable
    if there is nothing to switch, or if switching it would leave the machine
    with no display adapter at all.
    """
    state = snapshot()
    if not state["present"]:
        raise GpuError("this machine has no discrete GPU to switch")
    if state["enabled"] == enabled:
        return state
    if not enabled and not state["can_disable"]:
        raise GpuError(
            "refusing to disable the only display adapter - there would be "
            "nothing left to drive the screen")

    verb = "/enable-device" if enabled else "/disable-device"
    try:
        result = subprocess.run(
            ["pnputil", verb, state["instance_id"]],
            capture_output=True, text=True, errors="replace", timeout=60,
            startupinfo=_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GpuError(f"could not run pnputil: {exc}")

    if result.returncode not in (0, ERROR_SUCCESS_REBOOT_REQUIRED):
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        raise GpuError(
            f"pnputil {verb} failed (exit {result.returncode})"
            + (f": {detail[-1].strip()}" if detail else "")
            + ". This needs administrator; the service runs as SYSTEM.")

    after = snapshot()
    after["reboot_required"] = result.returncode == ERROR_SUCCESS_REBOOT_REQUIRED
    return after


def describe(state: dict) -> str:
    if not state.get("present"):
        return "no discrete GPU"
    if state.get("enabled"):
        return "on"
    return "off"


def menu_label(state: dict) -> str:
    """The tray item. Its wording is the state, so it has to be exact."""
    if not state.get("present"):
        return "No discrete GPU on this machine"
    name = (state.get("description") or "discrete GPU").strip()
    # "NVIDIA GeForce RTX 2070 Super with Max-Q Design" is too long for a menu.
    short = name.replace("NVIDIA GeForce ", "").replace(" with Max-Q Design", "")
    if state.get("enabled"):
        return f"Disable discrete GPU  ({short})"
    return f"Enable discrete GPU  ({short})"
