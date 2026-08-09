from __future__ import annotations

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
    assert w.readings == []
    assert w.latest() is None
