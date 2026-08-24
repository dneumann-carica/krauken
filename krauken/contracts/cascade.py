"""The beer-temp cascade: beer temp + beer target -> a chamber target
temperature, via a PI controller on beer-temp error alone (no gravity
dependency -- deliberate: a fermentation isn't guaranteed to always have a
gravity source mapped). This is genuinely shared, not just conceptually
similar code living in two places -- the offline demo generator
(platforms/simulator/plant.py) and the real control loop
(daemon/control_loop.py) both call these exact functions, so the demo
batch's chamber behavior is provably the same rule the real daemon runs,
not a separately-invented approximation of it.

Replaced an earlier fixed-clamp bang-bang design (fire a hard chamber
offset once |beer - target| crossed a trigger band, release it entirely
back to zero once beer crossed back through target). That could correct a
one-time deviation but had no way to counter a SUSTAINED one-directional
disturbance -- an actively fermenting beer's own exothermic heat -- except
by re-triggering over and over, releasing all the way back to no offset in
between each time: exactly the "long idle then a spike" oscillation a real
fermentation run showed. A PI controller holds a smooth, continuously-
graded offset instead, so a disturbance that never lets the error clear on
its own still gets fully countered eventually (via the integral term)
without ever needing to fully release. See control_constants.py's
BEER_KP_F_PER_F/BEER_KI_F_PER_F_H/INTEGRAL_MAX_F_H for the gain values and
how they're grounded against the simulator's own documented exotherm.

No chamber-side deadband is added here on purpose, even though a
continuous P term will produce continuous small target wiggles as beer
temp noise moves: the Hardware Supervisor's own actuator-level
CHAMBER_DEADBAND_F thermostat already absorbs exactly this (that's the
correct layer for it per this module's own docstring below -- relay
timing/chatter protection belongs to whatever is actually driving the
chamber, never here).

This module is daemon/beer-side only. It has no notion of relay timing
(min-on/min-off/lockout) -- that protection logic belongs to whatever is
actually driving the chamber (a real Hardware Supervisor on real hardware;
contracts/protection.py's state machine for the Simulator driver today),
per the architecture split in control_constants.py's docstring: the daemon
computes what chamber temperature it WANTS, never how a relay gets there.
"""
from __future__ import annotations

from krauken.contracts.control_constants import (
    BEER_KI_F_PER_F_H,
    BEER_KP_F_PER_F,
    CHAMBER_TARGET_MAX_F,
    CHAMBER_TARGET_MIN_F,
    INTEGRAL_MAX_F_H,
    RAMP_FEEDFORWARD_COUPLING_PER_H,
)


def update_beer_error_integral(
    beer_temp_f: float, beer_target_f: float, current_integral_f_h: float, dt_h: float,
) -> float:
    """Accumulates beer-target error (beer_target_f - beer_temp_f) over
    the dt_h just elapsed, clamped to +/-INTEGRAL_MAX_F_H -- anti-windup,
    so a prolonged real deviation (a stuck sensor, an authored stage
    that's simply wrong) can't let this grow without bound and cause a
    dangerous overshoot once whatever caused it finally clears. Caller is
    responsible for calling this once per tick with THAT tick's own dt_h
    (hours since the previous tick) before calling chamber_target_for()
    with the result -- the two are split so each is independently testable
    and neither silently duplicates the other's clamp logic.

    dt_h should reflect real elapsed time via a monotonic clock, never
    wall-clock time (which can step on an NTP correction) -- see
    contracts/clock.py's Clock.now() vs Clock.monotonic() docstrings."""
    error = beer_target_f - beer_temp_f
    new_integral = current_integral_f_h + error * dt_h
    return max(-INTEGRAL_MAX_F_H, min(INTEGRAL_MAX_F_H, new_integral))


def chamber_target_for(
    beer_temp_f: float, beer_target_f: float, beer_error_integral_f_h: float, ramp_rate_f_per_h: float = 0.0,
) -> float:
    """PI on beer-temp error (error = beer_target_f - beer_temp_f),
    beer_error_integral_f_h already updated for this tick by
    update_beer_error_integral() -- this function itself does no
    accumulation, so it's a pure function of the current inputs and safe
    to call as many times as needed (e.g. the chart's forward-projection
    preview) without side effects.

    ramp_rate_f_per_h is the beer's OWN authored target's current rate of
    change (contracts.stages.target_rate_f_per_h) -- zero for a held
    setpoint, nonzero (signed: negative while ramping down, e.g. a cold
    crash) while a stepped stage is actively ramping. Reacting only to
    PRESENT error isn't enough once the target itself keeps moving out
    from under the chamber: the beer would settle into a permanent lag
    behind the ramp instead of ever closing the gap (confirmed against
    real cold-crash sample data from the old cascade -- the beer/target
    gap grew smoothly and never closed). Adding rate/
    RAMP_FEEDFORWARD_COUPLING_PER_H directly (same sign as the rate
    itself) cancels that steady-state lag for whatever rate the stage is
    actually ramping at, on top of whatever the PI terms are separately
    doing about present error.

    Clamped to the absolute safety envelope since a large combination of
    error, accumulated integral, and ramp feedforward -- or a badly-
    authored stage (a huge ramp over very few hours) -- could otherwise
    ask for a chamber target no real equipment could or should attempt."""
    error = beer_target_f - beer_temp_f
    ramp_feedforward_f = ramp_rate_f_per_h / RAMP_FEEDFORWARD_COUPLING_PER_H
    target = (
        beer_target_f
        + BEER_KP_F_PER_F * error
        + BEER_KI_F_PER_F_H * beer_error_integral_f_h
        + ramp_feedforward_f
    )
    return max(CHAMBER_TARGET_MIN_F, min(CHAMBER_TARGET_MAX_F, target))
