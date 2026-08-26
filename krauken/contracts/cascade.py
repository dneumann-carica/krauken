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

Added a derivative term on top of the P+I above (genuinely PID now, not
PI) -- confirmed by real live data (fermentation 3's Free Rise stage:
beer lagging the ramp target by 3.5F, chamber pinned near the safety
envelope) that P+I alone leaves real headroom on the table: INTEGRAL_MAX_F_H
is a deliberately tight, disturbance-sized ceiling (see its own comment),
so a fast-moving ramp target saturates it almost immediately and then
just... waits, contributing nothing MORE while genuinely still behind.
Two things were tried and rejected first, each verified by direct
simulation rather than assumed: (1) back-calculation anti-windup against
that internal ceiling actually recovers SLOWER than the plain clamp
already here, because the shadow state overshoots past the ceiling during
saturation and has to fall back through it -- back-calculation only helps
when it's referenced against a genuine actuator limit (see (2)); (2)
dropping INTEGRAL_MAX_F_H entirely and doing textbook conditional-
integration against the REAL envelope (CHAMBER_TARGET_MIN_F/MAX_F) is
correct and safe (verified against a stuck-sensor fault: freezes
immediately, commands exactly the envelope edge, zero windup) but makes a
realistic transient WORSE (+2.09F peak overshoot vs today's +0.91F),
because the real envelope is far wider than the internal ceiling, so more
error accumulates before anything brakes it.

The derivative term is what actually delivers "move faster while
genuinely behind, brake automatically while closing in" without touching
either of those -- both a well-understood, standard technique (this is
just PID) and, critically, orthogonal to P+I: it reacts to the RATE
things are changing, not their magnitude, so it doesn't compete with the
integral's job of holding a steady offset against a real disturbance.
Two things make this specific to a MOVING target, though, not the classic
constant-setpoint case every PID tutorial assumes:
  - It differentiates against beer's own OBSERVED closing rate relative
    to the TARGET's own observed rate of change (both from actual
    consecutive readings, no hidden ramp-rate parameter -- keeps this
    function's documented ramp-agnosticism intact), not raw d(beer)/dt
    alone. Plain d(beer)/dt would apply a PERMANENT brake throughout any
    real ramp (beer is always "rising" while tracking one), reintroducing
    exactly the steady-state ramp-tracking error this session's earlier
    PI redesign was built to eliminate. Verified by simulation: this
    formulation leaves sustained-disturbance steady-state error
    essentially at zero (exotherm: -0.0009F, Cold Crash's rate:
    -0.0013F), matching P+I alone.
  - The raw rate is clamped to MAX_PLAUSIBLE_BEER_RATE_F_PER_H before
    it's ever filtered, and the filter itself (update_closing_rate_filter,
    CLOSING_RATE_FILTER_TAU_H) is a long, multi-hour low-pass -- both
    necessary, neither alone sufficient: a real sensor-settling artifact
    THIS fermentation's own probe produced (a 5.75F swing in under 2
    minutes) implies a momentary rate of -159F/h, and even a 4-hour
    filter can't meaningfully suppress that on its own within the first
    few ticks (a low-pass filter has no history to average against right
    at the start) -- confirmed by simulation, it still slammed the
    commanded chamber target straight to the envelope edge. Clamping the
    raw rate to something no real beer thermal mass could plausibly
    produce fixes it at the source; the long filter then handles ordinary
    per-tick sensor noise (verified against realistic noise, sigma=0.03F/
    tick: zero heat/cool demand reversals over 24h, vs hundreds without
    the rate clamp at the same gain).
See control_constants.py's BEER_KD_F_PER_F_PER_H/
CLOSING_RATE_FILTER_TAU_H/MAX_PLAUSIBLE_BEER_RATE_F_PER_H for the tuned
values and how they were chosen.
"""
from __future__ import annotations

from krauken.contracts.control_constants import (
    BEER_KD_F_PER_F_PER_H,
    BEER_KI_F_PER_F_H,
    BEER_KP_F_PER_F,
    CHAMBER_TARGET_MAX_F,
    CHAMBER_TARGET_MIN_F,
    CLOSING_RATE_FILTER_TAU_H,
    INTEGRAL_MAX_F_H,
    MAX_PLAUSIBLE_BEER_RATE_F_PER_H,
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


def update_closing_rate_filter(
    beer_temp_f: float, beer_target_f: float,
    prev_beer_temp_f: float | None, prev_beer_target_f: float | None,
    current_filtered_rate_f_per_h: float, dt_h: float,
) -> float:
    """Updates the low-pass-filtered "closing rate" the derivative term
    (chamber_target_for()'s closing_rate_filtered_f_per_h) reacts to:
    positive when beer is closing in on (or overshooting past) the target
    faster than the target itself is moving; negative when beer is
    falling further behind a moving target. See this module's own
    docstring for why this is closing-rate-relative-to-the-target's-own-
    rate, not raw d(beer)/dt, and why the raw rate is clamped before
    filtering.

    Caller is responsible for persisting the returned value AND this
    tick's own (beer_temp_f, beer_target_f) as next tick's
    prev_beer_temp_f/prev_beer_target_f, same pattern as
    update_beer_error_integral()'s own returned value -- kept as separate,
    explicit inputs/outputs rather than hidden state so this stays a pure
    function, safe to call repeatedly (e.g. the chart's forward-projection
    preview) without side effects.

    Returns current_filtered_rate_f_per_h UNCHANGED on the very first tick
    (prev_beer_temp_f is None) or a non-positive dt_h -- there's no real
    rate to compute yet, and guessing one would fabricate a derivative
    kick from nothing, the same reasoning update_beer_error_integral()
    applies to a fresh integral's first dt_h=0."""
    if prev_beer_temp_f is None or dt_h <= 0:
        return current_filtered_rate_f_per_h
    beer_rate = (beer_temp_f - prev_beer_temp_f) / dt_h
    beer_rate = max(-MAX_PLAUSIBLE_BEER_RATE_F_PER_H, min(MAX_PLAUSIBLE_BEER_RATE_F_PER_H, beer_rate))
    target_rate = (beer_target_f - prev_beer_target_f) / dt_h if prev_beer_target_f is not None else 0.0
    closing_rate = beer_rate - target_rate
    alpha = dt_h / (CLOSING_RATE_FILTER_TAU_H + dt_h)
    return current_filtered_rate_f_per_h + alpha * (closing_rate - current_filtered_rate_f_per_h)


def chamber_target_for(
    beer_temp_f: float, beer_target_f: float, beer_error_integral_f_h: float, closing_rate_filtered_f_per_h: float,
) -> float:
    """PID on beer-temp error (error = beer_target_f - beer_temp_f),
    beer_error_integral_f_h/closing_rate_filtered_f_per_h already updated
    for this tick by update_beer_error_integral()/
    update_closing_rate_filter() -- this function itself does no
    accumulation or filtering, so it's a pure function of the current
    inputs and safe to call as many times as needed (e.g. the chart's
    forward-projection preview) without side effects.

    Deliberately takes no ramp-rate input -- beer_target_f already reflects
    wherever contracts.stages.target_temp_f() says the authored target is
    RIGHT NOW, held or ramping, and that's all the P+I terms need: the
    integral naturally supplies whatever sustained offset is needed to
    track a moving target, exactly the way it supplies one to counter a
    sustained disturbance like fermentation's own exothermic heat -- see
    this module's own docstring for why an earlier, separate ramp-
    feedforward term here was both redundant and confirmed live to be
    actively harmful. closing_rate_filtered_f_per_h is NOT a ramp-rate
    input in that same sense -- it's derived purely from observed
    consecutive readings, not an authored stage property, so this
    function's ramp-agnosticism holds: it reacts to how fast things are
    ACTUALLY changing, never to what a stage says they're SUPPOSED to.

    Clamped to the absolute safety envelope since a large combination of
    error, accumulated integral, and closing-rate braking/boost -- or a
    badly-authored stage (a huge ramp over very few hours, far faster than
    INTEGRAL_MAX_F_H's own sizing assumes) -- could otherwise ask for a
    chamber target no real equipment could or should attempt."""
    error = beer_target_f - beer_temp_f
    target = (
        beer_target_f
        + BEER_KP_F_PER_F * error
        + BEER_KI_F_PER_F_H * beer_error_integral_f_h
        - BEER_KD_F_PER_F_PER_H * closing_rate_filtered_f_per_h
    )
    return max(CHAMBER_TARGET_MIN_F, min(CHAMBER_TARGET_MAX_F, target))
