"""Stage-criteria evaluation, shared by the offline demo generator
(db/seed.py) and the real control loop (daemon/control_loop.py) -- both
decide "has this stage finished" and "what's the target temp right now"
through the exact same functions, so a demo batch's stage timing is a
genuine exercise of the real evaluator, not a separately-invented one.

Operates on plain dict-like stage rows (sqlite3.Row or a dict) rather than
its own dataclass, matching every other read path in this codebase
(db/queries.py returns dicts) -- there's no separate DTO to keep in sync
with the schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from krauken.contracts.control_constants import BEER_TRIGGER_BAND_F
from krauken.contracts.gravity_stability import GravityStabilityWindow

# Max-min spread allowed within a gravity_stable_hours window for it to
# count as "flat." See GravityGate's docstring for what this tolerance is
# actually deciding between (genuine continued fermentation vs. ordinary
# sensor noise) -- it has to sit clearly ABOVE the real noise ceiling
# (so pure noise never fails the check) and clearly BELOW the smallest
# genuine decline worth catching (so a fermentation that's still
# slowly finishing doesn't get called "flat" early).
#
# At the simulator's steady-state jitter_sg=0.0002 (uniform, freshly
# re-rolled every read), a 24h window at typical tick cadence empirically
# spreads right up against ~0.0004 (its theoretical ceiling, 2*jitter_sg)
# essentially every time -- there's no meaningful margin below that from
# noise alone. The OLD value here (0.002) was 5x looser than that ceiling,
# which is why a real ~24h-window run was observed locking in "flat" while
# gravity had actually still dropped ~0.0017 over that same window (a real,
# if slow, continued decline, not noise) -- a stall-safety-worthy case this
# constant is supposed to help distinguish, not paper over. 0.0006 keeps
# comfortable margin above the ~0.0004 noise ceiling while sitting well
# below that kind of still-declining case.
GRAVITY_STABLE_TOLERANCE_SG = 0.0006


def target_temp_f(stage: Mapping[str, Any], elapsed_h: float) -> float:
    """The stage's own authored target -- constant, or linearly ramping
    from temp_from_f to temp_to_f over ramp_hours. This is a setpoint for
    the beer-temp cascade, independent of whichever criteria decide when
    the stage itself is finished (see stage_finished)."""
    if stage["temp_mode"] == "constant":
        return stage["temp_f"]
    ramp_hours = stage["ramp_hours"]
    frac = min(1.0, elapsed_h / ramp_hours) if ramp_hours > 0 else 1.0
    return stage["temp_from_f"] + (stage["temp_to_f"] - stage["temp_from_f"]) * frac


def target_rate_f_per_h(stage: Mapping[str, Any], elapsed_h: float) -> float:
    """The stage's own authored target's instantaneous rate of change --
    zero for a constant-mode stage, and zero for a stepped one too once
    elapsed_h has passed ramp_hours (the target has arrived and holds, per
    target_temp_f's own clamp above). Feeds the cascade's ramp feedforward
    (contracts.cascade.chamber_target_for) -- entirely separate from
    stage_finished's own completion criteria, which don't care how fast
    the target itself is moving."""
    if stage["temp_mode"] != "stepped":
        return 0.0
    ramp_hours = stage["ramp_hours"]
    if not ramp_hours or ramp_hours <= 0 or elapsed_h >= ramp_hours:
        return 0.0
    return (stage["temp_to_f"] - stage["temp_from_f"]) / ramp_hours


@dataclass
class GravityGate:
    """Satisfied once gravity has gone FLAT -- self-relative, stopped
    moving beyond ordinary sensor noise, regardless of its absolute level
    -- for gravity_stable_hours, AND the latest reading is at/below
    gravity_hi.

    Flatness is the real "fermentation is done" signal: it directly
    answers "has gravity stopped dropping," not "has it dropped below some
    line and then sat in a range" -- those are different questions, and a
    fermentation that's still slowly dropping doesn't look flat even if
    it's already crossed below gravity_hi. gravity_hi is a SAFETY NET
    only, checked separately: if gravity plateaus early -- a stalled
    fermentation, well above where the profile expects it to finish --
    that plateau is flat too, but it must never be mistaken for "done"
    just because it's flat. Both conditions have to hold.

    Built on GravityStabilityWindow (contracts/gravity_stability.py) --
    the exact same self-relative-flatness primitive contracts/
    og_detection.py's OGDetector uses, just with a much longer window and
    the added threshold safety check that OG-detection has no equivalent
    of (there's no "stall" concept when locking in a starting value)."""

    window: GravityStabilityWindow = field(default_factory=GravityStabilityWindow)

    def update(self, t_h: float, gravity: float, stable_hours: float) -> None:
        self.window.update(t_h, gravity, stable_hours)

    def satisfied(self, t_h: float, stable_hours: float, threshold: float) -> bool:
        latest = self.window.latest()
        if latest is None or latest > threshold:
            return False
        return self.window.is_flat(t_h, stable_hours, GRAVITY_STABLE_TOLERANCE_SG)

    def reset(self) -> None:
        """Call this instead of update() when the reading is stale/lost --
        a dead sensor must not count elapsed time toward stability just
        because nobody called update() to notice it drifted (see
        contracts/failsafe.py's module docstring)."""
        self.window.reset()


@dataclass
class TempHoldGate:
    """Same shape as GravityGate: satisfied once the beer has sat within
    BEER_TRIGGER_BAND_F of hold_temp_f continuously for hold_hours. Reuses
    the same tolerance the cascade used to trigger/release on (back when it
    was a discrete hysteresis, before it became a continuous PI controller
    with no discrete "at target" point of its own -- see contracts/
    cascade.py) rather than inventing a separate "close enough" number for
    what's conceptually the same idea: has the beer actually arrived."""

    stable_since_h: float | None = None

    def update(self, t_h: float, beer_temp_f: float, hold_temp_f: float) -> None:
        if abs(beer_temp_f - hold_temp_f) <= BEER_TRIGGER_BAND_F:
            if self.stable_since_h is None:
                self.stable_since_h = t_h
        else:
            self.stable_since_h = None

    def satisfied(self, t_h: float, hold_hours: float) -> bool:
        return self.stable_since_h is not None and (t_h - self.stable_since_h) >= hold_hours

    def reset(self) -> None:
        self.stable_since_h = None


@dataclass
class GravityBelowGate:
    """Same shape as TempHoldGate: satisfied once gravity has sat at or
    below a threshold continuously for hold_hours. Deliberately NOT
    GravityGate -- this makes no claim about flatness. Gravity can still be
    actively dropping the entire window and this is satisfied regardless,
    as long as it never climbs back above the threshold; a real hydrometer/
    Tilt reading can wobble slightly even while genuinely below the line,
    but that's what health/failsafe handling (reset() on a stale reading)
    is for, not a tolerance band here -- unlike GravityGate's self-relative
    noise floor, "at or below" is already exact."""

    stable_since_h: float | None = None

    def update(self, t_h: float, gravity: float, threshold: float) -> None:
        if gravity <= threshold:
            if self.stable_since_h is None:
                self.stable_since_h = t_h
        else:
            self.stable_since_h = None

    def satisfied(self, t_h: float, hold_hours: float) -> bool:
        return self.stable_since_h is not None and (t_h - self.stable_since_h) >= hold_hours

    def reset(self) -> None:
        self.stable_since_h = None


def stage_finished(
    stage: Mapping[str, Any],
    elapsed_h: float,
    t_h: float,
    *,
    gravity_gate: GravityGate | None = None,
    temp_hold_gate: TempHoldGate | None = None,
    gravity_below_gate: GravityBelowGate | None = None,
) -> tuple[bool, str]:
    """Returns (finished, reason) -- reason is only meaningful when
    finished is True; it becomes fermentation_stages.end_actual_reason.

    max_hours (if set -- optional per the relaxed safety rule, see the
    migration's file header) always wins outright: a stage that's run long
    enough is done regardless of what its own end_mode criteria say.
    min_hours (if set) is the opposite guard -- it blocks completion even
    if the end_mode criteria already look satisfied, so a spuriously early
    plateau (a gravity reading that happens to sit in-band for one tick
    right after the stage starts) can't end a stage before it's had a
    realistic chance to actually get there.
    """
    max_hours = stage.get("max_hours")
    if max_hours is not None and elapsed_h >= max_hours:
        return True, "max_cap"

    min_hours = stage.get("min_hours")
    if min_hours is not None and elapsed_h < min_hours:
        return False, stage["end_mode"]

    if stage["end_mode"] == "time":
        return elapsed_h >= stage["end_hours"], "time"
    if stage["end_mode"] == "gravity":
        if gravity_gate is None:
            return False, "gravity"
        return gravity_gate.satisfied(t_h, stage["gravity_stable_hours"], stage["gravity_hi"]), "gravity"
    if stage["end_mode"] == "temp_hold":
        if temp_hold_gate is None:
            return False, "temp_hold"
        return temp_hold_gate.satisfied(t_h, stage["hold_hours"]), "temp_hold"
    if stage["end_mode"] == "gravity_below":
        if gravity_below_gate is None:
            return False, "gravity_below"
        return gravity_below_gate.satisfied(t_h, stage["hold_hours"]), "gravity_below"
    return False, "time"
