"""
Mode control, with a failsafe that puts the EC back the way it was.

Everything here is read-modify-write on a single bit, never a whole-byte blast.
That matters: the NBFC config for the Aero15x v8 sets register 0x0C to the
literal 0xA1, but this machine reads 0x23 there, and 0x06 reads 0x60 where NBFC
expects 0x0B. The *bit positions* agree across sources; the surrounding bits do
not, and they belong to the firmware. Touch only the bit we mean.

Every write is verified by reading back. Without an Access_EC mutant a write can
be lost exactly like a read, and a silently-lost fan-speed write is the one
failure here that actually matters.

The Restorer is the safety net. It snapshots the mode registers before anything
is touched and puts them back on the way out - on normal exit, on an exception,
on Ctrl-C, and on interpreter shutdown. Fans left in a mode nobody is managing
is the state we never want to leave behind.
"""

from __future__ import annotations

import atexit
import signal
import sys
import threading

from .ec import WRITE_ATTEMPTS, EmbeddedController
from .registers import (
    BIT_CUSTOM_MODE,
    BIT_CUSTOM_TYPE_FIXED,
    BIT_GAMING,
    BIT_QUIET,
    CUSTOM_MODE_BIT,
    REG_CUSTOM_MODE,
    REG_CUSTOM_TYPE,
    REG_FAN1_APPLIED,
    REG_FAN1_SET,
    REG_FAN1_TACH,
    REG_FAN2_APPLIED,
    REG_FAN2_SET,
    REG_FAN2_TACH,
    REG_GAMING,
    REG_QUIET,
    WRITE_WHITELIST,
    bit,
    percent_to_raw,
    raw_to_percent,
    set_bit,
)

# The registers a Restorer snapshots and puts back. Deliberately only the mode
# and speed bytes - we do not attempt to restore the firmware's own state.
MANAGED = (REG_CUSTOM_TYPE, REG_QUIET, REG_GAMING, REG_CUSTOM_MODE,
           REG_FAN1_SET, REG_FAN2_SET)

READOUTS = (REG_FAN1_TACH, REG_FAN2_TACH, REG_FAN1_APPLIED, REG_FAN2_APPLIED)


class WriteRefused(RuntimeError):
    """A write was blocked before it reached the EC."""


class WriteLost(RuntimeError):
    """The EC did not hold the value we wrote."""


class Controller:
    def __init__(self, ec: EmbeddedController, dry_run: bool = False):
        self.ec = ec
        self.dry_run = dry_run
        self._lock = threading.RLock()

    # -- guarded primitives --------------------------------------------------

    def _write(self, register: int, value: int) -> int:
        if register not in WRITE_WHITELIST:
            raise WriteRefused(
                f"0x{register:02X} is not in the write whitelist. If this is "
                f"deliberate, add it to registers.WRITE_WHITELIST and say why."
            )
        if self.dry_run:
            print(f"    [dry-run] would write 0x{value:02X} to 0x{register:02X}")
            return self.ec.read(register)
        with self._lock:
            actual = self.ec.write_verified(register, value)
        if actual != value:
            # write_verified has already retried this several times, so by the
            # time we get here the EC really is declining to hold the value -
            # either clamping it, or filtering the address.
            raise WriteLost(
                f"wrote 0x{value:02X} to 0x{register:02X}, EC holds 0x{actual:02X} "
                f"after {WRITE_ATTEMPTS} attempts"
            )
        return actual

    def set_flag(self, register: int, position: int, on: bool) -> tuple[int, int]:
        """
        Read-modify-write one bit. Returns (before, after).

        The read must be stable. A read-modify-write built on a foreign byte
        would write that byte's other seven bits back into a control register,
        which is the worst thing in this file.
        """
        with self._lock:
            before = self.ec.read_stable(register)
            after = set_bit(before, position, on)
            if after == before:
                return before, before
            self._write(register, after)
            return before, after

    # -- modes ---------------------------------------------------------------

    def gaming(self, on: bool) -> tuple[int, int]:
        """
        Gaming mode. The safest write available: it can only raise fan speed,
        and the EC keeps ownership of the curve. Nothing here can stall a fan.
        """
        return self.set_flag(REG_GAMING, BIT_GAMING, on)

    def quiet(self, on: bool) -> tuple[int, int]:
        return self.set_flag(REG_QUIET, BIT_QUIET, on)

    def custom(self, on: bool) -> tuple[int, int]:
        """Master switch for manual control. Off means the EC decides."""
        return self.set_flag(REG_CUSTOM_MODE, BIT_CUSTOM_MODE, on)

    def fixed_speed_type(self, on: bool) -> tuple[int, int]:
        """Within custom mode: True = honour 0xB0/0xB1, False = auto-maximum."""
        return self.set_flag(REG_CUSTOM_TYPE, BIT_CUSTOM_TYPE_FIXED, on)

    # -- speed ---------------------------------------------------------------

    def set_speed(self, percent: float, fan: int | None = None) -> dict[int, int]:
        """
        Set a fixed duty. Requires custom mode and fixed type to be on already -
        this does not turn them on for you, because doing so implicitly is how
        you end up with fans you did not mean to own.

        percent_to_raw enforces the 30% stall floor.
        """
        raw = percent_to_raw(percent)
        targets = (REG_FAN1_SET, REG_FAN2_SET) if fan is None else \
                  ((REG_FAN1_SET,) if fan == 1 else (REG_FAN2_SET,))
        result = {}
        with self._lock:
            for register in targets:
                # Every write is a chance to interleave with acpi.sys, so do
                # not spend one restating a value the EC already holds. In a
                # steady-state curve this skips the large majority of writes.
                if self.ec.read_stable(register) == raw:
                    result[register] = raw
                    continue
                result[register] = self._write(register, raw)
        return result

    # -- the whole sequence, in the only safe order --------------------------

    def take_control(self, percent: float, percent2: float | None = None) -> dict:
        """
        Take the fans off the EC's curve and drive them at a fixed duty.

        Order is not negotiable:

          1. fixed-speed type on   - so 0xB0/0xB1 mean something
          2. duty written          - so there is a sane value loaded
          3. custom mode on        - the switch that hands us the fans

        Duty before the switch, always. The EC's idle 0xB0 is 57, below the
        stall floor, so engaging custom mode first would drop the fans to a
        duty that may not keep them turning. Loud is a recoverable mistake;
        stopped is not.
        """
        with self._lock:
            self.fixed_speed_type(True)
            self._write(REG_FAN1_SET, percent_to_raw(percent))
            self._write(REG_FAN2_SET,
                        percent_to_raw(percent if percent2 is None else percent2))
            self.set_flag(REG_CUSTOM_MODE, CUSTOM_MODE_BIT, True)
        return self.state()

    def release(self) -> dict:
        """
        Give the fans back to the EC.

        Custom mode off first, so the EC is deciding again before we restore
        the duty bytes it does not read.
        """
        with self._lock:
            self.set_flag(REG_CUSTOM_MODE, CUSTOM_MODE_BIT, False)
            self.set_flag(REG_CUSTOM_TYPE, BIT_CUSTOM_TYPE_FIXED, False)
        return self.state()

    # -- observation ---------------------------------------------------------

    def holds_control(self) -> bool:
        """
        Cheap check that custom mode is still on: one register, not ten.

        The daemon needs this every tick, and a full state() is 10 registers at
        3 agreeing reads each - 30 mailbox transactions every couple of seconds,
        for hours, on a bus we share with the OS with no mutex. This is 3.
        """
        return bit(self.ec.read_stable(REG_CUSTOM_MODE), CUSTOM_MODE_BIT)

    def state(self, stable: bool = True) -> dict:
        """
        Read the six control bytes and the four readouts.

        Stable by default: this is what decisions are made on. Ten registers at
        three agreeing reads each is roughly 30 transactions - still an order
        of magnitude less mailbox traffic than the 256-register dumps the probe
        was doing, and every value is one we can act on.
        """
        read = self.ec.read_stable if stable else self.ec.read
        with self._lock:
            raw = {reg: read(reg) for reg in MANAGED + READOUTS}
        return {
            "gaming": bit(raw[REG_GAMING], BIT_GAMING),
            "quiet": bit(raw[REG_QUIET], BIT_QUIET),
            # bit 7 is the real custom-mode switch on this BIOS. The first
            # version of this reported bit 0, which is why every sample of a
            # run that demonstrably HAD custom mode on logged "custom": false.
            "custom": bit(raw[REG_CUSTOM_MODE], CUSTOM_MODE_BIT),
            "custom_bit0": bit(raw[REG_CUSTOM_MODE], BIT_CUSTOM_MODE),
            "fixed_type": bit(raw[REG_CUSTOM_TYPE], BIT_CUSTOM_TYPE_FIXED),
            "fan1_set": raw[REG_FAN1_SET],
            "fan2_set": raw[REG_FAN2_SET],
            "fan1_applied": raw[REG_FAN1_APPLIED],
            "fan2_applied": raw[REG_FAN2_APPLIED],
            "fan1_tach": raw[REG_FAN1_TACH],
            "fan2_tach": raw[REG_FAN2_TACH],
            # Legacy keys, so the phase-1/2 tools keep working unchanged.
            "fan1_read": raw[REG_FAN1_TACH],
            "fan2_read": raw[REG_FAN2_TACH],
            "fan1_read_alt": raw[REG_FAN1_APPLIED],
            "fan2_read_alt": raw[REG_FAN2_APPLIED],
            "raw": raw,
        }


class Restorer:
    """
    Snapshot the managed registers, and put them back no matter how we leave.

    Covers four exits: normal, exception, Ctrl-C, and interpreter shutdown. The
    atexit hook is the important one - it catches the paths the context manager
    alone would miss.
    """

    def __init__(self, controller: Controller):
        self.controller = controller
        self.snapshot: dict[int, int] = {}
        self._restored = False
        self._previous_sigint = None

    def __enter__(self) -> "Restorer":
        ec = self.controller.ec
        self.snapshot = {reg: ec.read(reg) for reg in MANAGED}
        print("  snapshot: " + "  ".join(
            f"0x{r:02X}=0x{v:02X}" for r, v in self.snapshot.items()))
        atexit.register(self.restore)
        try:
            self._previous_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        except ValueError:
            pass  # not on the main thread
        return self

    def _on_sigint(self, *_args) -> None:
        print("\n  interrupted - restoring EC state")
        self.restore()
        if callable(self._previous_sigint):
            self._previous_sigint(*_args)
        raise KeyboardInterrupt

    def restore(self) -> None:
        if self._restored or not self.snapshot:
            return
        self._restored = True
        for register, value in self.snapshot.items():
            for attempt in range(4):
                try:
                    self.controller.ec.write(register, value)
                    actual = self.controller.ec.read(register)
                    if actual == value:
                        break
                except Exception as exc:  # keep going; restore every register
                    if attempt == 3:
                        print(f"  !! could not restore 0x{register:02X}: {exc}",
                              file=sys.stderr)
        print("  EC state restored.")

    def __exit__(self, *exc) -> None:
        self.restore()
        atexit.unregister(self.restore)
        if self._previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._previous_sigint)
            except ValueError:
                pass
