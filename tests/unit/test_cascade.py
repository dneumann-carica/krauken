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
