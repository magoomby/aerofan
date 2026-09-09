"""
Temperature sources, in order of preference, with honest failure.

This machine is unusually bare. MSAcpi_ThermalZoneTemperature is empty,
Win32_TemperatureProbe returns two nameless stubs with no reading, and there is
no LibreHardwareMonitor. What we have:

  1. EC registers      - once find_temps.py has identified them. Best source:
                         it is the same chip that drives the fans, so it is
                         what the firmware's own curve reacts to.
  2. nvidia-smi        - real GPU temperature, ~200 ms per call, always right.
  3. ACPI thermal zone - \\_tz.tz00, a single chassis zone reading ~301 K. Coarse
                         and slow-moving, but it exists with no EC traffic at
                         all, which makes it a reasonable last resort.

A Reading carries where it came from, because a curve driven by a stale or
absent sensor is more dangerous than no curve at all - the daemon hands the fans
back to the EC rather than guess.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from .ec import ECError, EmbeddedController


@dataclass
class Reading:
    celsius: float | None
    source: str
    age: float = 0.0

    @property
    def ok(self) -> bool:
        return self.celsius is not None


class ECTemperature:
    """A temperature read straight out of an EC register."""

    def __init__(self, ec: EmbeddedController, register: int,
                 scale: float = 1.0, name: str = "ec"):
        self.ec = ec
        self.register = register
        self.scale = scale
        self.name = name

    def read(self) -> Reading:
        try:
            raw = self.ec.read_stable(self.register)
        except ECError:
            return Reading(None, f"{self.name} 0x{self.register:02X} (failed)")
        celsius = raw * self.scale
        # A temperature register that reads 0 or 255 is not reporting a
        # temperature; it is reporting that something is wrong.
        if not 5 <= celsius <= 120:
            return Reading(None, f"{self.name} 0x{self.register:02X} "
                                 f"(implausible: {celsius:.0f})")
        return Reading(celsius, f"{self.name} 0x{self.register:02X}")


class NvidiaTemperature:
    """GPU temperature from nvidia-smi, cached so the curve can poll freely."""

    def __init__(self, min_interval: float = 2.0):
        self.min_interval = min_interval
        self._last = 0.0
        self._value: float | None = None

    def read(self) -> Reading:
        now = time.monotonic()
        if now - self._last < self.min_interval and self._value is not None:
            return Reading(self._value, "nvidia-smi (cached)", now - self._last)
        try:
            output = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip().splitlines()[0]
            self._value = float(output.strip())
            self._last = now
            return Reading(self._value, "nvidia-smi")
        except Exception:
            return Reading(None, "nvidia-smi (failed)")


class ThermalZoneTemperature:
    """
    ACPI thermal zone via the Windows performance counter, in kelvin.

    Coarse and slow, and on this machine it is a chassis zone rather than a
    core, so it lags real load badly. Present as a fallback only - a curve
    driven by this alone will always be late.
    """

    def __init__(self, min_interval: float = 5.0):
        self.min_interval = min_interval
        self._last = 0.0
        self._value: float | None = None

    def read(self) -> Reading:
        now = time.monotonic()
        if now - self._last < self.min_interval and self._value is not None:
            return Reading(self._value, "thermal zone (cached)", now - self._last)
        try:
            output = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Counter '\\Thermal Zone Information(*)\\Temperature')"
                 ".CounterSamples[0].CookedValue"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            self._value = float(output) - 273.15
            self._last = now
            return Reading(self._value, "thermal zone")
        except Exception:
            return Reading(None, "thermal zone (failed)")


class SensorSet:
    """
    The temperatures the curve runs on, each with a fallback chain, plus
    detection of a sensor that has stopped being a sensor.

    WHY THE STUCK DETECTOR EXISTS

    A two-hour session of Baldur's Gate 3 reported the CPU at 27.9 C for the
    entire run - not approximately, exactly, every single tick. That is
    \\_tz.tz00 reading a constant 301 K: the counter exists, Windows reports it,
    and it means nothing. The CPU fan sat at 45% for two hours while the GPU
    fan was at 98%.

    A reading that never moves is indistinguishable from a working sensor if
    you only look at one value, so we compare across sides. A source that has
    not changed at all over the window, while another source HAS moved, is not
    measuring anything. Requiring the other side to be moving is what stops a
    genuinely steady idle temperature from being called stuck.
    """

    def __init__(self, cpu_sources: list, gpu_sources: list,
                 stuck_seconds: float = 120.0):
        self.cpu_sources = cpu_sources
        self.gpu_sources = gpu_sources
        self.stuck_seconds = stuck_seconds
        self._history: dict[str, list[tuple[float, float]]] = {
            "cpu": [], "gpu": []}
        self._warned: set[str] = set()

    @staticmethod
    def _first_ok(sources) -> Reading:
        last = Reading(None, "no sources configured")
        for source in sources:
            last = source.read()
            if last.ok:
                return last
        return last

    def cpu(self) -> Reading:
        return self._first_ok(self.cpu_sources)

    def gpu(self) -> Reading:
        return self._first_ok(self.gpu_sources)

    def _record(self, side: str, reading: Reading, now: float) -> None:
        if not reading.ok:
            self._history[side].clear()
            return
        history = self._history[side]
        history.append((now, reading.celsius))
        cutoff = now - self.stuck_seconds * 2
        while history and history[0][0] < cutoff:
            history.pop(0)

    def _flat_for_window(self, side: str, now: float) -> bool:
        history = self._history[side]
        window = [v for t, v in history if t >= now - self.stuck_seconds]
        if len(window) < 5:
            return False
        if history[0][0] > now - self.stuck_seconds:
            return False  # not enough elapsed time yet, only enough samples
        return len(set(window)) == 1

    def sample(self, now: float | None = None) -> dict:
        import time as _time
        now = _time.monotonic() if now is None else now

        cpu, gpu = self.cpu(), self.gpu()
        self._record("cpu", cpu, now)
        self._record("gpu", gpu, now)

        cpu_flat = self._flat_for_window("cpu", now)
        gpu_flat = self._flat_for_window("gpu", now)

        # Only call it stuck if the *other* side is demonstrably alive.
        for side, flat, other_flat, reading in (
                ("cpu", cpu_flat, gpu_flat, cpu),
                ("gpu", gpu_flat, cpu_flat, gpu)):
            if flat and not other_flat and reading.ok:
                if side not in self._warned:
                    print(f"  SENSOR STUCK: {side} has read exactly "
                          f"{reading.celsius:.1f}C for {self.stuck_seconds:.0f}s "
                          f"while the other side moved.")
                    print(f"  Source was '{reading.source}'. Ignoring it and "
                          f"following the hotter side instead.")
                    self._warned.add(side)
                stuck = Reading(None, f"{reading.source} (stuck at "
                                      f"{reading.celsius:.1f}C)")
                if side == "cpu":
                    cpu = stuck
                else:
                    gpu = stuck
            elif not flat:
                self._warned.discard(side)

        readings = [r.celsius for r in (cpu, gpu) if r.ok]
        return {
            "cpu": cpu,
            "gpu": gpu,
            "hottest": max(readings) if readings else None,
            "any_ok": bool(readings),
        }
