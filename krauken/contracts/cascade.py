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

The algorithm is deliberately ramp-agnostic: chamber_target_for() takes no
ramp-rate input at all, constant-target stage or ramping one alike. An
earlier version of this PI rewrite added a separate ramp-feedforward term
(rate/RAMP_FEEDFORWARD_COUPLING_PER_H, carried over from the old bang-bang
cascade, which had no integral state at all and genuinely needed a manual
substitute for one). That's redundant with a real PI controller, and
confirmed live to be actively harmful: standard control theory says a
PI's own integral term settles, on its own, at exactly the value needed
to sustain zero steady-state error against a constant-rate ramp
(I_ss = rate / (coupling * Ki)) -- no separate term required, held stage
or ramping one, treated identically. The removed feedforward term instead
added a second, UNBOUNDED source of the same kind of offset on top of the
already-anti-windup-clamped integral, and for Free Rise's real authored
rate (1.5F/h) it alone exceeded the entire chamber safety envelope,
pinning chamber_target_f at CHAMBER_TARGET_MAX_F for over 3 of the stage's
~4 hours (confirmed directly from that fermentation's own samples) while
beer overshot the ramp's own end target by 3F.

Removing it means a sufficiently fast ramp can still legitimately outpace
what INTEGRAL_MAX_F_H allows the integral to sustain -- but that now shows
up as a bounded, safe lag (surfaced honestly to the user as "won't reach
target" rather than silently saturating the envelope for hours), not as
a second uncapped mechanism that can blow through it. See
INTEGRAL_MAX_F_H's own comment in control_constants.py for how its sizing
was reconsidered now that it's the only thing governing both exotherm
cancellation and ramp-tracking.

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


def chamber_target_for(beer_temp_f: float, beer_target_f: float, beer_error_integral_f_h: float) -> float:
    """PI on beer-temp error (error = beer_target_f - beer_temp_f),
    beer_error_integral_f_h already updated for this tick by
    update_beer_error_integral() -- this function itself does no
    accumulation, so it's a pure function of the current inputs and safe
    to call as many times as needed (e.g. the chart's forward-projection
    preview) without side effects.

    Deliberately takes no ramp-rate input -- beer_target_f already reflects
    wherever contracts.stages.target_temp_f() says the authored target is
    RIGHT NOW, held or ramping, and that's all this needs: the integral
    term above naturally supplies whatever sustained offset is needed to
    track a moving target, exactly the way it supplies one to counter a
    sustained disturbance like fermentation's own exothermic heat -- see
    this module's own docstring for why an earlier, separate ramp-
    feedforward term here was both redundant and confirmed live to be
    actively harmful.

    Clamped to the absolute safety envelope since a large combination of
    error and accumulated integral -- or a badly-authored stage (a huge
    ramp over very few hours, far faster than INTEGRAL_MAX_F_H's own
    sizing assumes) -- could otherwise ask for a chamber target no real
    equipment could or should attempt."""
    error = beer_target_f - beer_temp_f
    target = beer_target_f + BEER_KP_F_PER_F * error + BEER_KI_F_PER_F_H * beer_error_integral_f_h
    return max(CHAMBER_TARGET_MIN_F, min(CHAMBER_TARGET_MAX_F, target))
