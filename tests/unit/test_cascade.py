from __future__ import annotations

import pytest

from krauken.contracts.cascade import chamber_target_for, update_beer_error_integral, update_closing_rate_filter

# Gain values as of this writing (contracts/control_constants.py):
# BEER_KP_F_PER_F=2.0, BEER_KI_F_PER_F_H=2.0, INTEGRAL_MAX_F_H=4.0,
# BEER_KD_F_PER_F_PER_H=24.0, CLOSING_RATE_FILTER_TAU_H=4.0,
# MAX_PLAUSIBLE_BEER_RATE_F_PER_H=5.0, CHAMBER_TARGET_MIN_F=28.0,
# CHAMBER_TARGET_MAX_F=90.0 -- expected values below are computed directly
# from these, spelled out in each test's own comment, deliberately NOT
# imported symbolically: a test that recomputes its own expectation from
# whatever the live constant happens to be can't catch a real regression
# in the gain itself.
#
# No ramp-rate tests here -- chamber_target_for() takes no ramp input at
# all (see its own docstring for why a separate ramp-feedforward term was
# removed: redundant with, and confirmed live to be actively harmful
# alongside, the integral term above, which already supplies whatever
# sustained offset a ramping target needs on its own). The derivative
# term below is NOT a ramp-rate input in that sense either -- it's derived
# purely from observed consecutive readings (see update_closing_rate_filter's
# own docstring) -- so this file's P/I isolation tests below pass
# closing_rate_filtered_f_per_h=0.0 explicitly to isolate P+I exactly as
# before; the D-term's own behavior gets its own tests further down.


def test_holds_exactly_at_beer_target_with_zero_error_and_zero_integral():
    assert chamber_target_for(68.0, 68.0, 0.0, 0.0) == 68.0


def test_proportional_term_pushes_chamber_above_target_when_beer_runs_cold():
    # error = 68.0 - 67.0 = 1.0 -> +2.0*1.0
    assert chamber_target_for(67.0, 68.0, 0.0, 0.0) == 70.0


def test_proportional_term_pushes_chamber_below_target_when_beer_runs_warm():
    # error = 68.0 - 69.0 = -1.0 -> +2.0*(-1.0)
    assert chamber_target_for(69.0, 68.0, 0.0, 0.0) == 66.0


def test_integral_term_alone_offsets_the_chamber_with_zero_instantaneous_error():
    # Beer sitting exactly AT target right now, but with an accumulated
    # history of having run warm (a negative integral) -- the correction
    # persists even though there's nothing to react to this instant,
    # exactly the property a pure P (or the old bang-bang) design lacked.
    assert chamber_target_for(68.0, 68.0, -1.5, 0.0) == 65.0  # +2.0*(-1.5)


def test_update_beer_error_integral_accumulates_error_over_elapsed_time():
    integral = update_beer_error_integral(69.0, 68.0, 0.0, dt_h=0.5)  # error=-1.0
    assert integral == -0.5
    integral = update_beer_error_integral(69.0, 68.0, integral, dt_h=0.5)
    assert integral == -1.0


def test_update_beer_error_integral_anti_windup_clamps_the_positive_side():
    # 3.9 + (68.0-67.0)*1.0 = 4.9, clamped to the 4.0 ceiling.
    assert update_beer_error_integral(67.0, 68.0, 3.9, dt_h=1.0) == 4.0


def test_update_beer_error_integral_anti_windup_clamps_the_negative_side():
    # -3.9 + (68.0-69.0)*1.0 = -4.9, clamped to the -4.0 floor.
    assert update_beer_error_integral(69.0, 68.0, -3.9, dt_h=1.0) == -4.0


def test_clamps_to_the_absolute_safety_envelope():
    # An extreme combination -- max negative integral plus a huge
    # instantaneous error -- must never ask real equipment for an absurd
    # target: 40.0 + 2.0*(40.0-80.0) + 2.0*(-4.0) = 40 - 80 - 8 = -48,
    # clamped to the 28.0 floor.
    assert chamber_target_for(80.0, 40.0, -4.0, 0.0) == 28.0


def test_sustained_one_directional_error_drives_a_growing_correction_via_the_integral():
    # The property that motivated replacing the old bang-bang cascade: a
    # disturbance that never lets the error clear on its own (e.g. a
    # sustained fermentation exotherm) should get a progressively
    # stronger correction over time, not repeatedly bang-bang between a
    # fixed clamp and a full release back to zero -- see cascade.py's own
    # module docstring. A small, persistent residual warmth (0.3F) that
    # a modest proportional gain alone can't fully cancel.
    beer_target, beer_temp = 68.0, 68.3
    integral = 0.0
    targets = []
    for _ in range(10):  # 10 * 0.5h = 5 simulated hours, well short of the anti-windup clamp
        integral = update_beer_error_integral(beer_temp, beer_target, integral, dt_h=0.5)
        targets.append(chamber_target_for(beer_temp, beer_target, integral, 0.0))

    assert all(earlier > later for earlier, later in zip(targets, targets[1:])), (
        "each successive target should be strictly more aggressive (lower) than the last"
    )
    p_only = beer_target + 2.0 * (beer_target - beer_temp)  # what pure P alone would produce: 67.4
    assert targets[-1] < p_only - 1.0, "the integral should meaningfully out-correct P alone given enough time"


# --- Derivative term, in isolation ---------------------------------------


def test_closing_rate_filter_returns_unchanged_on_the_very_first_tick():
    # No prior reading yet -- nothing to differentiate, matches
    # update_beer_error_integral()'s own dt_h=0 "first tick" behavior.
    assert update_closing_rate_filter(67.0, 66.0, None, None, 0.0, dt_h=0.5) == 0.0
    assert update_closing_rate_filter(67.0, 66.0, 66.5, 66.0, 0.0, dt_h=0.0) == 0.0


def test_closing_rate_filter_is_positive_when_beer_closes_in_faster_than_a_held_target():
    # Held target (target_rate=0), beer rising 2F/h toward it -- a real,
    # unclamped, plausible rate (well under the 5F/h clamp) -- filtered
    # rate should end up positive (a "closing in" / brake-eligible signal).
    # alpha = dt_h/(tau_h+dt_h) for dt_h=0.1, CLOSING_RATE_FILTER_TAU_H=4.0.
    # pytest.approx -- (66.2-66.0)/0.1 isn't bit-exact 2.0 in floating
    # point, an artifact of the test's own chosen inputs, not the function.
    dt_h = 0.1
    rate = update_closing_rate_filter(66.2, 66.0, 66.0, 66.0, 0.0, dt_h=dt_h)
    assert rate > 0.0
    alpha = dt_h / (4.0 + dt_h)
    assert rate == pytest.approx(alpha * 2.0)  # alpha * (beer_rate=2.0 - target_rate=0.0 - 0.0)


def test_closing_rate_filter_is_near_zero_when_beer_tracks_a_ramp_exactly():
    # Beer rising at EXACTLY the target's own rate -- perfect ramp
    # tracking -- must not look like "closing in" and trigger a brake;
    # this is the whole reason the filter differentiates against the
    # target's own observed rate, not raw d(beer)/dt.
    rate = update_closing_rate_filter(66.15, 66.15, 66.0, 66.0, 0.0, dt_h=0.1)
    assert rate == 0.0


def test_closing_rate_filter_clamps_an_implausible_raw_rate_before_filtering():
    # A real recorded sensor-settling artifact from this exact codebase's
    # own history: beer read 67.94F then 65.29F about 2 minutes later (see
    # cascade.py's module docstring) -- an implied rate of roughly -159F/h,
    # far beyond any real beer thermal mass. MAX_PLAUSIBLE_BEER_RATE_F_PER_H
    # (5.0) must cap the raw rate before it ever reaches the filter.
    dt_h = 2.0 / 60.0
    rate = update_closing_rate_filter(65.29, 66.0, 67.94, 66.0, 0.0, dt_h=dt_h)
    alpha = dt_h / (4.0 + dt_h)
    clamped_expected = alpha * (-5.0 - 0.0)  # clamped beer_rate=-5.0, target_rate=0.0
    assert abs(rate - clamped_expected) < 1e-9
    unclamped_would_have_been = alpha * ((65.29 - 67.94) / dt_h)
    assert rate > unclamped_would_have_been, "the clamp must meaningfully soften the raw artifact's implied rate"


def test_derivative_term_brakes_the_chamber_target_when_beer_is_closing_in_fast():
    closing_rate = 0.5  # beer closing in at 0.5F/h faster than the target itself moves
    braked = chamber_target_for(66.0, 66.0, 0.0, closing_rate)
    unbraked = chamber_target_for(66.0, 66.0, 0.0, 0.0)
    assert braked < unbraked  # -24.0*0.5 = -12.0F less than the unbraked (here: at-target) value -- stays
    assert braked == unbraked - 24.0 * 0.5  # well within the envelope, so unaffected by the safety clamp


def test_derivative_term_boosts_the_chamber_target_when_beer_is_falling_behind():
    closing_rate = -1.5  # beer falling behind the moving target by 1.5F/h
    boosted = chamber_target_for(66.0, 70.0, 0.0, closing_rate)
    unboosted = chamber_target_for(66.0, 70.0, 0.0, 0.0)
    assert boosted > unboosted  # +24.0*1.5 = +36.0F more than the unboosted value
