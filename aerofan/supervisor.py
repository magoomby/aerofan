"""
The control engine, with a profile you can change while it runs.

``daemon.py`` used to own this loop, with the profile fixed at startup and the
only way out being Ctrl-C. That is fine for a terminal you are sitting in front
of, and no use at all as a service: the whole point of the service is that the
profile outlives the process and can be changed by something that is not a
keyboard interrupt.

So the loop moved here and grew three things:

    a switchable profile     ``request_profile()`` from any thread; the change
                             lands at the top of the next tick, in the loop's
                             own thread, so nothing else ever touches the EC.
    telemetry                ``snapshot()`` - what the tray's tooltip shows.
    degrade instead of exit  the daemon exited on a run of EC errors. A service
                             cannot exit; it hands the fans back to the EC,
                             waits, and tries the profile again.

WHAT "auto" MEANS HERE

``auto`` is a profile like any other as far as the menu is concerned, but it is
the absence of control: custom mode off, no writes, the EC's own firmware curve
back in charge. It is the resting state, the state every failure path lands in,
and what the tray's Exit does. Everything in this file is arranged so that
getting to ``auto`` is the one operation that cannot fail to be attempted.
"""

from __future__ import annotations

import logging
import threading
import time

from .control import Controller, WriteLost
from .curve import PROFILES, governors
from .ec import ECError

# The two ways the EC can let us down that are expected on this hardware, as
# opposed to a bug: it did not answer, or it did not keep what we wrote. Both
# are transient here and both are handled the same way - count it, and if they
# keep coming, give the fans back. WriteRefused is deliberately not in this
# tuple: that one means a register outside the whitelist, which is a
# programming error and should arrive with a traceback.
EC_TROUBLE = (ECError, WriteLost)
from .registers import (
    REG_FAN1_APPLIED, REG_FAN1_TACH, REG_FAN2_APPLIED, REG_FAN2_TACH,
    FAN_TACH_MAX, percent_to_raw, raw_to_percent,
)
from .sensors import (
    ECTemperature, NvidiaTemperature, SensorSet, ThermalZoneTemperature,
)
from .state import AUTO, MAX, describe_profile, fixed_percent, normalise_profile

DEFAULT_CONFIG = {
    "profile": AUTO,
    "poll_seconds": 2.0,
    # Nothing is being written in auto, so there is no reason to sample as
    # often. This is mostly about not spawning nvidia-smi every two seconds
    # for the whole time the machine is idle.
    "auto_poll_seconds": 5.0,
    # Filled in by tools/find_temps.py. Null means "not found yet", and we fall
    # back to nvidia-smi for the GPU and the ACPI thermal zone for the CPU -
    # which works, but reacts late.
    "cpu_temp_register": None,
    "gpu_temp_register": None,
    "temp_scale": 1.0,
    "watchdog_seconds": 15.0,
    "ec_error_budget": 5,
    # After the error budget is spent: fans to the EC, wait this long, try the
    # requested profile again. The daemon exited instead; a service must not.
    "recovery_seconds": 60.0,
    # Neither fan drops below this fraction of the busier one. Both fans cool
    # both sides on this chassis, so a large split is wasted cooling.
    "cross_coupling": 0.75,
    # A sensor that has not moved at all for this long, while the other side
    # has, is not a sensor. See SensorSet.
    "stuck_sensor_seconds": 120.0,
    # Keep nvidia-smi as a second GPU source even though an EC register is
    # configured. Off by default: see _build_sensors.
    "nvidia_smi_fallback": False,
    # The four readout registers cost 12 EC transactions. The tooltip does not
    # need them more often than this many ticks, and this bus is contended
    # enough that the cheapest way to lose fewer reads is to make fewer.
    "telemetry_every": 3,
}


class Watchdog:
    """
    Releases the fans if the control loop stops ticking.

    The loop can wedge without raising - a driver call that never returns, a
    thread deadlock. From outside that looks identical to everything being
    fine, except the fans are frozen at whatever the last command was. This
    notices and undoes it.

    Unlike the daemon's version, firing is not terminal: a loop that recovers
    re-arms the watchdog and takes the fans back. A wedged loop never gets
    that far, which is exactly the distinction we want.
    """

    def __init__(self, timeout: float, on_stall, log: logging.Logger) -> None:
        self.timeout = timeout
        self.on_stall = on_stall
        self.log = log
        self._last_tick = time.monotonic()
        self._stop = threading.Event()
        self._fired = False
        self._armed = False
        self._thread = threading.Thread(
            target=self._run, name="aerofan-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def arm(self, armed: bool) -> None:
        """Only meaningful while we hold the fans. In auto there is nothing to release."""
        self._armed = armed
        self._last_tick = time.monotonic()

    def tick(self) -> None:
        self._last_tick = time.monotonic()
        self._fired = False

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            if self._fired or not self._armed:
                continue
            if time.monotonic() - self._last_tick > self.timeout:
                self._fired = True
                self.log.error("WATCHDOG: no tick in %.0fs - releasing the fans"
                               " to the EC", self.timeout)
                try:
                    self.on_stall()
                except Exception as exc:
                    self.log.error("watchdog release failed: %s", exc)


class Supervisor:
    """
    Owns the EC for the life of the process. One per process, always.

    Every EC access happens on the loop thread. ``request_profile`` and
    ``snapshot`` are the only things other threads touch, and both are just a
    lock around a small dict.
    """

    def __init__(self, controller: Controller, config: dict,
                 log: logging.Logger, dry_run: bool = False,
                 on_profile_change=None):
        self.controller = controller
        self.ec = controller.ec
        self.config = dict(DEFAULT_CONFIG, **(config or {}))
        self.log = log
        self.dry_run = dry_run
        # Called with the new profile name whenever the active profile changes,
        # so the service can persist it. Kept as a callback rather than an
        # import so the daemon can pass None and never write to state.json.
        self.on_profile_change = on_profile_change

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self.running = False

        self.requested = normalise_profile(self.config.get("profile") or AUTO)
        self.effective = AUTO
        self._pending: str | None = self.requested
        self._reapply = False

        self.cpu_governor = None
        self.gpu_governor = None
        self.sensors = self._build_sensors()

        self.applied: dict[int, float | None] = {1: None, 2: None}
        self.ec_errors = 0
        self.degraded_until = 0.0
        self._ticks = 0
        self._last_logged: tuple | None = None
        self._last_log_time = 0.0
        self._peak = {"cpu": 0.0, "gpu": 0.0}

        self._telemetry: dict = {
            "profile": self.requested,
            "effective": AUTO,
            "description": describe_profile(self.requested),
            "cpu_c": None, "gpu_c": None,
            "cpu_duty": None, "gpu_duty": None,
            "fan1_percent": None, "fan2_percent": None,
            "fan1_tach": None, "fan2_tach": None,
            "tach_max": FAN_TACH_MAX,
            "cpu_source": None, "gpu_source": None,
            "custom": False, "degraded": False, "dry_run": dry_run,
            "updated": None, "started": time.time(), "error": None,
        }

        self.watchdog = Watchdog(
            float(self.config["watchdog_seconds"]), self._release_quietly, log)

    # -- construction --------------------------------------------------------

    def _build_sensors(self) -> SensorSet:
        scale = self.config.get("temp_scale", 1.0)
        cpu_sources, gpu_sources = [], []
        if self.config.get("cpu_temp_register") is not None:
            cpu_sources.append(ECTemperature(
                self.ec, int(self.config["cpu_temp_register"]), scale, "cpu"))
        if self.config.get("gpu_temp_register") is not None:
            gpu_sources.append(ECTemperature(
                self.ec, int(self.config["gpu_temp_register"]), scale, "gpu"))
        # nvidia-smi only where there is nothing better. Once the EC register
        # is known it is strictly worse: it costs a process launch, it does not
        # work at all with the discrete GPU disabled, and - measured - the
        # first call after an idle spell takes the GPU from P8 at 5 W to P0 at
        # 25 W. Polling a GPU to ask its temperature is what keeps it awake.
        if self.config.get("gpu_temp_register") is None \
                or self.config.get("nvidia_smi_fallback"):
            gpu_sources.append(NvidiaTemperature())
        # The chassis zone lags badly, so it is last and only for the CPU side.
        cpu_sources.append(ThermalZoneTemperature())
        return SensorSet(cpu_sources, gpu_sources,
                         float(self.config.get("stuck_sensor_seconds", 120.0)))

    # -- public, callable from any thread ------------------------------------

    def request_profile(self, name: str) -> str:
        """
        Ask for a profile. Returns the canonical name.

        The switch itself happens on the loop thread, at the top of the next
        tick. Doing it here would mean two threads driving the EC mailbox, and
        we do not have the Access_EC mutant on this machine to arbitrate that.
        """
        canonical = normalise_profile(name)
        with self._lock:
            self.requested = canonical
            self._pending = canonical
            self.degraded_until = 0.0
            self.ec_errors = 0
            self._telemetry["profile"] = canonical
            self._telemetry["description"] = describe_profile(canonical)
        self._wake.set()  # do not wait out the poll interval for this
        if self.on_profile_change:
            try:
                self.on_profile_change(canonical)
            except Exception as exc:
                self.log.warning("could not persist profile: %s", exc)
        self.log.info("profile requested: %s", canonical)
        return canonical

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._telemetry)

    def stop(self) -> None:
        self.running = False
        self._wake.set()

    def note_suspend(self) -> None:
        """
        The machine is going to sleep.

        The EC resets across suspend and forgets custom mode, so there is
        nothing to preserve. Handing the fans back now means we resume into a
        known state rather than a half-remembered one.
        """
        self.log.info("suspending - releasing the fans")
        self._release_quietly()
        with self._lock:
            self._pending = self.requested

    def note_resume(self) -> None:
        self.log.info("resumed - re-applying %s", self.requested)
        with self._lock:
            self._pending = self.requested
            self._reapply = True
        self._wake.set()

    # -- engaging and releasing ----------------------------------------------

    def _release_quietly(self) -> None:
        if self.dry_run:
            return
        try:
            self.controller.release()
            self.applied = {1: None, 2: None}
        except Exception as exc:
            self.log.error("could not release the fans: %s", exc)

    def _engage(self, name: str, now: float) -> None:
        """Switch to `name`. Runs on the loop thread only."""
        if name == AUTO:
            self.cpu_governor = self.gpu_governor = None
            self._release_quietly()
            self.watchdog.arm(False)
            # Say so at once. We have just turned the switch off, so waiting
            # for the next telemetry tick to read it back would leave the tray
            # claiming we hold the fans for up to fifteen seconds after we
            # visibly gave them up.
            self._publish(custom=False, cpu_duty=None, gpu_duty=None)
            self.log.info("auto - fans returned to the EC's own curve")
            return

        fixed = fixed_percent(name)
        if name == MAX:
            fixed = 100.0

        if fixed is not None:
            self.cpu_governor = self.gpu_governor = None
            if not self.dry_run:
                self.controller.take_control(fixed)
            self.applied = {1: fixed, 2: fixed}
            self.watchdog.arm(True)
            self.log.info("%s - both fans at %.0f%%", name, fixed)
            return

        profile = PROFILES[name]
        self.cpu_governor, self.gpu_governor = governors(profile)

        # Take control at whatever the curve says right now rather than at
        # 100%. The daemon engaged at 100 and let the governor walk it down,
        # which is two seconds of full noise every single time you switch
        # profile. Sampling first costs one sensor read and removes that.
        start_cpu = start_gpu = 100.0
        try:
            sample = self.sensors.sample(now)
            if sample["any_ok"]:
                hottest = sample["hottest"]
                cpu_c = sample["cpu"].celsius if sample["cpu"].ok else hottest
                gpu_c = sample["gpu"].celsius if sample["gpu"].ok else hottest
                start_cpu = self.cpu_governor.update(cpu_c, now)
                start_gpu = self.gpu_governor.update(gpu_c, now)
        except ECError as exc:
            self.log.warning("could not sample before engaging (%s) - "
                             "starting at 100%%", exc)

        if not self.dry_run:
            self.controller.take_control(start_cpu, start_gpu)
        self.applied = {1: start_cpu, 2: start_gpu}
        self.watchdog.arm(True)
        self.log.info("%s - %s (starting at cpu %.0f%% / gpu %.0f%%)",
                      name, profile["description"], start_cpu, start_gpu)

    def _ensure_control(self) -> bool:
        """
        Confirm we still hold the fans, and take them back if not.

        After sleep/resume the EC resets and custom mode is simply gone. We
        would keep computing correct duties and writing them into a register
        nobody reads. Checking the switch rather than trusting our own last
        write is the only way to notice.

        A failed read of the switch is not an answer either way, and this
        machine drops reads regularly - 0x0D included. Throwing the tick away
        over one would cost us the duty write as well, which is the part that
        actually matters, so an unreadable switch means "assume nothing has
        changed" and we look again in two seconds.
        """
        if self.dry_run:
            return True
        try:
            held = self.controller.holds_control()
        except ECError as exc:
            self.log.debug("could not read the custom-mode switch (%s);"
                           " assuming it is unchanged", exc)
            return True
        if held and not self._reapply:
            return True
        self._reapply = False
        if not held:
            self.log.warning("custom mode is off (EC reset, or something else"
                             " took over) - re-engaging")
        self.controller.take_control(
            self.applied[1] if self.applied[1] is not None else 100,
            self.applied[2] if self.applied[2] is not None else 100)
        return True

    def _holds_control_safely(self) -> bool:
        """holds_control for the status display, where a failed read is not news."""
        if self.dry_run:
            return False
        try:
            return self.controller.holds_control()
        except ECError:
            return bool(self._telemetry.get("custom"))

    # -- the loop ------------------------------------------------------------

    def _read_temperatures(self, now: float) -> dict:
        sample = self.sensors.sample(now)
        if not sample["any_ok"]:
            raise ECError(
                f"no usable temperature (cpu: {sample['cpu'].source}, "
                f"gpu: {sample['gpu'].source})")
        hottest = sample["hottest"]
        return {
            "cpu_c": sample["cpu"].celsius if sample["cpu"].ok else hottest,
            "gpu_c": sample["gpu"].celsius if sample["gpu"].ok else hottest,
            "cpu_source": sample["cpu"].source,
            "gpu_source": sample["gpu"].source,
        }

    def _drive(self, temps: dict, now: float) -> tuple[float, float]:
        cpu_duty = self.cpu_governor.update(temps["cpu_c"], now)
        gpu_duty = self.gpu_governor.update(temps["gpu_c"], now)

        # One chassis, two fans, shared heatpipes: both fans move air over both
        # sides. So neither is allowed to loaf while the other is working hard.
        # Without this, a two-hour session ran the CPU fan at 45% with the GPU
        # fan at 98% - even with a correct CPU sensor that is the wrong split,
        # because the CPU fan is helping cool the GPU too.
        coupling = float(self.config.get("cross_coupling", 0.0))
        if coupling > 0:
            floor = max(cpu_duty, gpu_duty) * coupling
            cpu_duty = max(cpu_duty, floor)
            gpu_duty = max(gpu_duty, floor)

        for fan, duty in ((1, cpu_duty), (2, gpu_duty)):
            raw = percent_to_raw(duty)
            if self.applied[fan] is not None and \
                    percent_to_raw(self.applied[fan]) == raw:
                continue  # no change worth a mailbox transaction
            if not self.dry_run:
                self.controller.set_speed(duty, fan=fan)
            self.applied[fan] = duty
        return cpu_duty, gpu_duty

    def _read_fan_readouts(self) -> dict:
        """
        What the fans are actually doing, as opposed to what we asked for.

        0xB3/0xB4 are the applied duty - the EC's own decision in auto mode,
        a mirror of our command in custom mode - and 0xFC/0xFE are the real
        tachometers on a 0..23 scale. Together they are the honest answer for
        the tooltip whoever is in charge.

        BEST EFFORT, PER REGISTER, ON PURPOSE

        These reads fail from time to time - 0xB3 and 0xB4 noticeably more than
        the rest - with the EC simply not answering. The first version of this
        read all four in one expression, so one flaky read threw the whole tick
        away and spent a life out of the error budget. That budget exists to
        protect the fans from writes that are not landing; a tooltip that is
        four seconds stale is not the same problem and must not share it.

        So each register stands alone, and whatever is missing keeps its
        previous value.
        """
        readouts = (
            ("fan1_percent", REG_FAN1_APPLIED, raw_to_percent),
            ("fan2_percent", REG_FAN2_APPLIED, raw_to_percent),
            ("fan1_tach", REG_FAN1_TACH, int),
            ("fan2_tach", REG_FAN2_TACH, int),
        )
        values = {}
        for key, register, convert in readouts:
            try:
                values[key] = convert(self.ec.read_stable(register))
            except ECError as exc:
                self.log.debug("telemetry read of 0x%02X failed: %s",
                               register, exc)
        return values

    def _publish(self, **fields) -> None:
        with self._lock:
            self._telemetry.update(fields)
            self._telemetry["profile"] = self.requested
            self._telemetry["effective"] = self.effective
            self._telemetry["description"] = describe_profile(self.requested)
            self._telemetry["degraded"] = time.monotonic() < self.degraded_until
            self._telemetry["updated"] = time.time()

    def _log_activity(self, temps: dict, duties: tuple, now: float) -> None:
        """
        Log when the duty changes, and otherwise once a minute.

        An earlier version logged whenever the *line* changed, which on a GPU
        temperature wobbling between 76 and 77 C meant a line every two seconds
        for two hours with the duty pinned at 98.8% throughout. What matters is
        what the fans are doing, so that is what triggers a line; the heartbeat
        carries the peaks so a long quiet stretch is still legible afterwards.
        """
        self._peak["cpu"] = max(self._peak["cpu"], temps["cpu_c"])
        self._peak["gpu"] = max(self._peak["gpu"], temps["gpu_c"])

        key = (self.effective, round(duties[0], 1), round(duties[1], 1))
        heartbeat = now - self._last_log_time >= 60.0
        if key == self._last_logged and not heartbeat:
            return
        note = ""
        if key == self._last_logged:
            note = (f"   [60s peak: cpu {self._peak['cpu']:.0f}C "
                    f"gpu {self._peak['gpu']:.0f}C]")
            self._peak = {"cpu": temps["cpu_c"], "gpu": temps["gpu_c"]}
        self.log.info("cpu %5.1fC -> %5.1f%%   gpu %5.1fC -> %5.1f%%%s",
                      temps["cpu_c"], duties[0], temps["gpu_c"], duties[1], note)
        self._last_logged = key
        self._last_log_time = now

    def _tick(self, now: float) -> None:
        with self._lock:
            pending = self._pending
        if pending is not None:
            # Clear the request only once it has actually been applied. An
            # earlier version took it off the queue first, so a switch whose
            # take_control lost a write was dropped silently: the status said
            # aggressive, the fans stayed on the EC, and nothing retried.
            if pending != self.effective:
                self._engage(pending, now)
                self.effective = pending
                self._last_logged = None
                # Force a readout refresh on the tick that follows a switch:
                # _ticks becomes 1 below, and 1 always reads. Otherwise the
                # tooltip shows the old profile's fan speeds until the normal
                # telemetry interval comes round.
                self._ticks = 0
            with self._lock:
                if self._pending == pending:
                    self._pending = None

        self._ticks += 1
        temps = self._read_temperatures(now)

        if self.effective == AUTO:
            duties = (None, None)
        else:
            self._ensure_control()
            if self.cpu_governor is not None:
                duties = self._drive(temps, now)
            else:
                duties = (self.applied[1], self.applied[2])
                # A fixed profile still has to be defended: the EC can take the
                # fans back, and _ensure_control re-writes our last duty.
            self._log_activity(temps, duties, now)

        fields = dict(temps)
        fields["cpu_duty"], fields["gpu_duty"] = duties
        if self.effective != AUTO:
            # _ensure_control has just confirmed it, or taken it back. No
            # second read needed for something we already know this tick.
            fields["custom"] = not self.dry_run
        every = max(1, int(self.config.get("telemetry_every", 2)))
        if self._ticks % every == 0 or self._ticks == 1:
            fields.update(self._read_fan_readouts())
            if self.effective == AUTO:
                # In auto we are not touching it, so it is worth checking
                # whether something else has.
                fields["custom"] = self._holds_control_safely()
        fields["error"] = None
        self._publish(**fields)

    def _degrade(self, reason: str) -> None:
        recovery = float(self.config.get("recovery_seconds", 60.0))
        self.log.error("too many consecutive EC failures (%s) - fans back to"
                       " the EC, retrying %s in %.0fs",
                       reason, self.requested, recovery)
        self._release_quietly()
        self.watchdog.arm(False)
        self.effective = AUTO
        self.ec_errors = 0
        self.degraded_until = time.monotonic() + recovery
        self._publish(error=reason, custom=False,
                      cpu_duty=None, gpu_duty=None)

    def run(self) -> int:
        budget = int(self.config["ec_error_budget"])
        self.running = True
        self.watchdog.start()
        self.log.info("supervisor starting - remembered profile is %s",
                      self.requested)
        if self.dry_run:
            self.log.info("DRY RUN - nothing will be written")
        if self.config.get("cpu_temp_register") is None:
            self.log.info("no CPU temperature register configured; the CPU side"
                          " falls back to the ACPI chassis zone, which lags"
                          " real load. Run tools/find_temps.py.")

        try:
            while self.running:
                now = time.monotonic()

                if self.degraded_until and now >= self.degraded_until:
                    self.degraded_until = 0.0
                    with self._lock:
                        self._pending = self.requested
                    self.log.info("recovery window over - retrying %s",
                                  self.requested)

                try:
                    if not self.degraded_until:
                        self._tick(now)
                        self.ec_errors = 0
                        self.watchdog.tick()
                except EC_TROUBLE as exc:
                    self.ec_errors += 1
                    self.log.warning("EC trouble (%d/%d): %s",
                                     self.ec_errors, budget, exc)
                    self._publish(error=str(exc))
                    if self.ec_errors >= budget:
                        self._degrade(str(exc))
                except Exception as exc:  # a bug here must not stop the loop
                    self.ec_errors += 1
                    self.log.exception("unexpected error in tick: %s", exc)
                    if self.ec_errors >= budget:
                        self._degrade(str(exc))

                interval = float(
                    self.config["auto_poll_seconds"] if self.effective == AUTO
                    else self.config["poll_seconds"])
                self._wake.wait(interval)
                self._wake.clear()
            return 0
        finally:
            self.watchdog.stop()
            self.log.info("supervisor stopping - fans back to the EC")
            self._release_quietly()
            self.effective = AUTO
            self._publish(custom=False, cpu_duty=None, gpu_duty=None)
