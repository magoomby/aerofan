"""
Find the CPU and GPU temperature registers in the EC.

The daemon needs temperature to drive a curve, and we do not have a source yet.
Windows offers almost nothing on this machine: MSAcpi_ThermalZoneTemperature is
empty, Win32_TemperatureProbe returns two nameless stubs with no reading, and
the one live ACPI thermal zone (\\_tz.tz00) reads 301 K - 28 C - which is a
chassis zone, not a core. The EC certainly knows both temperatures; we just have
to work out where.

METHOD

Heat the CPU and watch all 256 registers. A temperature register has to:

  * stay inside a plausible range the whole time (25-105 C)
  * rise materially under load
  * fall again on cooldown - this is what separates a temperature from a
    counter, an uptime byte, or a fan duty that also rises
  * move smoothly rather than in a single step

For the GPU there is a stronger test available: nvidia-smi gives us ground
truth. Any register that tracks it closely IS the GPU temperature register, and
that also calibrates the units - if the numbers match nvidia-smi one for one,
the register is plain degrees C, and if it is roughly double, it is half-degrees.

    python tools\\find_temps.py                 # ~4 minutes
    python tools\\find_temps.py --load 180      # longer, hotter, clearer

Reads only. Nothing is written to the EC.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aerofan.ec import ECError, EmbeddedController  # noqa: E402
from aerofan.load import (  # noqa: E402
    HIGH_PRIORITY_CLASS, CpuLoad, gpu_status, set_priority,
)
from aerofan.pawnio import PawnIO, PawnIOUnavailable, is_elevated  # noqa: E402

# Registers we already understand - excluded so they cannot win the search.
KNOWN = {0x06, 0x08, 0x0C, 0x0D, 0xB0, 0xB1, 0xB3, 0xB4, 0xFC, 0xFE}

PLAUSIBLE_MIN, PLAUSIBLE_MAX = 25, 105
MIN_RISE = 6


def snapshot(ec: EmbeddedController) -> dict[int, int]:
    return ec.dump()


def collect(ec, label, seconds, interval, samples):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        started = time.monotonic()
        registers = snapshot(ec)
        gpu = gpu_status()
        samples.append({
            "phase": label,
            "t": round(time.monotonic(), 1),
            "gpu_temp_c": gpu["temp_c"] if gpu else None,
            "gpu_util": gpu["util_pct"] if gpu else None,
            "registers": registers,
        })
        print(f"    [{label:<8}] {len(samples):3d} samples  "
              f"gpu={samples[-1]['gpu_temp_c']}C  "
              f"({time.monotonic() - started:.1f}s per sweep)")
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


def series(samples, register, phase=None):
    return [s["registers"][register] for s in samples
            if register in s["registers"] and (phase is None or s["phase"] == phase)]


def correlation(a: list[float], b: list[float]) -> float:
    if len(a) < 3 or len(a) != len(b):
        return 0.0
    if len(set(a)) < 2 or len(set(b)) < 2:
        return 0.0
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return cov / (var_a ** 0.5 * var_b ** 0.5)


def analyse(samples: list[dict]) -> None:
    idle = [s for s in samples if s["phase"] == "idle"]
    load = [s for s in samples if s["phase"] == "load"]
    cool = [s for s in samples if s["phase"] == "cool"]

    gpu_truth = [s["gpu_temp_c"] for s in samples if s["gpu_temp_c"] is not None]
    have_gpu = len(gpu_truth) == len(samples)

    candidates = []
    for register in range(0x100):
        if register in KNOWN:
            continue
        everything = series(samples, register)
        if len(everything) != len(samples):
            continue
        if not all(PLAUSIBLE_MIN <= v <= PLAUSIBLE_MAX for v in everything):
            continue

        idle_values = series(idle, register)
        load_values = series(load, register)
        cool_values = series(cool, register)
        if not (idle_values and load_values and cool_values):
            continue

        idle_median = statistics.median(idle_values)
        load_peak = max(load_values)
        cool_final = statistics.median(cool_values[-3:]) if cool_values else load_peak

        rise = load_peak - idle_median
        fall = load_peak - cool_final
        if rise < MIN_RISE or fall < MIN_RISE / 2:
            continue

        gpu_corr = correlation([float(v) for v in everything],
                               [float(v) for v in gpu_truth]) if have_gpu else 0.0

        candidates.append({
            "register": register,
            "idle": idle_median,
            "peak": load_peak,
            "rise": rise,
            "fall": fall,
            "gpu_corr": round(gpu_corr, 3),
            "trace": everything,
        })

    print("\n" + "=" * 72)
    print("TEMPERATURE CANDIDATES")
    print("=" * 72)
    if not candidates:
        print("  None. Either the load was too short to move anything, or the")
        print("  temperatures are stored somewhere other than plain degrees -")
        print("  try --load 240, and check the trace of any register that moved.")
        return

    candidates.sort(key=lambda c: c["rise"], reverse=True)
    print(f"\n  {'reg':>5} {'idle':>6} {'peak':>6} {'rise':>6} {'fall':>6}"
          f" {'gpu r':>7}   trace")
    for c in candidates[:12]:
        spark = " ".join(f"{v}" for v in c["trace"][::max(1, len(c["trace"]) // 10)])
        print(f"  0x{c['register']:02X} {c['idle']:>6.0f} {c['peak']:>6}"
              f" {c['rise']:>6.0f} {c['fall']:>6.0f} {c['gpu_corr']:>7.2f}   {spark}")

    print()
    gpu_match = [c for c in candidates if c["gpu_corr"] >= 0.9]
    if gpu_match:
        best = max(gpu_match, key=lambda c: c["gpu_corr"])
        print(f"  GPU temperature is very likely 0x{best['register']:02X} "
              f"(r={best['gpu_corr']:.2f} against nvidia-smi).")
        gpu_truth_peak = max(gpu_truth)
        ratio = best["peak"] / gpu_truth_peak if gpu_truth_peak else 0
        if 0.9 <= ratio <= 1.1:
            print("  Its values match nvidia-smi one for one, so the units are"
                  " plain degrees C.")
        else:
            print(f"  Its peak is {ratio:.2f}x nvidia-smi's, so check the units"
                  f" before using it.")
    else:
        print("  Nothing tracked nvidia-smi closely. The GPU stayed cool during"
              " a CPU-only load,")
        print("  which is expected - the CPU candidates above are still valid.")

    cpu_candidates = [c for c in candidates if c["gpu_corr"] < 0.9]
    if cpu_candidates:
        best = cpu_candidates[0]
        print(f"\n  CPU temperature is most likely 0x{best['register']:02X}: "
              f"idle {best['idle']:.0f}, peak {best['peak']}, "
              f"and it came back down by {best['fall']:.0f}.")
        print("\n  Put it in the daemon config:")
        print(f'      "cpu_temp_register": {best["register"]},')
        if gpu_match:
            print(f'      "gpu_temp_register": {max(gpu_match, key=lambda c: c["gpu_corr"])["register"]},')

    print("\n  Sanity-check the winner before trusting it: idle should be a"
          " believable")
    print("  idle core temperature, and the trace should look like heat, not"
          " like a counter.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find EC temperature registers.")
    parser.add_argument("--idle", type=int, default=30)
    parser.add_argument("--load", type=int, default=150)
    parser.add_argument("--cool", type=int, default=90)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "temp_search.json")
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

    samples: list[dict] = []
    try:
        ec = EmbeddedController(io)
        print(f"\n== idle, {args.idle}s ==")
        collect(ec, "idle", args.idle, args.interval, samples)

        print(f"\n== load, {args.load}s ==")
        with CpuLoad(args.load) as load:
            print(f"  {load.workers} burners at IDLE priority")
            collect(ec, "load", args.load, args.interval, samples)

        print(f"\n== cooldown, {args.cool}s ==")
        collect(ec, "cool", args.cool, args.interval, samples)

        analyse(samples)
        print(f"\n  EC transport health: {ec.health}")
        args.out.write_text(json.dumps(
            [{**s, "registers": {f"0x{k:02X}": v
                                 for k, v in s["registers"].items()}}
             for s in samples], indent=1))
        print(f"  Raw samples written to {args.out}")
        return 0
    except ECError as exc:
        print(f"\n  EC transport error: {exc}")
        if samples:
            analyse(samples)
        return 1
    finally:
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
