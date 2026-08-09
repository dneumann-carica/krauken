"""The live SimPlant driver: implements ChamberDriver/BeerTempSource/
GravitySource against the same plant model that generates the demo batch
(platforms/simulator/plant.py), so the control loop's M2 tests run against
real thermal/gravity physics instead of a hand-fed fixture.

One SimPlantEngine per daemon process, shared by however many of the two
Simulator DeviceCandidates (simulator:chamber, simulator:tilt) got mapped to
a role -- they're one conceptual batch's readings, not two independent
simulations that could drift apart from each other.

Ticking is lazy and self-contained, same as before -- but INDEPENDENT per
role. chamber/beer/gravity each track their own last-read monotonic time
and advance their own physics using their own elapsed dt, not one shared
tick that every read call re-triggers. That matters: with a single shared
tick (the old design), calling read_beer() then read_chamber() then
read_gravity() in the same control-loop pass -- which is exactly what
daemon/control_loop.py does every tick -- fired the physics/relay-
protection step three times per pass, not once, since each call saw
whatever tiny sliver of wall-clock time had elapsed since the read
immediately before it. Only the first of the three carried real elapsed
time; the other two were near-no-op reruns. Tracking each role's own dt
independently means a read only ever advances that role's own physics,
exactly once, regardless of what else got read in between -- and it means
chamber, beer, and gravity can genuinely be read on different cadences
without one starving or double-counting another's tick.

Relay-timing protection (contracts.protection) lives HERE, not in the
daemon's cascade -- this driver is standing in for where a real Hardware
Supervisor would enforce it on real hardware. set_target() only ever
receives a target *temperature*; engage/release direction is inferred from
which way that target sits relative to the current chamber temp, exactly
as a real thermostat-driven relay would decide it. next_relay_state() has
exactly one call site now: read_chamber()'s own tick, using chamber's own
dt -- beer and gravity have no relay to protect.

Gravity is the odd one out: plant.gravity_at() is a pure function of
absolute elapsed simulated hours, not an incremental integration step, so
it needs no dt-tracking at all -- read_gravity() just evaluates it fresh
against however much time has passed since this engine was constructed,
plus a small re-rolled-every-read jitter (plant.py's GravityParams.jitter_sg).
"""
from __future__ import annotations

import dataclasses
import random

from krauken.contracts.clock import Clock
from krauken.contracts.control_constants import ControlTuning
from krauken.contracts.models import BeerReading, ChamberMode, ChamberReading, GravityReading, Health
from krauken.contracts.protection import RelayState, next_relay_state, thermostat_demand
from krauken.platforms.simulator import plant

_MODE_MAP = {"idle": ChamberMode.IDLE, "cool": ChamberMode.COOL, "heat": ChamberMode.HEAT}

# The same 5 presets offered by the Hardware Setup wizard's chamber-location
# step (see HardwareWizard.tsx's LOCATION_PRESETS) -- picking one there
# changes what "ambient" means for the SimPlant engine's idle-chamber
# drift, so the location a user picks actually shows up in the simulated
# batch's behavior. Values are illustrative, not measured: a garage swings
# further and colder than a closet, a kitchen runs warm and stable, etc.
# Manual-driver-only setups have no ambient concept at all (operator-
# settable, no physics -- see platforms/manual/live.py), so this is
# Simulator-only by nature, not an oversight.
AMBIENT_PRESETS: dict[str, plant.AmbientParams] = {
    "Garage": plant.AmbientParams(base_f=62.0, seasonal_drop_f=14.0, diurnal_amplitude_f=4.0),
    "Basement": plant.AmbientParams(base_f=64.0, seasonal_drop_f=4.0, diurnal_amplitude_f=0.8),
    "Closet": plant.AmbientParams(base_f=70.0, seasonal_drop_f=3.0, diurnal_amplitude_f=1.0),
    "Kitchen": plant.AmbientParams(base_f=72.0, seasonal_drop_f=2.0, diurnal_amplitude_f=1.2),
    "Spare room": plant.AmbientParams(base_f=70.0, seasonal_drop_f=5.0, diurnal_amplitude_f=1.5),
}


class SimPlantEngine:
    def __init__(
        self,
        clock: Clock,
        params: plant.PlantParams | None = None,
        tuning: ControlTuning | None = None,
    ):
        self.clock = clock
        self.params = params or plant.PlantParams()
        self.tuning = tuning or ControlTuning()
        # Models a 1-probe vs. 2-probe rig, purely for the guided wizard's
        # two-probe identify test (daemon/tests_runtime.py) -- both probes
        # start together (as if both were sitting in the chamber before
        # being told apart) and the second one is nudged independently via
        # the dev panel to simulate warming it in hand. NOT reset by
        # reset_for_new_batch() -- this is a rig-wiring fact, not a batch
        # one.
        self.probe2_enabled = False
        self.probe2_temp_f: float | None = None
        self.reset_for_new_batch()

    def reset_for_new_batch(self) -> None:
        """Re-anchors the whole plant to "now" and puts chamber/beer/relay
        back to their starting values -- called by daemon/fermentation.py's
        start_fermentation() every time a NEW fermentation begins.

        Without this, the plant's absolute-time reference (_start_mono,
        which both beer's exotherm term and gravity's curve are evaluated
        against) stays anchored to whenever this ENGINE was constructed --
        effectively daemon-process-startup time, not fermentation-start
        time. Since the control loop's own tick loop
        (daemon/app.py's _control_loop) calls clock.sleep() every pass
        regardless of whether a fermentation is active, any ticks that
        happen before a fermentation starts (during hardware scan/mapping,
        or just an idle daemon sitting between batches) silently advance
        gravity/exotherm's curve ahead of "hour zero" -- a real bug this
        surfaced: gravity had already dropped measurably before a
        fermentation even began, making a gravity-gated stage look like it
        finished "too early" relative to its own elapsed-hours bookkeeping,
        because the two were reading from different clocks-since-zero.
        Cheap and idempotent to call any time; harmless if Simulator isn't
        even the mapped platform (same precedent as set_ambient_location)."""
        self._start_mono = self.clock.monotonic()
        self.chamber_temp_f = self.params.chamber_start_f
        self.beer_temp_f = self.params.beer_start_f
        self.relay = RelayState()
        self._chamber_target_f: float | None = None
        self._last_commanded_target_f: float | None = None
        # Independent per-role tick trackers -- see module docstring. No
        # tracker for gravity: it's a pure function of absolute elapsed
        # time (_t_h()), not an incremental step, so there's no "dt since
        # last read" for it to need.
        self._chamber_last_mono = self.clock.monotonic()
        self._beer_last_mono = self.clock.monotonic()

    def set_probe2_enabled(self, enabled: bool) -> None:
        self.probe2_enabled = enabled
        if enabled and self.probe2_temp_f is None:
            self.probe2_temp_f = self.chamber_temp_f

    def set_probe2_temp(self, temp_f: float | None) -> None:
        self.probe2_temp_f = temp_f

    def set_chamber_target(self, temp_f: float | None) -> None:
        self._chamber_target_f = temp_f
        if temp_f is not None:
            self._last_commanded_target_f = temp_f

    def set_ambient_location(self, location: str | None) -> None:
        """Called every control tick with whatever the chamber_location
        setting currently is (see daemon/control_loop.py) -- cheap and
        idempotent, so there's no need to track "did this change" here."""
        ambient = AMBIENT_PRESETS.get(location) if location else None
        if ambient is None:
            ambient = plant.AmbientParams()  # unset/unknown location -- the original generic default
        if ambient != self.params.ambient:
            self.params = dataclasses.replace(self.params, ambient=ambient)

    def _t_h(self) -> float:
        """Absolute elapsed simulated hours since this engine was
        constructed -- computed fresh from the clock every call, not
        accumulated, since it's needed by both beer's exotherm term and
        gravity's pure curve evaluation (see module docstring)."""
        return (self.clock.monotonic() - self._start_mono) / 3600.0

    def read_chamber(self) -> ChamberReading:
        now_mono = self.clock.monotonic()
        dt_s = now_mono - self._chamber_last_mono
        self._chamber_last_mono = now_mono
        if dt_s > 0:
            demand = (
                "idle"
                if self._chamber_target_f is None
                else thermostat_demand(self.chamber_temp_f, self._chamber_target_f, self.tuning.chamber_deadband_f)
            )
            self.relay = next_relay_state(self.relay, demand, dt_s, self.tuning)

            if self.relay.mode == "idle" or self._last_commanded_target_f is None:
                drive_to = plant.ambient_f(self.params.ambient, self._t_h(), self.params.total_hours)
                coeff = self.params.chamber_idle_leak_coeff
            else:
                drive_to = self._last_commanded_target_f
                coeff = self.params.heat_transfer_coeff

            dt_h = dt_s / 3600.0
            self.chamber_temp_f = plant.advance_chamber_temp(self.chamber_temp_f, self.params, dt_h, drive_to, coeff)

        return ChamberReading(
            temp_f=self.chamber_temp_f,
            mode=_MODE_MAP[self.relay.mode],
            health=Health.OK,
            last_good_ts=self.clock.now(),
            commanded_target_f=self._chamber_target_f,
        )

    def read_beer(self) -> BeerReading:
        now_mono = self.clock.monotonic()
        dt_s = now_mono - self._beer_last_mono
        self._beer_last_mono = now_mono
        if dt_s > 0:
            dt_h = dt_s / 3600.0
            self.beer_temp_f = plant.advance_beer_temp(
                self.beer_temp_f, self.chamber_temp_f, self.params, dt_h, self._t_h()
            )
        return BeerReading(temp_f=self.beer_temp_f, health=Health.OK, last_good_ts=self.clock.now())

    def read_gravity(self) -> GravityReading:
        t_h = self._t_h()
        gravity_sg = plant.gravity_at(self.params.gravity, t_h)
        g = self.params.gravity
        if t_h < g.settling_duration_h and g.settling_jitter_sg > g.jitter_sg:
            # Hydrometer/Tilt still bobbing, beer still sloshing right
            # after transfer -- a bigger, progressively-calming-down
            # noise than the gentle steady-state jitter, not a step
            # function, so OG-detection (contracts/og_detection.py) has
            # something realistic to actually wait out.
            frac = t_h / g.settling_duration_h
            jitter = g.settling_jitter_sg * (1 - frac) + g.jitter_sg * frac
        else:
            jitter = g.jitter_sg
        if jitter > 0:
            gravity_sg += random.uniform(-jitter, jitter)
        return GravityReading(gravity_sg=gravity_sg, health=Health.OK, last_good_ts=self.clock.now())


class SimChamberDriver:
    def __init__(self, engine: SimPlantEngine):
        self._engine = engine

    async def read_chamber(self) -> ChamberReading:
        return self._engine.read_chamber()

    async def set_target(self, temp_f: float | None) -> None:
        self._engine.set_chamber_target(temp_f)


class SimBeerTempSource:
    def __init__(self, engine: SimPlantEngine):
        self._engine = engine

    async def read(self) -> BeerReading:
        return self._engine.read_beer()


class SimGravitySource:
    def __init__(self, engine: SimPlantEngine):
        self._engine = engine

    async def read(self) -> GravityReading:
        return self._engine.read_gravity()
