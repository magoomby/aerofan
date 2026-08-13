"""
The control loop.

    python -m aerofan.daemon --profile aggressive
    python -m aerofan.daemon --profile aggressive --dry-run
    python -m aerofan.daemon --config C:\\path\\to\\aerofan.json

Elevated. Ctrl-C hands the fans back to the EC on the way out.

WHAT IT DOES EACH TICK

    read temperatures  ->  governor per fan  ->  write duty only if it changed

The "only if it changed" is not an optimisation. We share the EC mailbox with
Windows' ACPI driver with no mutex to arbitrate, so every write is a chance to
interleave with theirs. A steady-state curve writes almost nothing.

HOW IT FAILS

Every failure path ends in the same place: give the fans back to the EC. The
firmware's own curve is conservative and always available, and it is a far
better fallback than a duty nobody is managing. That happens on:

  * no usable temperature reading
  * repeated EC errors
  * the watchdog not seeing a tick
  * any unhandled exception, Ctrl-C, or interpreter shutdown

It also watches for the EC quietly taking the fans back - after sleep/resume the
EC resets and custom mode is gone - and re-applies rather than believing its own
last command.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from .control import Controller
from .curve import PROFILES, governors
from .ec import ECError, EmbeddedController
from .pawnio import PawnIO, PawnIOUnavailable, is_elevated
from .registers import (
    CUSTOM_MODE_BIT, REG_CUSTOM_MODE, REG_FAN1_APPLIED, REG_FAN2_APPLIED,
    percent_to_raw, raw_to_percent,
)
from .sensors import (
    ECTemperature, NvidiaTemperature, SensorSet, ThermalZoneTemperature,
)

DEFAULT_CONFIG = {
    "profile": "aggressive",
    "poll_seconds": 2.0,
    # Filled in by tools/find_temps.py. Null means "not found yet", and the
    # daemon falls back to nvidia-smi for the GPU and the ACPI thermal zone for
    # the CPU - which works, but reacts late.
    "cpu_temp_register": None,
    "gpu_temp_register": None,
    "temp_scale": 1.0,
    "watchdog_seconds": 15.0,
    "ec_error_budget": 5,
}


def load_config(path: Path | None) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path and path.is_file():
        config.update(json.loads(path.read_text()))
    return config


class Watchdog:
    """
    Releases the fans if the control loop stops ticking.

    The loop can wedge without raising - a driver call that never returns, a
    thread deadlock. From outside, that looks identical to everything being
    fine, except the fans are frozen at whatever the last command was. This
    notices and undoes it.
    """

    def __init__(self, timeout: float, on_stall) -> None:
        self.timeout = timeout
        self.on_stall = on_stall
        self._last_tick = time.monotonic()
        self._stop = threading.Event()
        self._fired = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def tick(self) -> None:
        self._last_tick = time.monotonic()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            if self._fired:
                continue
            if time.monotonic() - self._last_tick > self.timeout:
                self._fired = True
                print(f"\n  WATCHDOG: no tick in {self.timeout:.0f}s - "
                      f"releasing the fans to the EC")
                try:
                    self.on_stall()
                except Exception as exc:
                    print(f"  watchdog release failed: {exc}")


class Daemon:
    def __init__(self, controller: Controller, config: dict, dry_run: bool):
        self.controller = controller
        self.ec = controller.ec
        self.config = config
        self.dry_run = dry_run
        self.profile_name = config["profile"]
        self.profile = PROFILES[self.profile_name]
        self.cpu_governor, self.gpu_governor = governors(self.profile)
        self.sensors = self._build_sensors()
        self.running = True
        self.ec_errors = 0
        self.applied = {1: None, 2: None}
        self._released = False

    def _build_sensors(self) -> SensorSet:
        scale = self.config.get("temp_scale", 1.0)
        cpu_sources, gpu_sources = [], []
        if self.config.get("cpu_temp_register") is not None:
            cpu_sources.append(ECTemperature(
                self.ec, int(self.config["cpu_temp_register"]), scale, "cpu"))
        if self.config.get("gpu_temp_register") is not None:
            gpu_sources.append(ECTemperature(
                self.ec, int(self.config["gpu_temp_register"]), scale, "gpu"))
        gpu_sources.append(NvidiaTemperature())
        # The chassis zone lags badly, so it is last and only for the CPU side.
        cpu_sources.append(ThermalZoneTemperature())
        return SensorSet(cpu_sources, gpu_sources)

    # -- lifecycle -----------------------------------------------------------

    def release(self) -> None:
        if self._released or self.dry_run:
            return
        self._released = True
        try:
            self.controller.release()
            print("  Fans returned to the EC.")
        except Exception as exc:
            print(f"  !! could not release the fans: {exc}")

    def _ensure_control(self) -> bool:
        """
        Confirm we still hold the fans, and take them back if not.

        After sleep/resume the EC resets and custom mode is simply gone. The
        daemon would keep computing correct duties and writing them into a
        register nobody reads. Checking the switch rather than trusting our own
        last write is the only way to notice.
        """
        try:
            state = self.controller.state()
        except ECError:
            return False
        if state["custom"]:
            return True
        print("  custom mode is off (EC reset, or something else took over) -"
              " re-engaging")
        if not self.dry_run:
            self.controller.take_control(
                self.applied[1] if self.applied[1] is not None else 100,
                self.applied[2] if self.applied[2] is not None else 100)
        return True

    # -- the loop ------------------------------------------------------------

    def tick(self, now: float) -> dict:
        sample = self.sensors.sample()
        if not sample["any_ok"]:
            raise ECError(
                f"no usable temperature (cpu: {sample['cpu'].source}, "
                f"gpu: {sample['gpu'].source})")

        # Each fan follows its own sensor where it has one, but neither is
        # allowed to idle while the other side is hot - one chassis, two fans,
        # and both move air over both.
        hottest = sample["hottest"]
        cpu_c = sample["cpu"].celsius if sample["cpu"].ok else hottest
        gpu_c = sample["gpu"].celsius if sample["gpu"].ok else hottest

        cpu_duty = self.cpu_governor.update(cpu_c, now)
        gpu_duty = self.gpu_governor.update(gpu_c, now)

        for fan, duty in ((1, cpu_duty), (2, gpu_duty)):
            raw = percent_to_raw(duty)
            if self.applied[fan] is not None and \
                    percent_to_raw(self.applied[fan]) == raw:
                continue  # no change worth a mailbox transaction
            if not self.dry_run:
                self.controller.set_speed(duty, fan=fan)
            self.applied[fan] = duty

        return {"cpu_c": cpu_c, "gpu_c": gpu_c,
                "cpu_duty": cpu_duty, "gpu_duty": gpu_duty,
                "cpu_source": sample["cpu"].source,
                "gpu_source": sample["gpu"].source}

    def run(self) -> int:
        poll = float(self.config["poll_seconds"])
        budget = int(self.config["ec_error_budget"])

        print(f"\n  profile   : {self.profile_name} - {self.profile['description']}")
        print(f"  cpu curve : {self.profile['cpu']}")
        print(f"  gpu curve : {self.profile['gpu']}")
        print(f"  response  : up immediately, down {self.profile['fall_step']}%"
              f" per tick after {self.profile['settle_seconds']}s settled,"
              f" {self.profile['hysteresis']}C hysteresis")
        print(f"  critical  : {self.profile['critical_celsius']}C -> 100%")
        if self.dry_run:
            print("  DRY RUN - nothing will be written")

        if self.config.get("cpu_temp_register") is None:
            print("\n  NOTE: no CPU temperature register configured, so the CPU"
                  " side falls back")
            print("  to the ACPI chassis zone, which lags real load badly. Run"
                  " tools/find_temps.py")
            print("  and put the result in the config for a curve that reacts"
                  " in time.")

        watchdog = Watchdog(float(self.config["watchdog_seconds"]), self.release)
        watchdog.start()

        if not self.dry_run:
            print("\n  taking the fans")
            self.controller.take_control(100)
            self.applied = {1: 100.0, 2: 100.0}

        last_line = ""
        try:
            while self.running:
                now = time.monotonic()
                try:
                    if not self._ensure_control():
                        raise ECError("lost contact with the EC")
                    result = self.tick(now)
                    self.ec_errors = 0
                    watchdog.tick()

                    line = (f"  cpu {result['cpu_c']:5.1f}C -> "
                            f"{result['cpu_duty']:5.1f}%   "
                            f"gpu {result['gpu_c']:5.1f}C -> "
                            f"{result['gpu_duty']:5.1f}%")
                    if line != last_line:
                        print(line)
                        last_line = line
                except ECError as exc:
                    self.ec_errors += 1
                    print(f"  EC trouble ({self.ec_errors}/{budget}): {exc}")
                    if self.ec_errors >= budget:
                        print("  too many consecutive failures - giving the fans"
                              " back to the EC")
                        return 1
                time.sleep(poll)
            return 0
        finally:
            watchdog.stop()
            self.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="aerofan curve daemon.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll", type=float, default=None)
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        return print("Windows only.") or 1
    if not is_elevated():
        return print("Run elevated - the PawnIO driver is admin-only.") or 1

    config = load_config(args.config)
    if args.profile:
        config["profile"] = args.profile
    if args.poll:
        config["poll_seconds"] = args.poll

    try:
        io = PawnIO().open()
    except PawnIOUnavailable as exc:
        return print(f"PawnIO unavailable: {exc}") or 1

    controller = Controller(EmbeddedController(io), dry_run=args.dry_run)
    daemon = Daemon(controller, config, args.dry_run)

    def stop(*_):
        print("\n  stopping")
        daemon.running = False

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    try:
        return daemon.run()
    finally:
        daemon.release()
        print(f"  EC transport health: {controller.ec.health}")
        io.close()


if __name__ == "__main__":
    raise SystemExit(main())
