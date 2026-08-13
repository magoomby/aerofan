"""
Why do writes to 0x0C and 0x0D not stick, when 0x06 does?

The fixed-speed run proved three things at once:

    0x06: 0x60 -> 0x70    held
    0x0D: wrote 0x80, EC holds 0x00
    0x0C: wrote 0x33, EC holds 0x23

So the write path works - a byte we send does land in EC RAM, because 0x06 kept
it. Two registers specifically are being defended. There are only a few ways
that happens, and they need different responses:

  A. REVERTED. The write lands, then the EC's own firmware refreshes the byte
     from internal state a few milliseconds later. We would see the new value
     briefly and then watch it decay back. Fix: burst mode, or hold the value
     by rewriting it faster than the refresh.

  B. REJECTED. The byte never appears at all, not even for a moment. The EC is
     filtering writes to this address. Fix: find the unlock, or the real
     control path.

  C. RACED. acpi.sys wrote the old value back. Would look like A but be
     erratic rather than a consistent decay time.

This tool separates them by reading back as fast as the transport allows -
roughly every 1-3 ms - immediately after the write, then again at 100 ms, 500 ms
and 1 s. A value present in sample 1 and gone by sample 20 is case A with a
measurable decay time. A value absent from sample 1 is case B.

It then retries the same write inside an EC burst window, and finally hammers
the register in a tight loop to see whether the fans respond even if the byte
will not stay put.

SAFETY: 0xB0/0xB1 are set to 100% *before* any custom-mode bit is touched, so
that if custom mode does engage, the fans go loud rather than silent. Everything
is restored on exit including Ctrl-C.

    python tools\\diag_writes.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerofan.control import Controller, Restorer  # noqa: E402
from aerofan.ec import ECError, EmbeddedController  # noqa: E402
from aerofan.load import HIGH_PRIORITY_CLASS, set_priority  # noqa: E402
from aerofan.pawnio import PawnIO, PawnIOUnavailable, is_elevated  # noqa: E402
from aerofan.registers import (  # noqa: E402
    REG_CUSTOM_MODE, REG_CUSTOM_TYPE, REG_FAN1_READ_ALT, REG_FAN1_SET,
    REG_FAN2_SET, REG_GAMING, REG_QUIET, percent_to_raw,
)

FAST_SAMPLES = 25


def timeline(ec: EmbeddedController, register: int, value: int) -> dict:
    """Write, then watch the byte as closely as the transport allows."""
    original = ec.read_stable(register)
    started = time.monotonic()

    # MUST be write_verified, not write. The first version of this used a bare
    # write and then judged the register on what came back - so a single lost
    # write (roughly one in seven here) printed "REJECTED - the value never
    # appeared". That is how 0x06 and 0xB0 got called rejected on one run and
    # held on the next, with nothing changed. Landing the write and measuring
    # how long it survives are two different questions and must not share an
    # attempt.
    landed = ec.write_verified(register, value)
    write_done = time.monotonic()
    if landed != value:
        print(f"\n  0x{register:02X}: could not land 0x{value:02X} in "
              f"{6} verified attempts (EC holds 0x{landed:02X}) - "
              f"this one really is being refused")
        return {"register": register, "conclusion": "REFUSED after retries",
                "ever_seen": False, "last_seen_ms": None}

    fast = []
    for _ in range(FAST_SAMPLES):
        stamp = time.monotonic()
        try:
            fast.append((round((stamp - write_done) * 1000, 1), ec.read(register)))
        except ECError:
            fast.append((round((stamp - write_done) * 1000, 1), None))

    slow = []
    for delay in (0.1, 0.5, 1.0):
        time.sleep(delay)
        try:
            slow.append((int(delay * 1000), ec.read(register)))
        except ECError:
            slow.append((int(delay * 1000), None))

    ever_seen = any(v == value for _, v in fast + slow)
    last_seen = None
    for offset, seen in fast:
        if seen == value:
            last_seen = offset

    print(f"\n  0x{register:02X}: was 0x{original:02X}, wrote 0x{value:02X}"
          f"  (write took {(write_done - started) * 1000:.1f} ms)")
    print("    fast: " + " ".join(
        f"{offset:.0f}ms=" + ("--" if v is None else f"{v:02X}")
        for offset, v in fast[:12]))
    print("          " + " ".join(
        f"{offset:.0f}ms=" + ("--" if v is None else f"{v:02X}")
        for offset, v in fast[12:]))
    print("    slow: " + "  ".join(
        f"{offset}ms=" + ("--" if v is None else f"{v:02X}") for offset, v in slow))

    if not ever_seen:
        conclusion = "landed but never observed again - reads are unreliable"
    elif slow and slow[-1][1] == value:
        conclusion = "HELD - still set a second later"
    else:
        conclusion = f"REVERTED - held for about {last_seen} ms, then decayed"
    print(f"    => {conclusion}")

    # Put it back regardless of what happened.
    try:
        ec.write(register, original)
    except ECError:
        pass
    return {"register": register, "conclusion": conclusion,
            "ever_seen": ever_seen, "last_seen_ms": last_seen}


def burst_attempt(ec: EmbeddedController, register: int, value: int) -> None:
    """Same write, but inside an EC burst window."""
    original = ec.read(register)
    print(f"\n  0x{register:02X} inside burst mode:")
    acquired = ec.burst_enable()
    print(f"    burst acknowledged: {acquired}")
    if not acquired:
        print("    This EC did not grant burst. That is allowed, and it rules")
        print("    burst out as the answer.")
        return
    try:
        ec.write(register, value)
        readback = ec.read(register)
    finally:
        ec.burst_disable()
    after = ec.read(register)
    print(f"    in-burst readback: 0x{readback:02X}   after burst: 0x{after:02X}")
    if readback == value and after == value:
        print("    => BURST WORKS. This is the mechanism we were missing.")
    elif readback == value:
        print("    => held inside the window, lost on exit. The firmware")
        print("       refreshes it the moment burst ends.")
    else:
        print("    => still rejected. Not a burst problem.")
    try:
        ec.write(register, original)
    except ECError:
        pass


def hammer(ec: EmbeddedController, register: int, value: int,
           seconds: float = 6.0) -> None:
    """
    Rewrite the register as fast as we can and watch the tachometer.

    If the EC is refreshing the byte but acting on it in between, the fans will
    respond even though the value never looks set. Worth knowing: it would mean
    a viable, if ugly, control strategy.
    """
    original = ec.read(register)
    before = ec.read(REG_FAN1_READ_ALT)
    print(f"\n  hammering 0x{register:02X} = 0x{value:02X} for {seconds:.0f}s"
          f" (tachometer starts at {before})")
    print("    LISTEN.")
    deadline = time.monotonic() + seconds
    writes = 0
    peak = before
    while time.monotonic() < deadline:
        try:
            ec.write(register, value)
            writes += 1
        except ECError:
            pass
        if writes % 20 == 0:
            try:
                peak = max(peak, ec.read(REG_FAN1_READ_ALT))
            except ECError:
                pass
    after = ec.read(REG_FAN1_READ_ALT)
    print(f"    {writes} writes; tachometer peak {peak}, final {after}")
    if peak - before >= 3:
        print("    => THE FANS RESPONDED to this register.")
    else:
        print("    => no response.")
    try:
        ec.write(register, original)
    except ECError:
        pass


def main() -> int:
    if sys.platform != "win32":
        return print("Windows only.") or 1
    if not is_elevated():
        return print("Run elevated.") or 1

    set_priority(HIGH_PRIORITY_CLASS)
    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        return print(f"PawnIO unavailable: {exc}") or 1

    try:
        ec = EmbeddedController(io)
        controller = Controller(ec)

        with Restorer(controller):
            print("\n== 1. which registers accept a write at all? ==")
            print("  A control byte we can already change (0x06) is included as")
            print("  the positive control - if that one fails too, the problem")
            print("  is the transport, not the register.")

            current = {reg: ec.read(reg) for reg in
                       (REG_CUSTOM_TYPE, REG_QUIET, REG_GAMING, REG_CUSTOM_MODE,
                        REG_FAN1_SET, REG_FAN2_SET)}
            for reg, value in current.items():
                print(f"    0x{reg:02X} = 0x{value:02X}")

            results = []
            # 0x06 bit 4 - known good, positive control.
            results.append(timeline(ec, REG_CUSTOM_TYPE,
                                    current[REG_CUSTOM_TYPE] ^ 0x10))
            # 0xB0 - accepted writes before; confirm it still does.
            results.append(timeline(ec, REG_FAN1_SET, percent_to_raw(60)))
            # The two that failed.
            results.append(timeline(ec, REG_GAMING, current[REG_GAMING] | 0x10))
            results.append(timeline(ec, REG_CUSTOM_MODE, 0x80))
            results.append(timeline(ec, REG_CUSTOM_MODE, 0x01))

            print("\n== 2. does burst mode change the answer? ==")
            burst_attempt(ec, REG_GAMING, current[REG_GAMING] | 0x10)
            burst_attempt(ec, REG_CUSTOM_MODE, 0x80)

            print("\n== 3. hammer test ==")
            print("  First: park the fans at 100% so that if custom mode DOES")
            print("  engage, they go loud rather than silent.")
            raw = percent_to_raw(100)
            ec.write(REG_FAN1_SET, raw)
            ec.write(REG_FAN2_SET, raw)
            print(f"    0xB0/0xB1 = {raw} (0x{raw:02X}); readback "
                  f"0x{ec.read(REG_FAN1_SET):02X}/0x{ec.read(REG_FAN2_SET):02X}")
            hammer(ec, REG_CUSTOM_MODE, 0x80)

            print("\n" + "=" * 72)
            print("SUMMARY")
            print("=" * 72)
            for result in results:
                print(f"  0x{result['register']:02X}  {result['conclusion']}")
            rejected = [r for r in results if not r["ever_seen"]]
            if rejected and all(r["register"] in (REG_GAMING, REG_CUSTOM_MODE)
                                for r in rejected):
                print("\n  0x0C and 0x0D are filtered while 0x06 and 0xB0 are not.")
                print("  That is a firmware policy, not a bug in us. The next")
                print("  lead is what Gigabyte's own software does differently -")
                print("  most likely an unlock sequence, or a different command")
                print("  than 0x81 for this address range.")

        print(f"\n  EC transport health: {ec.health}")
        return 0
    except ECError as exc:
        print(f"\n  EC transport error: {exc}")
        return 1
    finally:
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
