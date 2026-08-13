"""
Temperature-to-duty curves, with asymmetric response.

THE PROBLEM THIS SOLVES

Sitting at 100% for a whole session is loud for no benefit, and the interesting
moments are the quiet ones: an in-game dialog, a paused menu where the renderer
stops, an efficient game that never troubles the hardware. The fans should come
down for those. But a curve that comes down eagerly also oscillates - heat is
slow and fans are fast, so a naive controller hunts, and hunting is far more
irritating than steady noise.

So the response is deliberately asymmetric:

  UP    immediate, and uncapped. Heat is the thing we are protecting against;
        there is never a reason to approach a needed speed slowly.
  DOWN  rate-limited and delayed. The temperature must sit lower than the
        current operating point for `settle_seconds` before the duty starts
        falling, and then it falls at most `fall_step` percent per tick.

That asymmetry is the whole design. It gives fast protection, and a descent slow
enough that a two-second dip during a cutscene does not start a cycle, while a
genuine minute-long menu pause does bring the fans down.

Hysteresis on top: the operating temperature has to drop `hysteresis` degrees
below the level that set the current duty before that duty is reconsidered at
all. Without it, a temperature hovering exactly on a curve point flaps between
two duties forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registers import PERCENT_MIN_SPIN


@dataclass
class Curve:
    """
    Linear interpolation between (celsius, percent) points.

    Points must be sorted by temperature. Below the first point the fan runs at
    the first point's duty; above the last, the last. Duties are clamped to the
    stall floor by percent_to_raw later, so a curve may legitimately say 0
    (meaning "let it stop") or anything from PERCENT_MIN_SPIN upward.
    """

    points: list[tuple[float, float]]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("a curve needs at least one point")
        self.points = sorted(self.points, key=lambda p: p[0])
        for _, percent in self.points:
            if not 0 <= percent <= 100:
                raise ValueError(f"duty out of range: {percent}")

    def duty(self, celsius: float) -> float:
        if celsius <= self.points[0][0]:
            return self.points[0][1]
        if celsius >= self.points[-1][0]:
            return self.points[-1][1]
        for (t0, d0), (t1, d1) in zip(self.points, self.points[1:]):
            if t0 <= celsius <= t1:
                if t1 == t0:
                    return d1
                span = (celsius - t0) / (t1 - t0)
                return d0 + span * (d1 - d0)
        return self.points[-1][1]


@dataclass
class Governor:
    """
    Applies a curve over time, with the asymmetry described above.

    One Governor per fan. It holds the operating point between ticks, so it must
    not be shared.
    """

    curve: Curve
    hysteresis: float = 3.0
    settle_seconds: float = 8.0
    fall_step: float = 4.0
    critical_celsius: float = 90.0
    minimum_percent: float = 0.0

    current: float = field(default=0.0, init=False)
    _cool_since: float | None = field(default=None, init=False)
    _set_at_temp: float | None = field(default=None, init=False)
    _descending: bool = field(default=False, init=False)

    def reset(self) -> None:
        self.current = 0.0
        self._cool_since = None
        self._set_at_temp = None
        self._descending = False

    def update(self, celsius: float, now: float) -> float:
        target = max(self.curve.duty(celsius), self.minimum_percent)

        # Safety valve: above critical, nothing else in this method applies.
        if celsius >= self.critical_celsius:
            self.current = 100.0
            self._set_at_temp = celsius
            self._cool_since = None
            self._descending = False
            return self.current

        if target > self.current:
            # Up: immediately, all the way, every time.
            self.current = target
            self._set_at_temp = celsius
            self._cool_since = None
            self._descending = False
            return self.current

        if target == self.current:
            self._cool_since = None
            self._descending = False
            return self.current

        # Down. The two gates guard *starting* a descent, not continuing one.
        #
        # They used to be re-evaluated every tick, which deadlocked the fall:
        # the first step set _set_at_temp to the current (cooler) temperature,
        # so on the next tick the hysteresis test compared that temperature
        # against itself, passed, and reset the timer. The duty dropped exactly
        # one step and then sat there forever. Once a descent has been
        # authorised it runs to the target unless the temperature climbs again,
        # which is handled by the target > current branch above.
        if not self._descending:
            if (self._set_at_temp is not None
                    and celsius > self._set_at_temp - self.hysteresis):
                # Not meaningfully cooler than when we chose this duty.
                self._cool_since = None
                return self.current
            if self._cool_since is None:
                self._cool_since = now
                return self.current
            if now - self._cool_since < self.settle_seconds:
                return self.current
            self._descending = True

        self.current = max(target, self.current - self.fall_step)
        if self.current <= target:
            self._descending = False
            self._cool_since = None
            self._set_at_temp = celsius
        return self.current


# Profiles. `aggressive` is the default: it reaches high duty early and holds a
# high floor, on the assumption that the user is wearing headphones and would
# rather have the thermal headroom than the quiet.
PROFILES: dict[str, dict] = {
    "aggressive": {
        "description": "Loud and early. Big thermal headroom for long sessions.",
        "cpu": [(40, 45), (50, 60), (60, 75), (70, 90), (78, 100)],
        "gpu": [(40, 40), (50, 55), (60, 75), (70, 90), (78, 100)],
        "hysteresis": 3.0,
        "settle_seconds": 8.0,
        "fall_step": 4.0,
        "critical_celsius": 88.0,
        "minimum_percent": PERCENT_MIN_SPIN,
    },
    "balanced": {
        "description": "Ramps with the work. Quiet at idle.",
        "cpu": [(45, 0), (55, 40), (65, 55), (75, 75), (85, 100)],
        "gpu": [(45, 0), (55, 40), (65, 55), (75, 75), (85, 100)],
        "hysteresis": 4.0,
        "settle_seconds": 12.0,
        "fall_step": 3.0,
        "critical_celsius": 92.0,
        "minimum_percent": 0.0,
    },
    "quiet": {
        "description": "Stays off as long as it safely can.",
        "cpu": [(55, 0), (65, 35), (75, 50), (85, 75), (92, 100)],
        "gpu": [(55, 0), (65, 35), (75, 50), (85, 75), (92, 100)],
        "hysteresis": 5.0,
        "settle_seconds": 20.0,
        "fall_step": 2.0,
        "critical_celsius": 95.0,
        "minimum_percent": 0.0,
    },
}


def governors(profile: dict) -> tuple[Governor, Governor]:
    """Build the CPU and GPU governors from a profile dict."""
    shared = {
        "hysteresis": profile.get("hysteresis", 3.0),
        "settle_seconds": profile.get("settle_seconds", 8.0),
        "fall_step": profile.get("fall_step", 4.0),
        "critical_celsius": profile.get("critical_celsius", 90.0),
        "minimum_percent": profile.get("minimum_percent", 0.0),
    }
    return (
        Governor(Curve([tuple(p) for p in profile["cpu"]]), **shared),
        Governor(Curve([tuple(p) for p in profile["gpu"]]), **shared),
    )
