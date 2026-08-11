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

from collections import deque
from dataclasses import dataclass, field


@dataclass
class GravityStabilityWindow:
    """Tracks a trailing window of (t_h, gravity_sg) readings, pruned to
    the most recent `window_h` on every update -- old volatile readings
    age out on their own, so if the LAST window_h of readings are tight,
    it's flat again regardless of what happened before the window
    started. No separate "restart the clock" logic needed anywhere.

    `readings` is a deque, not a plain list, and update()/is_flat() are
    O(1) amortized regardless of how many readings the window currently
    holds. An earlier version rebuilt a plain list (`[(t, g) for ... if
    t >= cutoff]`) and recomputed max(values)/min(values) from scratch on
    every single call -- both O(n) per call. At a tick cadence fast enough
    to pack thousands of readings into the window (the accelerated dev-
    time simulator's control loop: a 24h gravity_stable_hours window at a
    5s tick interval holds up to 17,280 readings), that dominated the real
    wall-clock cost of ticking through any gravity-gated stage -- roughly
    a 10x real-time slowdown measured against a plain time-based stage of
    comparable simulated length.

    The fix is the standard "sliding window maximum" technique (also
    covers minimum, run twice with the comparison flipped): alongside the
    full `readings` deque, keep two MONOTONIC deques, `_max_deque` and
    `_min_deque`, holding (t_h, gravity_sg) pairs. `_max_deque` is kept
    non-increasing by gravity_sg from front to back -- inserting a new
    reading pops off any tail entries it dominates (they can never be the
    window's max again, since the new one is both later AND at least as
    large) before appending, so the front is always the current window's
    maximum. `_min_deque` mirrors this for the minimum. Each reading is
    pushed and popped from each deque AT MOST ONCE over its lifetime (once
    an entry is popped for being dominated, or for aging out, it's never
    reinserted), so total deque work across N updates is O(N), i.e. O(1)
    amortized per update -- regardless of how large the window's READING
    COUNT grows, unlike the old max()/min() rebuild."""

    readings: deque[tuple[float, float]] = field(default_factory=deque)
    _max_deque: deque[tuple[float, float]] = field(default_factory=deque, repr=False, compare=False)
    _min_deque: deque[tuple[float, float]] = field(default_factory=deque, repr=False, compare=False)

    def update(self, t_h: float, gravity_sg: float, window_h: float) -> None:
        self.readings.append((t_h, gravity_sg))
        while self._max_deque and self._max_deque[-1][1] <= gravity_sg:
            self._max_deque.pop()
        self._max_deque.append((t_h, gravity_sg))
        while self._min_deque and self._min_deque[-1][1] >= gravity_sg:
            self._min_deque.pop()
        self._min_deque.append((t_h, gravity_sg))

        cutoff = t_h - window_h
        while self.readings and self.readings[0][0] < cutoff:
            self.readings.popleft()
        while self._max_deque and self._max_deque[0][0] < cutoff:
            self._max_deque.popleft()
        while self._min_deque and self._min_deque[0][0] < cutoff:
            self._min_deque.popleft()

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
        return (self._max_deque[0][1] - self._min_deque[0][1]) <= tolerance_sg

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
        self.readings.clear()
        self._max_deque.clear()
        self._min_deque.clear()
