"""
Register map and safety limits for the Gigabyte AERO 15 embedded controller.

Sources, which independently agree on the important addresses:

  * tangalbert919/p37-ec-aero-15, "Aero 15 Fan Control Registers.md"
  * NoteBook FanControl config "Gigabyte Aero15x v8" (author: maximmaxim345)

Nothing here has been confirmed on *this* machine yet - an AERO 15 Studio XB on
BIOS HFB07. Confirming it is the entire job of `aerofan.probe`, which only reads.
Treat every WRITE_* constant below as a hypothesis until the probe agrees.
"""

from __future__ import annotations

# --- EC I/O ports ------------------------------------------------------------
# The standard ACPI embedded controller pair. PawnIO's LpcACPIEC module refuses
# every other port, which is a useful backstop against a typo in this file.
EC_DATA_PORT = 0x62
EC_CMD_PORT = 0x66

# --- Mode control ------------------------------------------------------------
# Bit positions, not masks. Use bit() below.
REG_CUSTOM_TYPE = 0x06  # bit 4: 0 = auto-maximum, 1 = fixed speed
BIT_CUSTOM_TYPE_FIXED = 4

REG_QUIET = 0x08  # bit 6: quiet mode
BIT_QUIET = 6

REG_GAMING = 0x0C  # bit 4: gaming mode
BIT_GAMING = 4

REG_CUSTOM_MODE = 0x0D
# THE SOURCES DISAGREE HERE, and it matters more than anywhere else in this file.
#
#   p37-ec-aero-15 : 0x0D bit 0 = "custom mode activation"
#                    0x0D bit 7 = "deep control mode for fan curves"
#   NBFC Aero15x v8: writes 0x0D = 128 (bit 7), described as "Set custom mode on"
#   NBFC Aero16    : ORs 0x0D with 128 (bit 7), same description
#
# So the two NBFC configs - which are the ones actually tested against Windows -
# both use bit 7 for the thing p37-ec calls bit 0. One of them has the naming
# wrong, and guessing costs us a fan we thought we owned but do not.
# tools/test_fixed_speed.py resolves it empirically: it tries each candidate and
# checks whether 0xB0 writes start having a visible effect.
BIT_CUSTOM_MODE = 0
BIT_DEEP_CONTROL = 7

# Ordered by how much evidence backs them, best first.
CUSTOM_MODE_CANDIDATES = (
    (BIT_DEEP_CONTROL, "bit 7 (both NBFC configs use this)"),
    (BIT_CUSTOM_MODE, "bit 0 (p37-ec calls this custom mode activation)"),
    (None, "bits 0 and 7 together"),
)

# --- Fan speed set -----------------------------------------------------------
REG_FAN1_SET = 0xB0  # CPU-side fan
REG_FAN2_SET = 0xB1  # GPU-side fan

# --- Fan speed read ----------------------------------------------------------
# Which pair is live varies by generation: 0xFC/0xFE on Aero 14/15, 0xB3/0xB4 on
# Aero 16. The probe reads all four and reports which ones actually move.
REG_FAN1_READ = 0xFC
REG_FAN2_READ = 0xFE
REG_FAN1_READ_ALT = 0xB3
REG_FAN2_READ_ALT = 0xB4

# The read registers are not RPM. They are a small counter, empirically 0..22.
FAN_READ_MAX = 22

# --- Speed scale -------------------------------------------------------------
# Raw PWM duty byte. 0xE5 (229) is full.
RAW_MAX = 0xE5

# Hard floor. Below roughly 30% duty the PWM does not reliably keep the fan
# turning, and p37-ec warns explicitly against it. A fan that has stalled while
# the EC believes it is spinning is the failure mode that cooks the machine, so
# this floor is enforced in the write path and is NOT configurable.
RAW_MIN_SPIN = 0x44  # 68 == ~30%
PERCENT_MIN_SPIN = 30

# Registers this project is ever allowed to write. The daemon rejects anything
# else, so a bug elsewhere cannot scribble on an arbitrary EC byte. Keep this
# list as short as the feature set allows.
WRITE_WHITELIST = frozenset(
    {
        REG_CUSTOM_TYPE,
        REG_QUIET,
        REG_GAMING,
        REG_CUSTOM_MODE,
        REG_FAN1_SET,
        REG_FAN2_SET,
    }
)

# Registers worth watching in the probe, with human labels.
INTERESTING = {
    REG_CUSTOM_TYPE: "custom type (bit4 fixed)",
    REG_QUIET: "quiet (bit6)",
    REG_GAMING: "gaming (bit4)",
    REG_CUSTOM_MODE: "custom mode (bit0) / deep (bit7)",
    REG_FAN1_SET: "fan1 set",
    REG_FAN2_SET: "fan2 set",
    REG_FAN1_READ_ALT: "fan1 read (Aero16 position)",
    REG_FAN2_READ_ALT: "fan2 read (Aero16 position)",
    REG_FAN1_READ: "fan1 read (Aero15 position)",
    REG_FAN2_READ: "fan2 read (Aero15 position)",
}


def bit(value: int, position: int) -> bool:
    """True if `position` is set in `value`."""
    return bool(value & (1 << position))


def set_bit(value: int, position: int, on: bool) -> int:
    """Return `value` with `position` forced on or off."""
    mask = 1 << position
    return (value | mask) if on else (value & ~mask & 0xFF)


def percent_to_raw(percent: float) -> int:
    """
    Map 0-100% onto the EC's duty byte, honouring the stall floor.

    0 is passed through untouched: it means "off", which is a legitimate state
    the EC handles itself at idle. Anything above 0 is lifted to at least
    RAW_MIN_SPIN rather than being silently clamped to something that would not
    turn the blades.
    """
    if percent <= 0:
        return 0
    if percent > 100:
        percent = 100.0
    raw = round(RAW_MAX * percent / 100.0)
    return max(RAW_MIN_SPIN, min(RAW_MAX, raw))


def raw_to_percent(raw: int) -> float:
    """Inverse of percent_to_raw, for display. Not exact - the map is lossy."""
    if raw <= 0:
        return 0.0
    return round(100.0 * min(raw, RAW_MAX) / RAW_MAX, 1)
