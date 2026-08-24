"""A coupled thermal + gravity plant model. Used by the offline demo-batch
generator (db/seed.py) and by the live SimPlant driver
(platforms/simulator/live.py) that backs M2's real control loop in dev/test
environments with no real hardware.

The chamber-control decision inside step() calls contracts.cascade directly
-- the same functions the real daemon control loop calls -- so this demo
data and the live driver's own behavior are both an honest exercise of the
real cascade rule, not a separately-invented approximation of it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from krauken.contracts.cascade import beer_relay_demand, chamber_target_for


@dataclass(frozen=True, slots=True)
class AmbientParams:
    base_f: float = 74.0
    seasonal_drop_f: float = 7.0  # total linear drift over the run's full duration
    diurnal_amplitude_f: float = 1.6


@dataclass(frozen=True, slots=True)
class ExothermParams:
    peak_f_per_h: float = 0.22
    peak_at_h: float = 30.0
    width_h: float = 26.0


@dataclass(frozen=True, slots=True)
class GravityParams:
    og: float = 1.052
    terminal: float = 1.005
    midpoint_h: float = 60.0
    # Tuned so the curve is genuinely flat by the time a stage's gravity
    # gate is satisfied, not just "below threshold while still visibly
    # sloping": at a given gravity_hi crossing, the remaining gap to
    # terminal decays as roughly exp(-elapsed_h/steepness_h) afterward, so
    # a smaller steepness_h means more of that remaining drop completes
    # DURING the stability window rather than continuing to fall visibly
    # through it (and into the next stage). At 6.0, ~98% of the
    # post-crossing drop completes within a 24h stability window (was
    # 14.0 -- only ~82% -- which read as "still falling" through the
    # transition and into Diacetyl rest).
    steepness_h: float = 6.0
    # Live-driver-only -- see live.py's read_gravity(). gravity_at() itself
    # stays a pure, deterministic function (the offline demo generator in
    # db/seed.py wants that); jitter is layered on top only at the live
    # read site, freshly re-rolled every read, never accumulated. Reduced
    # from 0.0008 -- that read as visibly noisy on the chart.
    jitter_sg: float = 0.0002
    # Live-driver-only, same as jitter_sg -- but much larger, and only for
    # the first settling_duration_h after the batch starts (see live.py's
    # read_gravity()): models the hydrometer/Tilt still bobbing and the
    # beer still sloshing right after it's racked into the fermenter,
    # which is a genuinely bigger and different kind of noise than the
    # gentle steady-state jitter above. Decays linearly down to jitter_sg
    # over that window rather than stepping abruptly, so OG-detection
    # (contracts/og_detection.py) has something realistic to actually wait
    # out.
    settling_jitter_sg: float = 0.006
    settling_duration_h: float = 0.25


@dataclass(frozen=True, slots=True)
class PlantParams:
    beer_start_f: float = 68.0
    chamber_start_f: float = 69.0
    # Two DIFFERENT rates, not one -- a real compressor's active cooling/
    # heating capacity vastly exceeds the chamber's passive heat exchange
    # with the room, which is exactly why a thermostat-driven compressor
    # can hold a narrow band at a LOW duty cycle instead of running most
    # of the time. A single shared coefficient (the old design) made both
    # directions equally fast, so an idle chamber drifted back up toward
    # the target almost as quickly as an engaged compressor pulled it back
    # down -- the relay ended up cooling 60-65% of the time even at a mild
    # setpoint, with visible sawtooth "spikes" every idle window (project
    # owner's report). Splitting the rate fixes both symptoms at once.
    heat_transfer_coeff: float = 6.0  # per hour, ACTIVE only -- chamber temp -> whatever the
    # cascade just commanded (drive_to), while the relay is actually cooling/heating. Still
    # comfortably within "an hour or 2 at most" for a newly commanded target (project owner's
    # call): even the biggest single-hop swing in the full profile (diacetyl-rest's 72F hold ->
    # cold crash's 34F, a ~43F commanded step) settles in well under an hour at this rate.
    chamber_idle_leak_coeff: float = 0.20  # per hour, IDLE only -- chamber temp -> ambient,
    # passive-insulation speed (a closed, unpowered fridge box reaching room temp over many
    # hours, not 1.5-2h). Deliberately far slower than heat_transfer_coeff -- a real compressor's
    # active capacity vastly exceeds the chamber's passive heat exchange with the room, which is
    # exactly why a thermostat-driven compressor can hold a narrow band at a LOW duty cycle
    # instead of running most of the time.
    #
    # These two numbers (6.0 / 0.20), together with cascade.py's chamber_target_for() now
    # holding the chamber gently AT the beer target during idle rather than fully de-energizing
    # (see that function's docstring), were swept against a standalone bang-bang simulation of
    # this exact relay+physics loop in isolation (no exotherm/diurnal forcing) to land near the
    # project owner's stated cold-crash expectation from a real run: ~60% cooling duty at a
    # cold-crash setpoint (chamber ambient gap ~40F) vs. ~25% at a mild fermentation setpoint
    # (gap ~6F) -- the SAME global constants naturally produce both, because duty cycle scales
    # with how far the setpoint sits below ambient, not a per-stage special case. A real Primary
    # run comes out somewhat above that ~25% isolated baseline (closer to 40-45%) because
    # exotherm and the ambient's own diurnal swing add genuine extra cool cycles on top of it --
    # real yeast-heat load the chamber does actually need to manage, not a tuning miss.
    beer_chamber_coupling: float = 0.05  # per hour: beer temp -> chamber temp
    total_hours: float = 456.0
    ambient: AmbientParams = field(default_factory=AmbientParams)
    exotherm: ExothermParams = field(default_factory=ExothermParams)
    gravity: GravityParams = field(default_factory=GravityParams)


@dataclass(frozen=True, slots=True)
class PlantState:
    t_h: float
    beer_temp_f: float
    chamber_temp_f: float
    gravity: float
    mode: str  # cool | heat | idle
    # The chamber temperature this step was actually driven toward -- None
    # on an initial/starting state (nothing has been driven toward yet).
    # Set by advance_physics() from its own drive_to argument, so this is
    # correct for BOTH of its callers: step()'s own chamber_target_for()
    # value (the offline chart-projection path this was added for -- see
    # projection.py) and the live SimPlant driver's protection-adjusted
    # value (platforms/simulator/live.py) -- both are equally "the target
    # this step drove toward," just computed differently upstream.
    chamber_target_f: float | None = None


def ambient_f(p: AmbientParams, t_h: float, total_h: float) -> float:
    seasonal = -p.seasonal_drop_f * (t_h / total_h)
    diurnal = p.diurnal_amplitude_f * math.sin(2 * math.pi * t_h / 24.0)
    return p.base_f + seasonal + diurnal


def exotherm_f_per_h(p: ExothermParams, t_h: float) -> float:
    return p.peak_f_per_h * math.exp(-((t_h - p.peak_at_h) ** 2) / (2 * p.width_h**2))


def gravity_at(p: GravityParams, t_h: float) -> float:
    """Logistic decay from og to terminal, centered at midpoint_h. Unlike
    every other math.exp() call in this module (advance_chamber_temp's
    exact-solution decay, exotherm_f_per_h's Gaussian), whose exponents
    are unconditionally <= 0 by construction and so only ever safely
    underflow toward 0 for extreme inputs (see advance_physics's own
    docstring on the real ~1e200F incident that motivated that pattern),
    a plain logistic's exponent is unbounded in BOTH directions -- large
    positive t_h genuinely does need math.exp() of a large POSITIVE
    number, which overflows. A real, if exceptional, control-loop bug
    (a runaway/never-waiting SimulatorClock racing t_h far past any
    realistic fermentation length after an unrelated failure blocked the
    stage-completion check that would normally have stopped ticking)
    reached exactly this case. Standard stable-sigmoid rewrite: whichever
    side of the midpoint t_h is on, only ever exponentiate the
    non-positive half."""
    x = (t_h - p.midpoint_h) / p.steepness_h
    if x >= 0:
        e = math.exp(-x)
        fraction = e / (1 + e)
    else:
        fraction = 1 / (1 + math.exp(x))
    return p.terminal + (p.og - p.terminal) * fraction


def initial_state(p: PlantParams) -> PlantState:
    return PlantState(
        t_h=0.0,
        beer_temp_f=p.beer_start_f,
        chamber_temp_f=p.chamber_start_f,
        gravity=p.gravity.og,
        mode="idle",
    )


def advance_physics(state: PlantState, p: PlantParams, dt_h: float, drive_to: float, mode: str) -> PlantState:
    """The thermal + gravity integration only -- no cascade decision. Takes
    whatever chamber temperature the caller has already decided to drive
    toward (a clamp-derived target while actively cooling/heating, or the
    ambient temperature while idle) and `mode` purely for bookkeeping on
    the returned state. Shared by step() below (which makes its own cascade
    decision, for the offline demo generator) and the live SimPlant driver
    (platforms/simulator/live.py), which makes ITS decision via
    contracts.protection instead, since a live driver has real relay-timing
    protection to respect that the offline generator doesn't model.

    Chamber uses the EXACT solution of dT/dt = k*(drive_to - T) --
    drive_to + (T0-drive_to)*exp(-k*dt_h) -- not a forward-Euler step,
    because this same function also backs projection.py's (this package's
    sibling module) forward preview, which steps in much coarser 0.5h increments than the
    live driver's per-tick calls. A forward-Euler step is only stable
    while k*dt_h stays comfortably below 1; once heat_transfer_coeff was
    tuned up for the relay's own duty-cycle physics (see PlantParams),
    0.5h*that coefficient pushed well past the Euler-instability
    threshold, and a naive Euler step diverges exponentially over
    repeated iterations -- exactly what produced a real ~1e200F
    projected temperature that crashed the chart's tick-generation loop
    in production. The exact exponential form is unconditionally stable
    for ANY step size and coefficient, so this can never recur regardless
    of future retuning or projection-horizon choices. Beer's own coupling
    coefficient is two orders of magnitude smaller and in no such danger,
    so it keeps its plain Euler step below (exact would cost real
    complexity here for no practical benefit)."""
    new_chamber = drive_to + (state.chamber_temp_f - drive_to) * math.exp(-p.heat_transfer_coeff * dt_h)
    exo = exotherm_f_per_h(p.exotherm, state.t_h)
    new_beer = (
        state.beer_temp_f
        + p.beer_chamber_coupling * (new_chamber - state.beer_temp_f) * dt_h
        + exo * dt_h
    )
    new_gravity = gravity_at(p.gravity, state.t_h + dt_h)

    return PlantState(
        t_h=state.t_h + dt_h, beer_temp_f=new_beer, chamber_temp_f=new_chamber, gravity=new_gravity, mode=mode,
        chamber_target_f=drive_to,
    )


def step(
    state: PlantState, p: PlantParams, dt_h: float, beer_target_f: float, ramp_rate_f_per_h: float = 0.0
) -> PlantState:
    """Convenience entry point for the chart's forward-projection preview
    (projection.py, this package's sibling module): makes its own cascade decision (no relay-
    timing protection -- see advance_physics's docstring) and then
    integrates. chamber_target_for() always returns a real target now,
    never None (see its own docstring -- idle governs gently at the beer
    target rather than de-energizing), so there's no ambient-fallback
    branch to pick between here anymore.

    ramp_rate_f_per_h passes through to chamber_target_for's feedforward --
    the caller supplies contracts.stages.target_rate_f_per_h(stage, t) so
    a projected cold-crash-style ramp gets the same more-aggressive chamber
    push the real control loop now applies, not a stale fixed-clamp preview
    of a lag the daemon no longer actually produces."""
    mode = beer_relay_demand(state.beer_temp_f, beer_target_f, state.mode)
    drive_to = chamber_target_for(mode, beer_target_f, ramp_rate_f_per_h)
    return advance_physics(state, p, dt_h, drive_to, mode)


# --- Independent per-role stepping, for the live SimPlantEngine ---------
#
# The functions above (PlantState/advance_physics/step) stay exactly as
# they were: db/seed.py's offline generator advances chamber, beer, and
# gravity together in lockstep with one shared dt, and that's still the
# right model for a one-shot batch of historical data with no real clock.
#
# The live driver (platforms/simulator/live.py) is different: chamber,
# beer, and gravity each get read (and therefore ticked) independently, on
# their own cadence, so each needs its own dt against its own last-read
# time -- see live.py's module docstring for why. These functions are the
# same underlying formulas as advance_physics(), just callable one role at
# a time instead of requiring one shared PlantState mutation covering all
# three every time any single one of them is read.


def advance_chamber_temp(chamber_temp_f: float, p: PlantParams, dt_h: float, drive_to: float, coeff: float) -> float:
    """One independent step for the chamber's own thermal state -- the
    exact solution of dT/dt = coeff*(drive_to - T), same reasoning as
    advance_physics's chamber term (see its docstring): unconditionally
    stable for any dt_h/coeff combination, not just ones small enough for
    a forward-Euler step to stay convergent. `coeff` is the caller's
    choice of p.heat_transfer_coeff (relay actively cooling/heating) or
    p.chamber_idle_leak_coeff (relay idle, drifting toward ambient) --
    see PlantParams' docstring comments for why those two rates are
    deliberately different, not the same knob reused."""
    return drive_to + (chamber_temp_f - drive_to) * math.exp(-coeff * dt_h)


def advance_beer_temp(
    beer_temp_f: float, current_chamber_temp_f: float, p: PlantParams, dt_h: float, t_h: float
) -> float:
    """One independent Euler step for beer's own thermal state. Reads
    whatever the chamber's CURRENT temp happens to be for the coupling
    term -- a read-only reference; this call never advances chamber's own
    state. A slight lag if the two end up read on different cadences is
    physically realistic (this is exactly how a real probe pair would
    behave), not a defect."""
    exo = exotherm_f_per_h(p.exotherm, t_h)
    return beer_temp_f + p.beer_chamber_coupling * (current_chamber_temp_f - beer_temp_f) * dt_h + exo * dt_h
