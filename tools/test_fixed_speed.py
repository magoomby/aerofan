"""
Phase 2: take the fans off the EC's curve and drive them directly.

This is the payoff, and it is also the first write that can do harm, so it is
built as an experiment with an explicit failure verdict rather than a command.

WHAT THE PHASE 1 DATA ALREADY TOLD US

0xB0 and 0xB1 sat at 57 the entire time the fans ramped from 10 to 14 (and
0xB3/0xB4 from 75 to 107). So 0xB0 is NOT the live duty in auto mode - the EC
drives the fans from its own internal curve and never looks at 0xB0 until custom
mode is on. That also explains the missing gale: with Gigabyte Control Center
gone, nothing has ever switched this EC off its default quiet curve.

THE ONE THING WE DO NOT KNOW

Which bit turns custom mode on. p37-ec says 0x0D bit 0; both NBFC configs write
0x0D bit 7 and call it custom mode. Rather than pick a side, step 2 below tries
each candidate and measures whether 0xB0 writes start doing anything. The test
for "did it work" is not the readback - the EC will happily store a byte it is
ignoring - it is whether the *tachometer* moves.

SAFETY

  * the 30% stall floor is enforced in percent_to_raw and cannot be overridden
  * every write is read back
  * Restorer puts 0x06, 0x08, 0x0C, 0x0D, 0xB0 and 0xB1 back on every exit
    path, including Ctrl-C, and restores 0x0D (custom off) before 0xB0
  * the ramp only ever goes UP from the EC's own idle value

    python tools\\test_fixed_speed.py --dry-run   # plumbing only
    python tools\\test_fixed_speed.py             # the real thing
    python tools\\test_fixed_speed.py --skip-independence

Listen throughout. You are the instrument this test is calibrated against.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerofan.control import Controller, Restorer, WriteLost  # noqa: E402
from aerofan.ec import ECError, EmbeddedController  # noqa: E402
from aerofan.load import HIGH_PRIORITY_CLASS, gpu_status, set_priority  # noqa: E402
from aerofan.pawnio import PawnIO, PawnIOUnavailable, is_elevated  # noqa: E402
from aerofan.registers import (  # noqa: E402
    BIT_CUSTOM_MODE, BIT_CUSTOM_TYPE_FIXED, BIT_DEEP_CONTROL, REG_CUSTOM_MODE,
    REG_CUSTOM_TYPE, REG_FAN1_SET, REG_FAN2_SET, percent_to_raw, raw_to_percent,
    set_bit,
)

# A response this size in the tachometer is unambiguous. Phase 1 saw 10 -> 14
# from a 20-second CPU load, so +3 cannot be mistaken for drift.
RESPONSE_THRESHOLD = 3

SETTLE_SECONDS = 12
RAMP_STEPS = (40, 55, 70, 85, 100)


class Log:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.rows: list[dict] = []

    def sample(self, phase: str, note: str = "") -> dict:
        state = self.controller.state()
        gpu = gpu_status()
        row = {
            "phase": phase,
            "note": note,
            "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "custom": state["custom"],
            "fixed_type": state["fixed_type"],
            "fan1_set": state["fan1_set"],
            "fan2_set": state["fan2_set"],
            "fan1_read": state["fan1_read"],
            "fan2_read": state["fan2_read"],
            "fan1_read_alt": state["fan1_read_alt"],
            "fan2_read_alt": state["fan2_read_alt"],
            "gpu_temp_c": gpu["temp_c"] if gpu else None,
        }
        self.rows.append(row)
        print(f"    {phase:<22} set={row['fan1_set']:3d}/{row['fan2_set']:3d}  "
              f"read={row['fan1_read']:3d}/{row['fan2_read']:3d}  "
              f"alt={row['fan1_read_alt']:3d}/{row['fan2_read_alt']:3d}"
              f"{('  ' + note) if note else ''}")
        return row

    def watch(self, phase: str, seconds: float, note: str = "") -> list[dict]:
        rows = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows.append(self.sample(phase, note))
            note = ""
            time.sleep(3.0)
        return rows


def baseline_readout(log: Log) -> int:
    rows = log.watch("baseline", 9)
    return max(r["fan1_read_alt"] for r in rows)


def try_custom_bit(controller: Controller, log: Log, ec, bits, label: str,
                   baseline: int) -> tuple[bool, int]:
    """
    Turn on a candidate custom-mode bit, command 100%, and see if the fan moves.

    Returns (worked, best_readout). The readback is not the test - the EC will
    store a byte it intends to ignore. The tachometer is the test.
    """
    print(f"\n  -- trying {label} --")

    # ORDER MATTERS. Park the duty at 100% BEFORE enabling custom mode. The
    # EC's idle 0xB0 is 57, which is below the stall floor - if custom mode
    # engaged with that still loaded, the fans would go quiet rather than loud,
    # which is the one direction we never want to move in by accident.
    raw = percent_to_raw(100)
    controller._write(REG_FAN1_SET, raw)
    controller._write(REG_FAN2_SET, raw)
    print(f"     commanded 0xB0/0xB1 = {raw} (0x{raw:02X}, 100%) first")

    before = ec.read(REG_CUSTOM_MODE)
    after = before
    for position in bits:
        after = set_bit(after, position, True)
    if after != before:
        controller._write(REG_CUSTOM_MODE, after)
    print(f"     0x0D: 0x{before:02X} -> 0x{after:02X}")
    print("     LISTEN - if this bit is the one, the fans go loud now.")

    rows = log.watch("probe-custom", SETTLE_SECONDS, label)
    best = max(r["fan1_read_alt"] for r in rows)
    delta = best - baseline
    worked = delta >= RESPONSE_THRESHOLD
    print(f"     tachometer {baseline} -> {best} (delta {delta:+d})  "
          f"=> {'RESPONDED' if worked else 'no response'}")

    if not worked:
        # Put this candidate back before trying the next, or we cannot tell
        # which bit was responsible for a later success.
        controller._write(REG_CUSTOM_MODE, before)
    return worked, best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2: fixed fan speed.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-independence", action="store_true",
                        help="skip the split-speed test at the end")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "fixed_speed_test.json")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        return print("Windows only.") or 1
    if not is_elevated():
        return print("Run elevated.") or 1

    set_priority(HIGH_PRIORITY_CLASS)
    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        return print(f"PawnIO unavailable: {exc}") or 1

    winner = None
    try:
        ec = EmbeddedController(io)
        controller = Controller(ec, dry_run=args.dry_run)
        log = Log(controller)

        print("\n== 1. baseline, EC still in charge ==")
        with Restorer(controller):
            baseline = baseline_readout(log)
            print(f"  idle tachometer (0xB3): {baseline}")

            print("\n== 2. set fixed-speed type (0x06 bit 4) ==")
            before, after = controller.fixed_speed_type(True)
            print(f"  0x06: 0x{before:02X} -> 0x{after:02X}")

            print("\n== 3. find the custom-mode bit ==")
            print("  The sources disagree. Trying each in order of evidence.")
            candidates = [
                ((BIT_DEEP_CONTROL,), "0x0D bit 7  (both NBFC configs)"),
                ((BIT_CUSTOM_MODE,), "0x0D bit 0  (p37-ec)"),
                ((BIT_CUSTOM_MODE, BIT_DEEP_CONTROL), "0x0D bits 0 and 7"),
            ]
            for bits, label in candidates:
                worked, _ = try_custom_bit(controller, log, ec, bits, label,
                                           baseline)
                if worked:
                    winner = (bits, label)
                    break

            if winner is None:
                print("\n" + "=" * 72)
                print("VERDICT: no candidate bit gave the fans to us.")
                print("=" * 72)
                print("  0xB0/0xB1 accept writes but the EC is ignoring them, so")
                print("  custom mode is enabled by something we have not found.")
                print("  Next lead: diff the full 256-register dump before and")
                print("  after toggling a fan profile in Gigabyte Control Center,")
                print("  which is the only thing known to have driven this EC.")
                return 2

            print(f"\n  >>> custom mode is {winner[1]} <<<")

            print("\n== 4. ramp ==")
            print("  Each step holds for 12s. Listen for the steps.")
            ramp = []
            for percent in RAMP_STEPS:
                raw = percent_to_raw(percent)
                controller._write(REG_FAN1_SET, raw)
                controller._write(REG_FAN2_SET, raw)
                rows = log.watch(f"ramp {percent}%", SETTLE_SECONDS,
                                 f"raw={raw} (0x{raw:02X})")
                # Drop the first sample: it is taken immediately after the
                # write, before the EC has updated, so it still reports the
                # PREVIOUS step. Taking max() over the whole phase let the
                # leftover 229 from the 100% probe make the 40% step read 229
                # and produced a bogus "not monotonic" verdict.
                settled = rows[1:] or rows
                ramp.append((percent, raw,
                             max(r["fan1_read_alt"] for r in settled),
                             max(r["fan1_read"] for r in settled)))

            print("\n  commanded -> observed")
            print(f"    {'%':>5} {'raw':>5} {'0xB3':>6} {'0xFC':>6}")
            for percent, raw, alt, fine in ramp:
                print(f"    {percent:>4}% {raw:>5} {alt:>6} {fine:>6}")

            monotonic = all(ramp[i][2] <= ramp[i + 1][2]
                            for i in range(len(ramp) - 1))
            print(f"\n  monotonic with commanded duty: {monotonic}")
            if not monotonic:
                print("  Not monotonic - either the EC is clamping, or 0xB0 is")
                print("  not a linear duty byte on this BIOS.")

            if not args.skip_independence:
                print("\n== 5. are the two fans independent? ==")
                print("  fan1 -> 100%, fan2 -> 40%. If 0xFC and 0xFE diverge,")
                print("  they are separate. If they stay locked, one 0xB0 drives")
                print("  both and 0xB1 is decorative.")
                controller._write(REG_FAN1_SET, percent_to_raw(100))
                controller._write(REG_FAN2_SET, percent_to_raw(40))
                rows = log.watch("split 100/40", SETTLE_SECONDS)
                diverged = any(r["fan1_read"] != r["fan2_read"] or
                               r["fan1_read_alt"] != r["fan2_read_alt"]
                               for r in rows)
                print(f"\n  readouts diverged: {diverged}")
                if diverged:
                    print("  The fans are independently controllable. The daemon")
                    print("  can run separate curves for CPU and GPU.")
                else:
                    print("  Locked together. Treat them as one fan - and always")
                    print("  write 0xB0 and 0xB1 to the same value, as p37-ec")
                    print("  says to.")

        print("\n== restored ==")
        print(f"  EC transport health: {ec.health}")
        args.out.write_text(json.dumps(log.rows, indent=1))
        print(f"  Raw samples written to {args.out}")
        return 0

    except WriteLost as exc:
        print(f"\n  WRITE NOT HELD: {exc}")
        print("  State restored.")
        return 1
    except ECError as exc:
        print(f"\n  EC transport error: {exc}")
        return 1
    finally:
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
