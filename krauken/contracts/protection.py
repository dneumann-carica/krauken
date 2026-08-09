"""Compressor/relay anti-chatter protection: the precedence rules a real
Hardware Supervisor enforces on real hardware (board-not-verified, then
min-on, then lockout, then min-off, then thermostat demand), extracted as a
pure state machine so it's honestly testable today even though no real
Supervisor process exists yet. The M2 SimPlant live driver
(platforms/simulator/live.py) runs this directly in-process as a stand-in;
a future real Supervisor imports the exact same function rather than
re-implementing the rule.

`board_verified` covers the "board-not-verified always wins" rule generically
(force idle, no timers) without baking in anything Krauken-specific -- the
Simulator always passes True, since it has no board-ID loopback concept.

This module is deliberately ignorant of beer temperature: it only ever sees
a `demand` string (what the cascade wants) and elapsed time. That's the
whole point of the tier split in control_constants.py -- this logic must
keep protecting the compressor even if the beer-temp cascade above it is
completely broken.
"""
from __future__ import annotations

from dataclasses import dataclass

from krauken.contracts.control_constants import ControlTuning

RelayMode = str  # "idle" | "cool" | "heat"


def thermostat_demand(current_temp_f: float, target_temp_f: float, deadband_f: float) -> RelayMode:
    """The "then thermostat demand" step this module's own docstring lists
    last in the Hardware Supervisor's precedence chain -- hysteresis/
    deadband thermostat around the commanded chamber target
    (krauken-software-design.md Section 7), one tier below
    contracts/cascade.py's beer_relay_demand() (same hysteresis shape, one
    level up: that one decides what the CHAMBER should be commanded to do
    based on BEER temp; this one decides what the RELAY should physically
    do based on CHAMBER temp actually reaching that command).

    Fires cool/heat once the reading is `deadband_f` past the target in
    either direction; releases the moment it's back within the deadband --
    NOT "crosses back through the target itself." That distinction is the
    whole fix: a first-order exponential approach toward a fixed drive_to
    (advance_chamber_temp) is asymptotic and, once genuinely stable
    (k*dt_h < 1, no overshoot), mathematically never actually reaches or
    crosses the exact target -- it just gets arbitrarily close, forever. A
    "release on crossing" condition never fires against that kind of
    approach, so heat/cool would run indefinitely once engaged, long after
    the chamber has visibly stopped moving. Releasing as soon as the
    reading is merely close enough (within deadband_f, from either side)
    fixes that without reintroducing the old zero-deadband chatter bug --
    the three zones (cool wanted / idle / heat wanted) partition the whole
    range with no ambiguity, so this no longer needs `current_mode` at all
    (unlike the crossing-based version this replaces)."""
    if current_temp_f - target_temp_f >= deadband_f:
        return "cool"
    if target_temp_f - current_temp_f >= deadband_f:
        return "heat"
    return "idle"
    if current_mode == "heat" and current_temp_f >= target_temp_f:
        return "idle"
    return current_mode


@dataclass(frozen=True, slots=True)
class RelayState:
    mode: RelayMode = "idle"
    held_s: float = 0.0  # time since `mode` last changed
    last_run: RelayMode | None = None  # last non-idle mode that actually ran, for lockout


def next_relay_state(
    state: RelayState,
    demand: RelayMode,
    dt_s: float,
    tuning: ControlTuning = ControlTuning(),
    *,
    board_verified: bool = True,
) -> RelayState:
    """One tick of the protection state machine. Pure -- same inputs, same
    output, no clock/IO of its own; the caller supplies dt_s from whatever
    Clock it's using (real or ManualClock), which is what lets a scenario
    test compress a multi-week run without waiting for it."""
    if not board_verified:
        return RelayState(mode="idle", held_s=0.0, last_run=state.last_run)

    held_s = state.held_s + dt_s

    if state.mode != "idle":
        if demand == state.mode:
            return RelayState(mode=state.mode, held_s=held_s, last_run=state.last_run)
        # min-on outranks both the lockout rule below and the thermostat's
        # own demand -- a relay that just started never gets cut short.
        if held_s >= tuning.min_on_s:
            return RelayState(mode="idle", held_s=0.0, last_run=state.last_run)
        return RelayState(mode=state.mode, held_s=held_s, last_run=state.last_run)

    if demand in ("cool", "heat"):
        switching_sides = state.last_run is not None and state.last_run != demand
        required_wait = tuning.opposite_lockout_s if switching_sides else tuning.min_off_s
        if held_s >= required_wait:
            return RelayState(mode=demand, held_s=0.0, last_run=demand)
        return RelayState(mode="idle", held_s=held_s, last_run=state.last_run)

    return RelayState(mode="idle", held_s=held_s, last_run=state.last_run)
