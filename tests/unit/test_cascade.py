from __future__ import annotations

from krauken.contracts.cascade import beer_relay_demand, chamber_target_for


def test_fires_cool_once_beer_exceeds_trigger_band_above_target():
    assert beer_relay_demand(68.5, 68.0, "idle") == "cool"


def test_fires_heat_once_beer_exceeds_trigger_band_below_target():
    assert beer_relay_demand(67.5, 68.0, "idle") == "heat"


def test_stays_idle_within_trigger_band():
    assert beer_relay_demand(68.3, 68.0, "idle") == "idle"


def test_cooling_releases_only_once_beer_crosses_back_through_target():
    assert beer_relay_demand(68.1, 68.0, "cool") == "cool"  # still above target -- keep cooling
    assert beer_relay_demand(68.0, 68.0, "cool") == "idle"  # crossed through -- release


def test_heating_releases_only_once_beer_crosses_back_through_target():
    assert beer_relay_demand(67.9, 68.0, "heat") == "heat"  # still below target -- keep heating
    assert beer_relay_demand(68.0, 68.0, "heat") == "idle"  # crossed through -- release


def test_chamber_target_clamps_cool_below_beer_target():
    assert chamber_target_for("cool", 68.0) == 63.0


def test_chamber_target_clamps_heat_above_beer_target():
    assert chamber_target_for("heat", 68.0) == 72.0


def test_chamber_target_holds_at_beer_target_when_idle():
    # Not None -- idle no longer means "no target, de-energize." It means
    # "govern gently right at the beer's own target," not the aggressive
    # +/-CLAMP_F overshoot cool/heat use. See chamber_target_for's
    # docstring for why de-energizing produced a long-idle-then-spike
    # pattern in a real run.
    assert chamber_target_for("idle", 68.0) == 68.0


def test_chamber_target_ramp_feedforward_leaves_a_held_target_unchanged():
    # rate 0 (the default, and what a "constant" stage always has) -- same
    # plain clamp as before, no behavior change for the common case.
    assert chamber_target_for("cool", 68.0, ramp_rate_f_per_h=0.0) == 63.0
    assert chamber_target_for("heat", 68.0, ramp_rate_f_per_h=0.0) == 72.0


def test_chamber_target_ramp_feedforward_widens_the_clamp_for_a_fast_ramp():
    # A cold-crash-style ramp (68->38F over 96h -> -0.3125F/h) needs more
    # than the plain 5F clamp to keep the beer from permanently lagging the
    # moving target -- see the module docstring's derivation. -0.3125/0.05
    # = 6.25, so the chamber should be pushed 6.25F below the target here,
    # not just 5F.
    assert chamber_target_for("cool", 68.0, ramp_rate_f_per_h=-0.3125) == 68.0 - 6.25


def test_chamber_target_ramp_feedforward_never_narrows_a_slow_ramps_clamp():
    # A slow ramp (rate/coupling < the plain clamp) shouldn't get LESS
    # aggressive than today's plain-clamp behavior -- max(), not replace.
    assert chamber_target_for("cool", 68.0, ramp_rate_f_per_h=-0.01) == 63.0


def test_chamber_target_ramp_feedforward_is_symmetric_for_heating():
    assert chamber_target_for("heat", 68.0, ramp_rate_f_per_h=0.3125) == 68.0 + 6.25


def test_chamber_target_clamps_to_the_absolute_safety_envelope():
    # An extreme, badly-authored ramp (e.g. a huge drop over very few
    # hours) shouldn't ask real equipment for an absurd target -- the
    # feedforward is bounded by the same absolute envelope the rest of the
    # control stack assumes.
    assert chamber_target_for("cool", 40.0, ramp_rate_f_per_h=-5.0) == 28.0
