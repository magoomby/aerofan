"""
aerofan - drive the AERO 15's fans directly, or ask the service to.

    aerofan status              what the fans are doing now
    aerofan profile             which profile is active
    aerofan profile aggressive  switch to a curve profile
    aerofan max                 both fans to 100%. The pre-game button.
    aerofan set 85              both fans to 85%
    aerofan set 100 --fan 1     one fan only (0xB0; 0xB1 with --fan 2)
    aerofan auto                give the fans back to the EC
    aerofan cpu                 the CPU maximum frequency cap
    aerofan cpu 2.3ghz          cap it; 'unlimited' to remove the cap

TWO WAYS TO THE SAME PLACE

If the AeroFan service is running, every one of those goes down the named pipe
and needs no elevation at all - the service owns the driver and does the work.
That is the normal case once tools\\install.ps1 has been run.

If it is not running, the same commands drive the EC directly, and that needs
an elevated shell because opening the PawnIO driver is an administrator
operation. This is the fallback, and it is what the whole CLI used to be.

They are never both true. Two processes on the EC mailbox interleave and
corrupt each other's transactions - this machine has no Access_EC mutant to
serialise them - so when the service is up, direct access is refused rather
than raced.

WHAT "MAX" ACTUALLY DOES

    0x06 bit 4 = 1      fixed-speed type
    0xB0 = 0xB1 = 229   100% duty
    0x0D bit 7 = 1      custom mode on

in that order, each write verified. `auto` reverses the switch first, so the EC
is back in charge before anything else moves.

State survives until the EC resets - so until you reboot or sleep - when you
are driving it directly. Through the service it survives everything, because
the service writes it to state.json and re-applies it at boot. That is the
difference the service makes.
"""

from __future__ import annotations

import argparse
import sys

from . import cpufreq
from . import ipc
from .control import Controller
from .ec import ECError, EmbeddedController
from .load import gpu_status
from .pawnio import PawnIO, PawnIOUnavailable, is_elevated
from .registers import FAN_TACH_MAX, PERCENT_MIN_SPIN, raw_to_percent
from .state import (
    AUTO, MAX, SERVICE_NAME, describe_profile, normalise_profile,
    profile_names,
)


def open_controller():
    if sys.platform != "win32":
        raise SystemExit("Windows only.")
    if not is_elevated():
        raise SystemExit(
            "aerofan needs an elevated shell (the PawnIO driver is admin-only).\n"
            "Right-click PowerShell -> Run as administrator.\n\n"
            f"Or install the {SERVICE_NAME} service, after which none of these\n"
            "commands need elevation:  .\\tools\\install.ps1"
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


# -- direct mode -------------------------------------------------------------


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


def run_direct(args) -> int:
    io, controller = open_controller()
    try:
        if args.command == "status":
            show(controller)
            return 0

        if args.command == "profile":
            if not args.name:
                state = controller.state()
                print("\n  No service running, so there is no remembered"
                      " profile.")
                print(f"  Right now: {'custom mode (aerofan)' if state['custom'] else 'the EC own curve'}\n")
                return 0
            print(f"\n  Profiles need the daemon or the service. For a one-off"
                  f" here:\n")
            print("      python -m aerofan.cli max")
            print("      python -m aerofan.cli set 70")
            print(f"      python -m aerofan.daemon --profile {args.name}\n")
            return 1

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


# -- service mode ------------------------------------------------------------


def _number(value, suffix: str = "", width: int = 6) -> str:
    if isinstance(value, (int, float)):
        return f"{value:{width}.1f}{suffix}"
    return f"{'-':>{width}}{suffix}"


def show_service(data: dict) -> None:
    profile = data.get("profile") or AUTO
    effective = data.get("effective") or profile
    holding = data.get("custom")

    print(f"\n  in control : "
          f"{'the aerofan service (custom mode ON)' if holding else 'the EC own curve'}")
    print(f"  profile    : {profile} - {describe_profile(profile)}")
    if effective != profile:
        print(f"  effective  : {effective}"
              f"{'  (recovering from EC errors)' if data.get('degraded') else ''}")
    if data.get("error"):
        print(f"  note       : {data['error']}")

    cpu = data.get("cpu") or {}
    if cpu.get("supported"):
        if not cpu.get("agrees", True):
            print(f"  cpu max    : {cpufreq.describe(cpu.get('ac'))} plugged"
                  f" in, {cpufreq.describe(cpu.get('dc'))} on battery")
        else:
            print(f"  cpu max    : {cpufreq.describe(cpu.get('limit'))}")

    print(f"\n  cpu        : {_number(data.get('cpu_c'), 'C')}"
          f"   -> {_number(data.get('cpu_duty'), '%')}"
          f"   [{data.get('cpu_source') or '-'}]")
    print(f"  gpu        : {_number(data.get('gpu_c'), 'C')}"
          f"   -> {_number(data.get('gpu_duty'), '%')}"
          f"   [{data.get('gpu_source') or '-'}]")

    tach_max = data.get("tach_max") or FAN_TACH_MAX
    print(f"\n  {'':<8}{'applied':>12}{'tachometer':>14}")
    for index, (percent_key, tach_key) in enumerate(
            (("fan1_percent", "fan1_tach"), ("fan2_percent", "fan2_tach")), 1):
        percent = data.get(percent_key)
        tach = data.get(tach_key)
        tach_text = f"{tach}/{tach_max}" if tach is not None else "-"
        graph = bar(tach, tach_max) if isinstance(tach, int) else ""
        print(f"  fan {index}   {_number(percent, '%'):>12}{tach_text:>14}"
              f"   {graph}")
    print()


def run_via_service(args) -> int:
    try:
        if args.command == "status":
            show_service(ipc.status())
            return 0

        if args.command == "profile":
            if not args.name:
                data = ipc.status()
                print(f"\n  {data.get('profile')} - "
                      f"{describe_profile(data.get('profile') or AUTO)}")
                print(f"  Available: {', '.join(profile_names())}, or fixed:NN\n")
                return 0
            data = ipc.set_profile(normalise_profile(args.name))
            show_service(data)
            return 0

        if args.command == "auto":
            show_service(ipc.set_profile(AUTO))
            return 0

        if args.command == "max":
            print("\n  Taking the fans to 100%. This will be loud.")
            show_service(ipc.set_profile(MAX))
            return 0

        if args.command == "set":
            if args.fan is not None:
                print("\n  The service drives both fans from one profile, so"
                      " --fan is not")
                print("  available through it. For a single fan, stop the"
                      f" service first:")
                print(f"      sc stop {SERVICE_NAME}\n")
                return 1
            percent = args.percent
            if 0 < percent < PERCENT_MIN_SPIN:
                print(f"\n  {percent:.0f}% is below the {PERCENT_MIN_SPIN}%"
                      f" stall floor; using {PERCENT_MIN_SPIN}% instead.")
            show_service(ipc.set_profile(f"fixed:{max(percent, 0):g}"))
            return 0

        return 1
    except ipc.ServiceUnavailable as exc:
        print(f"\n  The service stopped answering: {exc}\n")
        return 1
    except (ipc.ProtocolError, ValueError) as exc:
        print(f"\n  {exc}\n")
        return 1


# -- the CPU cap, which needs no EC at all -----------------------------------


def run_cpu(args) -> int:
    """
    Show or set the CPU maximum frequency.

    Its own path because it touches nothing this file otherwise touches: no
    driver, no EC, no fans - just a Windows power setting. Through the service
    it needs no elevation; without one it needs administrator, and it will say
    so rather than failing halfway through.
    """
    if args.mhz is None:
        try:
            cpu = (ipc.status().get("cpu") or {}) if ipc.is_running() \
                else cpufreq.read_limit()
        except (ipc.ServiceUnavailable, ipc.ProtocolError,
                cpufreq.CpuFreqError, OSError) as exc:
            print(f"\n  Could not read the CPU limit: {exc}\n")
            return 1
        if not cpu.get("supported"):
            print("\n  This machine does not expose a maximum processor"
                  " frequency setting.\n")
            return 1
        if not cpu.get("agrees", True):
            print(f"\n  cpu max : {cpufreq.describe(cpu.get('ac'))} plugged in,"
                  f" {cpufreq.describe(cpu.get('dc'))} on battery")
        else:
            print(f"\n  cpu max : {cpufreq.describe(cpu.get('limit'))}")
        choices = cpu.get("choices") or list(cpufreq.DEFAULT_CHOICES)
        print("  choices : "
              + ", ".join(cpufreq.describe(c) for c in choices)
              + "   (or any MHz value)\n")
        return 0

    try:
        target = cpufreq.validate(args.mhz)
    except ValueError as exc:
        print(f"\n  {exc}\n")
        return 1

    if ipc.is_running():
        try:
            data = ipc.set_cpu_max(target)
        except (ipc.ServiceUnavailable, ipc.ProtocolError) as exc:
            print(f"\n  {exc}\n")
            return 1
        cpu = data.get("cpu") or {}
    else:
        if not is_elevated():
            print("\n  Setting the CPU limit needs an elevated shell, because"
                  " it writes to the")
            print("  active power scheme. Either run this from an"
                  " administrator PowerShell, or")
            print(f"  install the {SERVICE_NAME} service and it will do it for"
                  " you:  .\\tools\\install.ps1\n")
            return 1
        try:
            cpu = cpufreq.apply_limit(target)
        except (cpufreq.CpuFreqError, OSError) as exc:
            print(f"\n  {exc}\n")
            return 1

    print(f"\n  cpu max : {cpufreq.describe(cpu.get('limit'))}"
          f"   (plugged in and on battery)\n")
    return 0


# -- entry point -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aerofan",
        description="Direct fan control for the Gigabyte AERO 15.")
    parser.add_argument("--direct", action="store_true",
                        help="drive the EC here even if the service is running."
                             " Needs elevation, and they will fight.")
    sub = parser.add_subparsers(dest="command", required=True)

    # argparse runs help strings through %-formatting, so a literal percent
    # sign has to be doubled or add_parser raises "badly formed help string".
    sub.add_parser("status", help="show what the fans are doing")
    sub.add_parser("max", help="both fans to 100%% - the pre-game button")
    sub.add_parser("auto", help="give the fans back to the EC")

    profile_parser = sub.add_parser(
        "profile", help="show or change the active profile")
    profile_parser.add_argument("name", nargs="?", default=None)

    set_parser = sub.add_parser("set", help="set a fixed duty")
    set_parser.add_argument("percent", type=float)
    set_parser.add_argument("--fan", type=int, choices=(1, 2), default=None,
                            help="only one fan (default: both)")

    cpu_parser = sub.add_parser(
        "cpu", help="show or cap the CPU maximum frequency")
    cpu_parser.add_argument(
        "mhz", nargs="?", default=None,
        help="2300, 2.3ghz, or 'unlimited'. Omit to show the current cap.")

    args = parser.parse_args(argv)

    # The CPU cap is a power-scheme setting, not an EC one, so it never wants
    # the driver and does not care whether the service holds it.
    if args.command == "cpu":
        return run_cpu(args)

    if args.direct:
        return run_direct(args)
    if ipc.is_running():
        return run_via_service(args)
    return run_direct(args)


if __name__ == "__main__":
    raise SystemExit(main())
