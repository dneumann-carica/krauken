"""Shared low-level primitive: has a rolling window of gravity readings
gone flat -- stopped moving beyond ordinary sensor noise, self-relative,
with no notion of any fixed target?

Used by two different higher-level gates for two different purposes:
- contracts/stages.py's GravityGate -- has fermentation actually FINISHED
  (not just paused mid-way)?
- contracts/og_detection.py's OGDetector -- has the reading settled down
  enough after the beer was transferred into the fermenter to trust it as
  OG, despite the hydrometer/Tilt still bobbing and the beer still
  sloshing right after racking?

Same underlying question -- "has this sensor's recent history stopped
changing" -- asked with different window lengths and different things
done with the answer, so it's one primitive rather than two
independently-invented ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GravityStabilityWindow:
    """Tracks a trailing window of (t_h, gravity_sg) readings, pruned to
    the most recent `window_h` on every update -- old volatile readings
    age out on their own, so if the LAST window_h of readings are tight,
    it's flat again regardless of what happened before the window
    started. No separate "restart the clock" logic needed anywhere."""

    readings: list[tuple[float, float]] = field(default_factory=list)

    def update(self, t_h: float, gravity_sg: float, window_h: float) -> None:
        self.readings.append((t_h, gravity_sg))
        cutoff = t_h - window_h
        self.readings = [(t, g) for t, g in self.readings if t >= cutoff]

    def is_flat(self, t_h: float, window_h: float, tolerance_sg: float) -> bool:
        """True once the window holds a FULL window_h worth of history
        (not just "the two readings we happen to have are close together")
        and its max-min spread is within tolerance_sg. A genuine continued
        drop shows up as spread across the window exactly like noise
        does -- this one check catches both, which is why a single
        primitive covers both the OG-detection and fermentation-complete
        use cases."""
        if not self.readings:
            return False
        oldest_t_h = self.readings[0][0]
        if (t_h - oldest_t_h) < window_h:
            return False
        values = [g for _, g in self.readings]
        return (max(values) - min(values)) <= tolerance_sg

    def mean(self) -> float | None:
        if not self.readings:
            return None
        values = [g for _, g in self.readings]
        return sum(values) / len(values)

    def latest(self) -> float | None:
        return self.readings[-1][1] if self.readings else None

    def reset(self) -> None:
        """Call when the reading is stale/missing -- a dropout must not
        let readings from before and after the gap be stitched together
        as if they were continuous (same failsafe philosophy as
        contracts/stages.py's other gates -- see contracts/failsafe.py's
        module docstring)."""
        self.readings = []
