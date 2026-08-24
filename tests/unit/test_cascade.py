from __future__ import annotations

from krauken.contracts.cascade import chamber_target_for, update_beer_error_integral

# Gain values as of this writing (contracts/control_constants.py):
# BEER_KP_F_PER_F=2.0, BEER_KI_F_PER_F_H=2.0, INTEGRAL_MAX_F_H=3.0,
# RAMP_FEEDFORWARD_COUPLING_PER_H=0.05, CHAMBER_TARGET_MIN_F=28.0,
# CHAMBER_TARGET_MAX_F=90.0 -- expected values below are computed directly
# from these, spelled out in each test's own comment, deliberately NOT
# imported symbolically: a test that recomputes its own expectation from
# whatever the live constant happens to be can't catch a real regression
# in the gain itself.


def test_holds_exactly_at_beer_target_with_zero_error_and_zero_integral():
    assert chamber_target_for(68.0, 68.0, 0.0) == 68.0


def test_proportional_term_pushes_chamber_above_target_when_beer_runs_cold():
    # error = 68.0 - 67.0 = 1.0 -> +2.0*1.0
    assert chamber_target_for(67.0, 68.0, 0.0) == 70.0


def test_proportional_term_pushes_chamber_below_target_when_beer_runs_warm():
    # error = 68.0 - 69.0 = -1.0 -> +2.0*(-1.0)
    assert chamber_target_for(69.0, 68.0, 0.0) == 66.0


def test_integral_term_alone_offsets_the_chamber_with_zero_instantaneous_error():
    # Beer sitting exactly AT target right now, but with an accumulated
    # history of having run warm (a negative integral) -- the correction
    # persists even though there's nothing to react to this instant,
    # exactly the property a pure P (or the old bang-bang) design lacked.
    assert chamber_target_for(68.0, 68.0, -1.5) == 65.0  # +2.0*(-1.5)


def test_update_beer_error_integral_accumulates_error_over_elapsed_time():
    integral = update_beer_error_integral(69.0, 68.0, 0.0, dt_h=0.5)  # error=-1.0
    assert integral == -0.5
    integral = update_beer_error_integral(69.0, 68.0, integral, dt_h=0.5)
    assert integral == -1.0


def test_update_beer_error_integral_anti_windup_clamps_the_positive_side():
    # 2.9 + (68.0-67.0)*1.0 = 3.9, clamped to the 3.0 ceiling.
    assert update_beer_error_integral(67.0, 68.0, 2.9, dt_h=1.0) == 3.0


def test_update_beer_error_integral_anti_windup_clamps_the_negative_side():
    # -2.9 + (68.0-69.0)*1.0 = -3.9, clamped to the -3.0 floor.
    assert update_beer_error_integral(69.0, 68.0, -2.9, dt_h=1.0) == -3.0


def test_ramp_feedforward_leaves_a_held_target_unchanged():
    # rate 0 (the default, and what a "constant" stage always has) -- no
    # feedforward contribution, same as a plain PI response.
    assert chamber_target_for(68.0, 68.0, 0.0, ramp_rate_f_per_h=0.0) == 68.0


def test_ramp_feedforward_pushes_the_chamber_further_below_a_downward_ramp():
    # A cold-crash-style ramp (68->38F over 96h -> -0.3125F/h) needs the
    # chamber running continuously below the MOVING target just to keep
    # the beer tracking it, on top of whatever the PI terms are doing
    # about present error (here, none -- beer sitting exactly at target,
    # zero integral). -0.3125 / 0.05 = -6.25.
    assert chamber_target_for(68.0, 68.0, 0.0, ramp_rate_f_per_h=-0.3125) == 68.0 - 6.25


def test_ramp_feedforward_is_symmetric_for_an_upward_ramp():
    assert chamber_target_for(68.0, 68.0, 0.0, ramp_rate_f_per_h=0.3125) == 68.0 + 6.25


def test_ramp_feedforward_stacks_additively_with_the_proportional_term():
    # error=+1.0 (beer running cold) -> +2.0, PLUS the same -6.25 ramp
    # feedforward as above -- additive, not a max()/replace() choice like
    # the old fixed-clamp design needed.
    assert chamber_target_for(67.0, 68.0, 0.0, ramp_rate_f_per_h=-0.3125) == 68.0 + 2.0 - 6.25


def test_clamps_to_the_absolute_safety_envelope():
    # An extreme combination -- max negative integral plus a huge,
    # badly-authored ramp -- must never ask real equipment for an absurd
    # target: 40.0 + 2.0*0 + 2.0*(-3.0) + (-5.0/0.05) = 40 - 6 - 100 = -66,
    # clamped to the 28.0 floor.
    assert chamber_target_for(40.0, 40.0, -3.0, ramp_rate_f_per_h=-5.0) == 28.0


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
        targets.append(chamber_target_for(beer_temp, beer_target, integral))

    assert all(earlier > later for earlier, later in zip(targets, targets[1:])), (
        "each successive target should be strictly more aggressive (lower) than the last"
    )
    p_only = beer_target + 2.0 * (beer_target - beer_temp)  # what pure P alone would produce: 67.4
    assert targets[-1] < p_only - 1.0, "the integral should meaningfully out-correct P alone given enough time"
