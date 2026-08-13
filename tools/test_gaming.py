"""
Phase 1: the first write. Toggle gaming mode and watch what the fans do.

Gaming mode is the right first write. It flips one bit, it can only ever raise
fan speed, and the EC keeps ownership of the curve - there is no value we can
set that stalls a blade. Compare that with writing 0xB0 directly, which is a
duty byte we do not yet fully understand on this BIOS.

Four phases, so the effect of the bit is separable from the effect of heat:

    A  60s idle,  gaming OFF   - what the machine does on its own
    B  60s idle,  gaming ON    - does the bit alone move the fans?
    C  90s load,  gaming ON    - does it change the ramp under heat?
    D  60s idle,  gaming OFF   - does it come back down?

Only register 0x0C bit 4 is ever written, by read-modify-write. Every write is
read back. The Restorer puts 0x0C back on any exit path including Ctrl-C.

    python tools\\test_gaming.py            # the full four phases
    python tools\\test_gaming.py --dry-run  # prove the plumbing, write nothing
    python tools\\test_gaming.py --quick    # 20s phases, for impatient debugging

Listen during phase B. That is the whole point of it.
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
from aerofan.load import (  # noqa: E402
    HIGH_PRIORITY_CLASS, CpuLoad, gpu_status, set_priority,
)
from aerofan.pawnio import PawnIO, PawnIOUnavailable, is_elevated  # noqa: E402
from aerofan.registers import (  # noqa: E402
    BIT_GAMING, REG_GAMING, raw_to_percent,
)

SAMPLE_INTERVAL = 3.0


class Run:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.rows: list[dict] = []

    def sample(self, phase: str) -> dict:
        state = self.controller.state()
        gpu = gpu_status()
        row = {
            "phase": phase,
            "t": round(time.monotonic(), 1),
            "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gaming": state["gaming"],
            "fan1_set": state["fan1_set"],
            "fan2_set": state["fan2_set"],
            "fan1_read": state["fan1_read"],
            "fan2_read": state["fan2_read"],
            "fan1_read_alt": state["fan1_read_alt"],
            "fan2_read_alt": state["fan2_read_alt"],
            "gpu_temp_c": gpu["temp_c"] if gpu else None,
            "gpu_util_pct": gpu["util_pct"] if gpu else None,
        }
        self.rows.append(row)
        print(
            f"    [{phase:<12}] gaming={'ON ' if row['gaming'] else 'off'}  "
            f"set={row['fan1_set']:3d}/{row['fan2_set']:3d}  "
            f"read={row['fan1_read']:3d}/{row['fan2_read']:3d}  "
            f"alt={row['fan1_read_alt']:3d}/{row['fan2_read_alt']:3d}  "
            f"gpu={row['gpu_temp_c']}C/{row['gpu_util_pct']}%"
        )
        return row

    def sample_for(self, phase: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.sample(phase)
            time.sleep(SAMPLE_INTERVAL)


def summarise(run: Run) -> None:
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)

    def stats(phase: str, key: str):
        values = [r[key] for r in run.rows if r["phase"] == phase]
        if not values:
            return None
        return min(values), max(values), values[-1]

    fields = [
        ("fan1_read", "fan1 read 0xFC"),
        ("fan2_read", "fan2 read 0xFE"),
        ("fan1_read_alt", "fan1 read 0xB3"),
        ("fan2_read_alt", "fan2 read 0xB4"),
        ("fan1_set", "fan1 set  0xB0"),
        ("fan2_set", "fan2 set  0xB1"),
        ("gpu_temp_c", "gpu temp"),
    ]
    phases = ["A idle off", "B idle ON", "C load ON", "D idle off"]

    print(f"\n  {'':<18}" + "".join(f"{p:>18}" for p in phases))
    for key, label in fields:
        cells = ""
        for phase in phases:
            result = stats(phase, key)
            cells += f"{'-':>18}" if not result else \
                f"{f'{result[0]}-{result[1]} ({result[2]})':>18}"
        print(f"  {label:<18}{cells}")
    print("\n  Cells are min-max (final).")

    a = stats("A idle off", "fan1_read")
    b = stats("B idle ON", "fan1_read")
    c = stats("C load ON", "fan1_read")

    print()
    # Guard against concluding anything about a bit that never changed. A dry
    # run, or a write the EC silently refused, both land here - and phase B
    # comparing equal to phase A means nothing at all in that case.
    gaming_seen = {r["gaming"] for r in run.rows}
    if gaming_seen == {False}:
        print("  Gaming mode was never actually ON in any sample - this was a")
        print("  dry run, or the write did not take. NOTHING can be concluded")
        print("  about the effect of the bit. Re-run without --dry-run.")
    elif a and b and b[1] > a[1]:
        print(f"  Gaming mode alone moved the fans at idle "
              f"({a[1]} -> {b[1]}). The bit works and is independent of heat.")
    elif a and b:
        print("  Gaming mode alone did NOT move the fans at idle. Either the")
        print("  bit does nothing on this BIOS, or its effect is only visible")
        print("  once there is heat to respond to - compare phase C.")
    if b and c and c[1] > b[1]:
        print(f"  Load raised them further ({b[1]} -> {c[1]}), so the readout")
        print("  really is tracking fan speed rather than a mode echo.")

    f1 = [r["fan1_read"] for r in run.rows]
    f2 = [r["fan2_read"] for r in run.rows]
    if f1 == f2:
        print("\n  0xFC and 0xFE were identical in every sample. Either both fans")
        print("  are driven together, or one of these is a mirror rather than a")
        print("  second tachometer. Phase 2 settles it by writing 0xB0 alone.")
    else:
        differences = sum(1 for x, y in zip(f1, f2) if x != y)
        print(f"\n  0xFC and 0xFE differed in {differences}/{len(f1)} samples,")
        print("  so the two fans report independently.")

    gpu_temps = [r["gpu_temp_c"] for r in run.rows if r["gpu_temp_c"] is not None]
    gpu_utils = [r["gpu_util_pct"] for r in run.rows if r["gpu_util_pct"] is not None]
    if gpu_temps and gpu_utils and max(gpu_utils) < 10:
        print(f"\n  GPU stayed idle (max {max(gpu_utils)}% util, "
              f"{min(gpu_temps)}-{max(gpu_temps)}C) throughout. So any movement")
        print("  in fan 2 above came from CPU heat, not from the GPU.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1: gaming-mode write test.")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the whole sequence but never write")
    parser.add_argument("--quick", action="store_true",
                        help="20s phases instead of 60/90")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "gaming_test.json")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("Windows only.")
        return 1
    if not is_elevated():
        print("Run elevated.")
        return 1

    idle_seconds = 20 if args.quick else 60
    load_seconds = 20 if args.quick else 90

    set_priority(HIGH_PRIORITY_CLASS)

    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        print(f"PawnIO unavailable: {exc}")
        return 1

    try:
        ec = EmbeddedController(io)
        controller = Controller(ec, dry_run=args.dry_run)
        run = Run(controller)

        current = ec.read(REG_GAMING)
        print(f"\n  0x0C is currently 0x{current:02X}; bit {BIT_GAMING} "
              f"(gaming) is {'set' if current & (1 << BIT_GAMING) else 'clear'}")
        print(f"  we will write 0x{current | (1 << BIT_GAMING):02X}, "
              f"i.e. that one bit and nothing else")
        if args.dry_run:
            print("  DRY RUN - no write will actually happen")

        with Restorer(controller):
            print(f"\n== A: {idle_seconds}s idle, gaming OFF ==")
            controller.gaming(False)
            run.sample_for("A idle off", idle_seconds)

            print(f"\n== B: {idle_seconds}s idle, gaming ON ==")
            print("  LISTEN NOW. Nothing is loading the CPU; any change you")
            print("  hear is the bit alone.")
            before, after = controller.gaming(True)
            print(f"  0x0C: 0x{before:02X} -> 0x{after:02X}")
            run.sample_for("B idle ON", idle_seconds)

            print(f"\n== C: {load_seconds}s CPU load, gaming ON ==")
            with CpuLoad(load_seconds) as load:
                print(f"  {load.workers} burner processes at IDLE priority "
                      f"(leaving headroom for the sampler and acpi.sys)")
                run.sample_for("C load ON", load_seconds)

            print(f"\n== D: {idle_seconds}s idle, gaming OFF ==")
            controller.gaming(False)
            run.sample_for("D idle off", idle_seconds)

        summarise(run)
        print(f"\n  EC transport health: {ec.health}")
        args.out.write_text(json.dumps(run.rows, indent=1))
        print(f"  Raw samples written to {args.out}")
        return 0

    except WriteLost as exc:
        print(f"\n  WRITE NOT HELD: {exc}")
        print("  The EC did not keep the value. It may be clamping it, or the")
        print("  write was lost. State has been restored either way.")
        return 1
    except ECError as exc:
        print(f"\n  EC transport error: {exc}")
        return 1
    finally:
        io.close()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
