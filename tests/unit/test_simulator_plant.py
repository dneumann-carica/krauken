"""Direct unit coverage for platforms/simulator/plant.py's pure physics
functions -- previously untested directly (only exercised indirectly via
higher-level simulator/live tests). Added alongside the gravity_at()
overflow fix: a runaway/never-waiting SimulatorClock pushed t_h to an
astronomical value on real hardware (caught live on brewpi.local, not in
CI), and gravity_at()'s plain logistic formula raised OverflowError
("math range error") for a large positive exponent -- unlike every other
math.exp() call in this module, whose exponents are unconditionally <= 0
by construction (see advance_physics's own docstring on the prior,
similar ~1e200F incident that motivated that pattern) and so only ever
safely underflow toward 0 instead."""
from __future__ import annotations

import math

from krauken.platforms.simulator.plant import GravityParams, gravity_at

DEFAULT = GravityParams()  # og=1.052, terminal=1.005, midpoint_h=60.0, steepness_h=6.0


def test_gravity_at_midpoint_is_exactly_halfway():
    assert gravity_at(DEFAULT, DEFAULT.midpoint_h) == DEFAULT.terminal + (DEFAULT.og - DEFAULT.terminal) / 2


def test_gravity_at_zero_is_close_to_og_not_yet_decayed():
    g = gravity_at(DEFAULT, 0.0)
    assert abs(g - DEFAULT.og) < 0.001


def test_gravity_at_far_past_midpoint_is_close_to_terminal():
    g = gravity_at(DEFAULT, 200.0)
    assert abs(g - DEFAULT.terminal) < 0.001


def test_gravity_at_matches_the_original_unstable_formula_for_normal_t_h():
    # Regression check: the numerically-stable rewrite must produce
    # identical results to the original plain-logistic formula for any
    # t_h that formula could actually evaluate without overflowing --
    # this is a stability fix, not a behavior change.
    for t_h in (0.0, 12.5, 60.0, 100.0, 300.0):
        x = (t_h - DEFAULT.midpoint_h) / DEFAULT.steepness_h
        original = DEFAULT.terminal + (DEFAULT.og - DEFAULT.terminal) / (1 + math.exp(x))
        assert gravity_at(DEFAULT, t_h) == original


def test_gravity_at_extreme_positive_t_h_does_not_overflow():
    # The actual real-world failure: a runaway clock pushed t_h to
    # something astronomically past midpoint_h. Must settle at terminal,
    # not raise.
    g = gravity_at(DEFAULT, 1e15)
    assert g == DEFAULT.terminal


def test_gravity_at_extreme_negative_t_h_does_not_overflow():
    g = gravity_at(DEFAULT, -1e15)
    assert g == DEFAULT.og
