from __future__ import annotations

from krauken.contracts.control_constants import BEER_TEMP_LOST_THRESHOLD_S, CHAMBER_TEMP_LOSS_S
from krauken.contracts.failsafe import assess_health, is_stale


def test_is_stale_treats_no_reading_at_all_as_stale():
    assert is_stale(None, now=1000.0, threshold_s=60.0) is True


def test_is_stale_true_only_past_the_threshold():
    assert is_stale(last_good_ts=100.0, now=100.0 + 59.0, threshold_s=60.0) is False
    assert is_stale(last_good_ts=100.0, now=100.0 + 61.0, threshold_s=60.0) is True


def test_assess_health_all_fresh():
    now = 10_000.0
    result = assess_health(
        now=now,
        beer_last_good_ts=now - 5,
        chamber_last_good_ts=now - 5,
        gravity_last_good_ts=now - 5,
        gravity_mapped=True,
    )
    assert (result.beer_temp_ok, result.chamber_temp_ok, result.gravity_ok) == (True, True, True)


def test_assess_health_beer_temp_lost():
    now = 10_000.0
    result = assess_health(
        now=now,
        beer_last_good_ts=now - BEER_TEMP_LOST_THRESHOLD_S - 1,
        chamber_last_good_ts=now,
        gravity_last_good_ts=now,
        gravity_mapped=True,
    )
    assert result.beer_temp_ok is False
    assert result.chamber_temp_ok is True


def test_assess_health_chamber_temp_lost():
    now = 10_000.0
    result = assess_health(
        now=now,
        beer_last_good_ts=now,
        chamber_last_good_ts=now - CHAMBER_TEMP_LOSS_S - 1,
        gravity_last_good_ts=now,
        gravity_mapped=True,
    )
    assert result.chamber_temp_ok is False


def test_assess_health_gravity_unmapped_is_none_not_false():
    now = 10_000.0
    result = assess_health(
        now=now, beer_last_good_ts=now, chamber_last_good_ts=now, gravity_last_good_ts=None, gravity_mapped=False
    )
    assert result.gravity_ok is None  # unmapped is a distinct state from "mapped but stale"


def test_assess_health_gravity_mapped_but_stale_is_false():
    now = 10_000.0
    result = assess_health(
        now=now,
        beer_last_good_ts=now,
        chamber_last_good_ts=now,
        gravity_last_good_ts=now - 100_000,
        gravity_mapped=True,
    )
    assert result.gravity_ok is False
