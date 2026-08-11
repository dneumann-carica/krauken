from __future__ import annotations

from krauken.contracts.og_detection import OG_STABLE_WINDOW_H, OGDetector


def test_locks_once_spread_stays_under_tolerance_for_the_full_window():
    d = OGDetector()
    for t in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5):
        d.update(t, 1.052)
    assert d.locked_og == 1.052


def test_does_not_lock_before_a_full_window_of_history():
    d = OGDetector()
    d.update(0.0, 1.052)
    d.update(0.2, 1.052)
    assert d.locked_og is None  # only 0.2h of the required 0.5h so far


def test_does_not_lock_while_still_sloshing():
    d = OGDetector()
    for t, g in [(0.0, 1.080), (0.1, 1.020), (0.2, 1.065), (0.3, 1.040), (0.4, 1.055), (0.5, 1.048)]:
        d.update(t, g)
    assert d.locked_og is None  # wild swings -- nowhere near tolerance


def test_locks_to_the_mean_of_the_settled_window():
    d = OGDetector()
    for t, g in [(0.0, 1.0515), (0.1, 1.0525), (0.2, 1.0520), (0.3, 1.0518), (0.4, 1.0522), (0.5, 1.0520)]:
        d.update(t, g)
    assert d.locked_og is not None
    assert 1.0515 <= d.locked_og <= 1.0525  # settled-window mean, close to the true value


def test_ignores_further_updates_once_locked():
    d = OGDetector()
    for t in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5):
        d.update(t, 1.052)
    assert d.locked_og == 1.052
    d.update(1.0, 1.100)  # wildly different reading, arrives after locking
    assert d.locked_og == 1.052  # unchanged -- OG is set exactly once


def test_reset_clears_the_accumulating_window_but_not_an_already_locked_value():
    d = OGDetector()
    d.update(0.0, 1.052)
    d.reset()  # a stale reading before locking -- clears the window
    assert len(d.window.readings) == 0
    assert d.locked_og is None

    for t in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5):
        d.update(t, 1.052)
    assert d.locked_og == 1.052
    d.reset()  # a stale reading AFTER locking -- must not clear the locked value
    assert d.locked_og == 1.052


def test_force_lock_is_a_noop_on_an_empty_window():
    d = OGDetector()
    d.force_lock()
    assert d.locked_og is None  # nothing to lock to -- caller retries next tick


def test_force_lock_uses_whatever_has_been_collected():
    d = OGDetector()
    d.update(0.0, 1.050)
    d.update(0.2, 1.054)  # never settles within OG_STABLE_WINDOW_H
    assert d.locked_og is None
    d.force_lock()
    assert d.locked_og == (1.050 + 1.054) / 2


def test_force_lock_is_a_noop_once_already_locked():
    d = OGDetector()
    for t in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5):
        d.update(t, 1.052)
    assert d.locked_og == 1.052
    d.force_lock()
    assert d.locked_og == 1.052


def test_stable_window_constant_is_half_an_hour():
    # sanity-checks the module's own tuning constant hasn't drifted silently
    assert OG_STABLE_WINDOW_H == 0.5
