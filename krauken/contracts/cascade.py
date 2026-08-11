"""The beer-temp cascade: beer temp + beer target -> a chamber demand mode
-> a concrete chamber target temperature. This is genuinely shared, not
just conceptually similar code living in two places -- the offline demo
generator (platforms/simulator/plant.py) and the real control loop
(daemon/control_loop.py) both call these exact functions, so the demo
batch's chamber behavior is provably the same rule the real daemon runs,
not a separately-invented approximation of it.

This module is daemon/beer-side only. It has no notion of relay timing
(min-on/min-off/lockout) -- that protection logic belongs to whatever is
actually driving the chamber (a real Hardware Supervisor on real hardware;
contracts/protection.py's state machine for the Simulator driver today),
per the architecture split in control_constants.py's docstring: the daemon
computes what chamber temperature it WANTS, never how a relay gets there.
"""
from __future__ import annotations

from krauken.contracts.control_constants import (
    BEER_RELEASE_OFFSET_F,
    BEER_TRIGGER_BAND_F,
    CHAMBER_COOL_CLAMP_F,
    CHAMBER_HEAT_CLAMP_F,
    CHAMBER_TARGET_MAX_F,
    CHAMBER_TARGET_MIN_F,
    RAMP_FEEDFORWARD_COUPLING_PER_H,
)


def beer_relay_demand(beer_temp_f: float, beer_target_f: float, current_mode: str) -> str:
    """Fire when the gap exceeds BEER_TRIGGER_BAND_F; release only once beer
    temp crosses back through the target itself (BEER_RELEASE_OFFSET_F) --
    hysteresis, so the cascade doesn't chatter right at the setpoint."""
    if beer_temp_f - beer_target_f >= BEER_TRIGGER_BAND_F:
        return "cool"
    if beer_target_f - beer_temp_f >= BEER_TRIGGER_BAND_F:
        return "heat"
    if current_mode == "cool" and beer_temp_f <= beer_target_f + BEER_RELEASE_OFFSET_F:
        return "idle"
    if current_mode == "heat" and beer_temp_f >= beer_target_f - BEER_RELEASE_OFFSET_F:
        return "idle"
    return current_mode


def chamber_target_for(mode: str, beer_target_f: float, ramp_rate_f_per_h: float = 0.0) -> float:
    """cool/heat push the chamber CLAMP_F past the beer target -- an
    aggressive overshoot meant to correct a real deviation quickly. idle
    does NOT de-energize (that was the old behavior, and it's the reason
    a real chamber would drift with the room for however long beer stayed
    satisfied, then need a long, hard correction once it finally noticed --
    exactly the "long idle then a spike" pattern the project owner flagged
    from a real run). Instead idle keeps the chamber loosely governed
    right at the beer target itself, no offset -- a real always-on
    fridge-constant-style controller works the same way: it never fully
    lets go, it just backs off from an aggressive correction to a gentle
    hold once the thing it's actually protecting (the beer) reaches
    target. Never returns None anymore; the return type used to be
    float|None for that reason.

    ramp_rate_f_per_h is the beer's OWN authored target's current rate of
    change (contracts.stages.target_rate_f_per_h) -- zero for a held
    setpoint, nonzero while a stepped stage (e.g. a cold crash) is actively
    ramping. A fixed clamp sized for correcting a one-time deviation from a
    HELD target isn't enough once the target itself keeps moving out from
    under the chamber: the beer settles into a permanent lag behind the
    ramp instead of ever closing the gap (confirmed against real cold-crash
    sample data -- the beer/target gap grew smoothly and never closed).
    Widening the clamp by ramp_rate/RAMP_FEEDFORWARD_COUPLING_PER_H exactly
    cancels that steady-state lag for whatever rate the stage is actually
    ramping at, while max() means a held target (rate 0) or a slow ramp
    keeps today's plain clamp -- this only ever makes the chamber MORE
    aggressive, never less. Clamped to the absolute safety envelope since a
    badly-authored stage (a huge ramp over very few hours) could otherwise
    ask for a chamber target no real equipment could or should attempt."""
    ramp_push_f = abs(ramp_rate_f_per_h) / RAMP_FEEDFORWARD_COUPLING_PER_H
    if mode == "cool":
        target = beer_target_f - max(CHAMBER_COOL_CLAMP_F, ramp_push_f)
    elif mode == "heat":
        target = beer_target_f + max(CHAMBER_HEAT_CLAMP_F, ramp_push_f)
    else:
        target = beer_target_f
    return max(CHAMBER_TARGET_MIN_F, min(CHAMBER_TARGET_MAX_F, target))
