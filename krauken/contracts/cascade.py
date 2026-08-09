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


def chamber_target_for(mode: str, beer_target_f: float) -> float:
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
    float|None for that reason."""
    if mode == "cool":
        return beer_target_f - CHAMBER_COOL_CLAMP_F
    if mode == "heat":
        return beer_target_f + CHAMBER_HEAT_CLAMP_F
    return beer_target_f
