"""
aerofan - drive the AERO 15's fans directly.

    aerofan status              what the fans are doing now
    aerofan max                 both fans to 100%. The pre-game button.
    aerofan set 85              both fans to 85%
    aerofan set 100 --fan 1     one fan only (0xB0; 0xB1 with --fan 2)
    aerofan auto                give the fans back to the EC

Everything needs an elevated shell - opening the PawnIO driver is an
administrator operation.

State survives until the EC resets, which means until you reboot or sleep the
machine. It is not persistent, and that is deliberate: a fan setting that
survived a crash with nothing managing it would be worse than one that does not.

WHAT "MAX" ACTUALLY DOES

    0x06 bit 4 = 1      fixed-speed type
    0xB0 = 0xB1 = 229   100% duty
    0x0D bit 7 = 1      custom mode on

in that order, each write verified. `auto` reverses the switch first, so the EC
is back in charge before anything else moves.
"""

from __future__ import annotations

import argparse
import sys

from .control import Controller
from .ec import ECError, EmbeddedController
from .load import gpu_status
from .pawnio import PawnIO, PawnIOUnavailable, is_elevated
from .registers import (
    FAN_TACH_MAX, PERCENT_MIN_SPIN, raw_to_percent,
)


def open_controller():
    if sys.platform != "win32":
        raise SystemExit("Windows only.")
    if not is_elevated():
        raise SystemExit(
            "aerofan needs an elevated shell (the PawnIO driver is admin-only).\n"
            "Right-click PowerShell -> Run as administrator."
        )
    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        raise SystemExit(f"PawnIO unavailable: {exc}")
    return io, Controller(EmbeddedController(io))


def bar(value: int, maximum: int, width: int = 24) -> str:
    if maximum <= 0:
        return " " * width
    filled = max(0, min(width, round(width * value / maximum)))
    return "#" * filled + "." * (width - filled)


def show(controller: Controller) -> None:
    state = controller.state()
    gpu = gpu_status()

    if state["custom"]:
        owner = "aerofan (custom mode ON)"
    else:
        owner = "the EC's own curve"
    print(f"\n  in control : {owner}")
    print(f"  fixed type : {'on' if state['fixed_type'] else 'off'}"
          f"    gaming: {'on' if state['gaming'] else 'off'}"
          f"    quiet: {'on' if state['quiet'] else 'off'}")

    print(f"\n  {'':<8}{'commanded':>12}{'applied':>12}{'tachometer':>14}")
    for index, (set_key, applied_key, tach_key) in enumerate(
            (("fan1_set", "fan1_applied", "fan1_tach"),
             ("fan2_set", "fan2_applied", "fan2_tach")), start=1):
        commanded = state[set_key]
        applied = state[applied_key]
        tach = state[tach_key]
        # Commanded only means anything while we hold custom mode.
        commanded_text = (f"{raw_to_percent(commanded):.0f}%"
                          if state["custom"] else "-")
        print(f"  fan {index}   {commanded_text:>12}"
              f"{raw_to_percent(applied):>11.0f}%"
              f"{tach:>10}/{FAN_TACH_MAX}   {bar(tach, FAN_TACH_MAX)}")

    if gpu:
        print(f"\n  gpu        : {gpu['temp_c']}C, {gpu['util_pct']}% util")
    if not state["custom"]:
        print("\n  'applied' is the EC's own decision. Run 'aerofan max' to"
              " take over.")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aerofan",
        description="Direct fan control for the Gigabyte AERO 15.")
    sub = parser.add_subparsers(dest="command", required=True)

    # argparse runs help strings through %-formatting, so a literal percent
    # sign has to be doubled or add_parser raises "badly formed help string".
    sub.add_parser("status", help="show what the fans are doing")
    sub.add_parser("max", help="both fans to 100%% - the pre-game button")
    sub.add_parser("auto", help="give the fans back to the EC")

    set_parser = sub.add_parser("set", help="set a fixed duty")
    set_parser.add_argument("percent", type=float)
    set_parser.add_argument("--fan", type=int, choices=(1, 2), default=None,
                            help="only one fan (default: both)")

    args = parser.parse_args(argv)
    io, controller = open_controller()

    try:
        if args.command == "status":
            show(controller)
            return 0

        if args.command == "auto":
            controller.release()
            print("\n  Fans returned to the EC's own curve.")
            show(controller)
            return 0

        if args.command == "max":
            print("\n  Taking the fans to 100%. This will be loud.")
            controller.take_control(100)
            show(controller)
            return 0

        if args.command == "set":
            percent = args.percent
            if 0 < percent < PERCENT_MIN_SPIN:
                print(f"\n  {percent:.0f}% is below the {PERCENT_MIN_SPIN}% stall"
                      f" floor; using {PERCENT_MIN_SPIN}% instead.")
                print("  Below that the PWM duty will not reliably keep the"
                      " blades turning,")
                print("  and a stalled fan the EC thinks is spinning is how"
                      " machines cook.")
            if args.fan is None:
                controller.take_control(percent)
            else:
                # One fan means the other keeps whatever it has, so custom mode
                # still has to be on for either to matter.
                controller.take_control(percent if args.fan == 1 else 100,
                                        percent if args.fan == 2 else 100)
                controller.set_speed(percent, fan=args.fan)
            show(controller)
            return 0

        return 1
    except ECError as exc:
        print(f"\n  EC error: {exc}")
        print("  Nothing is guaranteed to have been applied. Run"
              " 'aerofan status'.")
        return 1
    finally:
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
