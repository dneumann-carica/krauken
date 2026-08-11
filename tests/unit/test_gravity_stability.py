from __future__ import annotations

import random

from krauken.contracts.gravity_stability import GravityStabilityWindow


def test_is_flat_false_with_too_little_history():
    w = GravityStabilityWindow()
    w.update(t_h=0.0, gravity_sg=1.050, window_h=1.0)
    assert w.is_flat(t_h=0.0, window_h=1.0, tolerance_sg=0.002) is False  # zero elapsed history
    w.update(t_h=0.5, gravity_sg=1.050, window_h=1.0)
    assert w.is_flat(t_h=0.5, window_h=1.0, tolerance_sg=0.002) is False  # only 0.5h of the required 1h


def test_is_flat_true_once_a_full_window_is_tight():
    w = GravityStabilityWindow()
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        w.update(t_h=t, gravity_sg=1.050, window_h=1.0)
    assert w.is_flat(t_h=1.0, window_h=1.0, tolerance_sg=0.002) is True


def test_is_flat_false_when_spread_exceeds_tolerance():
    w = GravityStabilityWindow()
    for t, g in [(0.0, 1.050), (0.25, 1.045), (0.5, 1.040), (0.75, 1.035), (1.0, 1.030)]:
        w.update(t_h=t, gravity_sg=g, window_h=1.0)
    assert w.is_flat(t_h=1.0, window_h=1.0, tolerance_sg=0.002) is False  # 0.020 spread, way over


def test_trailing_window_forgets_an_old_excursion_once_it_ages_out():
    """A stray excursion (sloshing, a bad tick) poisons flatness only
    while it's still inside the trailing window -- once enough time has
    passed that it's pruned out, flatness recovers on its own with no
    separate "restart the clock" logic needed."""
    w = GravityStabilityWindow()
    w.update(t_h=0.0, gravity_sg=1.050, window_h=1.0)
    w.update(t_h=0.1, gravity_sg=1.080, window_h=1.0)  # a wild stray reading
    for t in (0.2, 0.4, 0.6, 0.8, 1.0):
        w.update(t_h=t, gravity_sg=1.050, window_h=1.0)
    # the excursion (t=0.1) is still inside the trailing 1h window here
    assert w.is_flat(t_h=1.0, window_h=1.0, tolerance_sg=0.002) is False

    for t in (1.2, 1.6, 2.0):
        w.update(t_h=t, gravity_sg=1.050, window_h=1.0)
    # by t_h=2.0, the trailing window starts at 1.0 -- well clear of the t=0.1 excursion
    assert w.is_flat(t_h=2.0, window_h=1.0, tolerance_sg=0.002) is True


def test_mean_averages_the_current_window():
    w = GravityStabilityWindow()
    for t, g in [(0.0, 1.050), (0.5, 1.052), (1.0, 1.048)]:
        w.update(t_h=t, gravity_sg=g, window_h=10.0)
    assert w.mean() == (1.050 + 1.052 + 1.048) / 3


def test_latest_returns_the_most_recent_reading():
    w = GravityStabilityWindow()
    w.update(t_h=0.0, gravity_sg=1.050, window_h=10.0)
    w.update(t_h=1.0, gravity_sg=1.049, window_h=10.0)
    assert w.latest() == 1.049


def test_mean_and_latest_are_none_when_empty():
    w = GravityStabilityWindow()
    assert w.mean() is None
    assert w.latest() is None


def test_reset_clears_the_window():
    w = GravityStabilityWindow()
    w.update(t_h=0.0, gravity_sg=1.050, window_h=1.0)
    w.reset()
    assert len(w.readings) == 0
    assert w.latest() is None


class _NaiveReferenceWindow:
    """The pre-optimization implementation, kept ONLY as a slow-but-obviously-
    correct oracle for the randomized cross-check below -- rebuilds a plain
    list and recomputes max()/min() from scratch on every call, which is
    exactly the O(n)-per-call cost the real GravityStabilityWindow no longer
    pays (see that class's own docstring)."""

    def __init__(self) -> None:
        self.readings: list[tuple[float, float]] = []

    def update(self, t_h: float, gravity_sg: float, window_h: float) -> None:
        self.readings.append((t_h, gravity_sg))
        cutoff = t_h - window_h
        self.readings = [(t, g) for t, g in self.readings if t >= cutoff]

    def is_flat(self, t_h: float, window_h: float, tolerance_sg: float) -> bool:
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


def test_matches_the_naive_reference_implementation_across_a_long_randomized_sequence():
    # The sliding-window-max/min rewrite's whole point is behavioral
    # equivalence with the old O(n)-per-call list-rebuild approach, just
    # faster -- cross-checking against that exact old logic (kept above as
    # a deliberately-slow oracle) over a long, varied, seeded-random
    # sequence is a much stronger correctness guarantee than hand-picked
    # example cases alone, especially for a monotonic-deque implementation
    # (an off-by-one in when an entry gets popped is an easy, classic
    # mistake that a handful of examples can easily miss).
    rng = random.Random(20260810)
    fast = GravityStabilityWindow()
    slow = _NaiveReferenceWindow()
    t_h = 0.0
    gravity_sg = 1.050
    window_h = 2.0
    tolerance_sg = 0.002
    for i in range(2000):
        t_h += rng.uniform(0.01, 0.3)
        # A mix of tight noise, occasional spikes, and duplicate values --
        # duplicates specifically exercise the monotonic deques' tie-
        # breaking (<=/>=, not strict <>), and spikes exercise an entry
        # that's briefly the window's max/min before aging back out.
        if rng.random() < 0.05:
            gravity_sg += rng.uniform(-0.05, 0.05)
        elif rng.random() < 0.3:
            pass  # repeat the exact same value
        else:
            gravity_sg += rng.uniform(-0.0015, 0.0015)

        fast.update(t_h, gravity_sg, window_h)
        slow.update(t_h, gravity_sg, window_h)

        assert fast.latest() == slow.latest(), f"latest() mismatch at i={i}, t_h={t_h}"
        assert fast.mean() == slow.mean(), f"mean() mismatch at i={i}, t_h={t_h}"
        assert fast.is_flat(t_h, window_h, tolerance_sg) == slow.is_flat(t_h, window_h, tolerance_sg), (
            f"is_flat() mismatch at i={i}, t_h={t_h}"
        )
