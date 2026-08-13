"""
Phase 0: find out what this machine's EC actually does. Reads only.

The register map in registers.py comes from two Aero 15 sources that agree with
each other, but neither was written against an AERO 15 Studio XB on BIOS HFB07.
Before anything writes to the EC we want evidence, from this machine, that:

  * the EC responds at all through PawnIO, and the transport is stable
  * bytes 0xB0/0xB1 look like duty values rather than noise
  * one of the two candidate read-register pairs tracks fan speed
  * we can see the difference between idle and loaded

Method is the same as fn_probe.py in fusion-kbd: take a baseline, change one
thing in the physical world, take another sample, diff, and print a verdict
rather than a wall of numbers. Raw samples go to JSON so a wrong verdict can be
re-read later without re-running.

    python -m aerofan.probe                 # baseline + idle stability
    python -m aerofan.probe --load 60       # 60s of CPU load, sampling throughout

Nothing in this file writes to the EC. It cannot change your fan speed.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .ec import ECError, EmbeddedController
from .pawnio import PawnIO, PawnIOUnavailable, is_elevated
from .registers import (
    INTERESTING,
    REG_FAN1_READ,
    REG_FAN1_READ_ALT,
    REG_FAN1_SET,
    REG_FAN2_READ,
    REG_FAN2_READ_ALT,
    REG_FAN2_SET,
    raw_to_percent,
)

READ_CANDIDATES = (REG_FAN1_READ, REG_FAN2_READ, REG_FAN1_READ_ALT, REG_FAN2_READ_ALT)


from .load import HIGH_PRIORITY_CLASS, CpuLoad, set_priority  # noqa: E402


class Sampler:
    def __init__(self, ec: EmbeddedController):
        self.ec = ec
        self.samples: list[dict] = []

    def snapshot(self, label: str) -> dict[int, int]:
        registers = self.ec.dump()
        self.samples.append(
            {
                "label": label,
                "t": round(time.monotonic(), 3),
                "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "registers": {f"0x{k:02X}": v for k, v in registers.items()},
            }
        )
        return registers


def _print_interesting(registers: dict[int, int]) -> None:
    for reg, label in INTERESTING.items():
        value = registers.get(reg)
        if value is None:
            continue
        extra = ""
        if reg in (REG_FAN1_SET, REG_FAN2_SET):
            extra = f"  (~{raw_to_percent(value)}% if this is a duty byte)"
        print(f"    0x{reg:02X}  {value:3d}  0x{value:02X}  {label}{extra}")


def _diff(before: dict[int, int], after: dict[int, int]) -> dict[int, tuple[int, int]]:
    return {
        reg: (before[reg], after[reg])
        for reg in before
        if reg in after and before[reg] != after[reg]
    }


def run(load_seconds: int, out_path: Path, interval: float) -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 1
    if not is_elevated():
        print(
            "Not running elevated. Opening the PawnIO driver needs administrator\n"
            "rights - reopen PowerShell with 'Run as administrator' and retry."
        )
        return 1

    # The sampler must outrank the burners, or its EC poll loop gets starved
    # and every read looks lost. This is what killed the first --load run.
    set_priority(HIGH_PRIORITY_CLASS)

    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        print(f"PawnIO unavailable: {exc}")
        return 1

    try:
        ec = EmbeddedController(io)
        if ec.mutex.available:
            print(f"  EC mutex: {ec.mutex.name}")
        else:
            print(
                "  EC mutex: absent (this firmware does not create Access_EC).\n"
                "  We are unsynchronised against Windows' own ACPI driver, so\n"
                "  transactions retry. Watch the retry rate in the verdict - a\n"
                "  few percent is normal, double digits is not."
            )

        sampler = Sampler(ec)

        print("\n== baseline ==")
        started = time.monotonic()
        baseline = sampler.snapshot("baseline")
        elapsed = time.monotonic() - started
        print(f"  256 registers read in {elapsed:.2f}s "
              f"({elapsed / 256 * 1000:.2f} ms per transaction)")
        _print_interesting(baseline)

        print("\n== idle stability (5 samples, 1s apart) ==")
        for index in range(5):
            time.sleep(1.0)
            sampler.snapshot(f"idle-{index}")
        idle_moves = _movers(sampler.samples[0:6])
        print(f"  registers that moved at idle: "
              f"{', '.join(f'0x{r:02X}' for r in sorted(idle_moves)) or 'none'}")

        if load_seconds > 0:
            print(f"\n== under load ({load_seconds}s, all cores) ==")
            print("  Listen. The fans should audibly ramp.")
            with CpuLoad(load_seconds) as load:
                print(f"  {load.workers} burner processes at IDLE priority")
                deadline = time.monotonic() + load_seconds
                index = 0
                while time.monotonic() < deadline:
                    time.sleep(interval)
                    registers = sampler.snapshot(f"load-{index}")
                    readouts = "  ".join(
                        f"0x{r:02X}={registers.get(r, 0):3d}"
                        for r in READ_CANDIDATES
                    )
                    print(f"    [{index:3d}] {readouts}")
                    index += 1

            print("\n== cooldown (20s) ==")
            for index in range(4):
                time.sleep(5.0)
                sampler.snapshot(f"cool-{index}")

        verdict(sampler)
        print(f"\n  EC transport health: {ec.health}")
        out_path.write_text(json.dumps(sampler.samples, indent=1))
        print(f"\nRaw samples written to {out_path}")
        return 0
    except ECError as exc:
        print(f"\nEC transport error: {exc}")
        print("Nothing was written. Safe to retry.")
        return 1
    finally:
        io.close()


def _movers(samples: list[dict]) -> set[int]:
    if len(samples) < 2:
        return set()
    keys = samples[0]["registers"].keys()
    moved = set()
    for key in keys:
        values = {sample["registers"][key] for sample in samples}
        if len(values) > 1:
            moved.add(int(key, 16))
    return moved


def verdict(sampler: Sampler) -> None:
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)

    idle = [s for s in sampler.samples if s["label"].startswith(("baseline", "idle"))]
    load = [s for s in sampler.samples if s["label"].startswith("load")]

    def series(samples, register):
        key = f"0x{register:02X}"
        return [s["registers"][key] for s in samples if key in s["registers"]]

    if not load:
        print("  No load phase was run, so fan-tracking cannot be judged.")
        print("  Re-run with --load 60 once you are happy the reads are stable.")
    else:
        print("  Candidate fan-speed read registers, idle vs load:\n")
        best = []
        for register in READ_CANDIDATES:
            idle_values, load_values = series(idle, register), series(load, register)
            if not idle_values or not load_values:
                continue
            idle_mean = statistics.fmean(idle_values)
            load_max = max(load_values)
            delta = load_max - idle_mean
            plausible = 0 <= load_max <= 40 and delta >= 2
            flag = "  <-- tracks load" if plausible else ""
            print(f"    0x{register:02X}  idle mean {idle_mean:5.1f}   "
                  f"load max {load_max:3d}   delta {delta:+5.1f}{flag}")
            if plausible:
                best.append(register)
        print()
        if len(best) >= 2:
            print(f"  Two registers track load: "
                  f"{', '.join(f'0x{r:02X}' for r in best)}.")
            print("  That is consistent with two independently-reported fans, and")
            print("  matches the documented map. Proceed to a single guarded write.")
        elif len(best) == 1:
            print(f"  Only 0x{best[0]:02X} tracks load. Either the second fan reads")
            print("  elsewhere, or both fans share one tachometer. Widen the search")
            print("  with the full-diff list below before writing anything.")
        else:
            print("  Nothing tracked load in the candidate registers. Do NOT write.")
            print("  Check the full-diff list below for registers that did move -")
            print("  the map may sit somewhere else entirely on this BIOS.")

    if idle and load:
        moved = _movers(idle + load)
        stable_at_idle = _movers(idle)
        interesting = sorted(moved - stable_at_idle)
        print("\n  Registers that moved under load but were stable at idle:")
        print("   ", ", ".join(f"0x{r:02X}" for r in interesting) or "none")
        print("  (Temperatures and fan tachometers should both be in this set.)")

    print("\n  Reminder: this run wrote nothing. The next step does.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only EC probe for AERO 15.")
    parser.add_argument("--load", type=int, default=0,
                        help="seconds of CPU load to apply (0 = skip)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between samples under load")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "probe_results.json")
    args = parser.parse_args(argv)
    return run(args.load, args.out, args.interval)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
