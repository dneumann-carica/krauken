from __future__ import annotations

from krauken.contracts.stages import GravityGate, TempHoldGate, stage_finished, target_temp_f


def test_target_temp_constant():
    stage = {"temp_mode": "constant", "temp_f": 66.0}
    assert target_temp_f(stage, elapsed_h=5.0) == 66.0


def test_target_temp_ramps_linearly_then_clamps_at_the_end():
    stage = {"temp_mode": "stepped", "temp_from_f": 66.0, "temp_to_f": 70.0, "ramp_hours": 24.0}
    assert target_temp_f(stage, elapsed_h=0.0) == 66.0
    assert target_temp_f(stage, elapsed_h=12.0) == 68.0
    assert target_temp_f(stage, elapsed_h=100.0) == 70.0  # past ramp_hours -- clamped, not extrapolated


def test_time_end_mode():
    stage = {"end_mode": "time", "end_hours": 48.0}
    assert stage_finished(stage, elapsed_h=47.9, t_h=47.9) == (False, "time")
    assert stage_finished(stage, elapsed_h=48.0, t_h=48.0) == (True, "time")


def test_max_hours_wins_outright_even_mid_time_stage():
    stage = {"end_mode": "time", "end_hours": 999.0, "max_hours": 48.0}
    assert stage_finished(stage, elapsed_h=48.0, t_h=48.0) == (True, "max_cap")


def test_min_hours_blocks_completion_even_if_criteria_already_look_satisfied():
    stage = {"end_mode": "time", "end_hours": 10.0, "min_hours": 20.0}
    finished, _ = stage_finished(stage, elapsed_h=15.0, t_h=15.0)
    assert finished is False


def test_gravity_end_mode_requires_the_gate():
    """"Stable" means self-relative flatness now, not "below a fixed line"
    -- see GravityGate's docstring. A single early reading isn't enough
    history to call anything flat yet; a full gravity_stable_hours window
    of tight readings is."""
    stage = {"end_mode": "gravity", "gravity_hi": 1.016, "gravity_stable_hours": 24.0}
    gate = GravityGate()
    gate.update(t_h=0.0, gravity=1.012, stable_hours=24.0)
    finished, reason = stage_finished(stage, elapsed_h=0.0, t_h=0.0, gravity_gate=gate)
    assert (finished, reason) == (False, "gravity")  # not enough history yet

    for t in (6.0, 12.0, 18.0, 24.0):
        gate.update(t_h=t, gravity=1.012, stable_hours=24.0)
    finished, reason = stage_finished(stage, elapsed_h=24.0, t_h=24.0, gravity_gate=gate)
    assert (finished, reason) == (True, "gravity")


def test_gravity_gate_still_moving_is_not_flat_even_if_already_below_threshold():
    """A genuine continued decline shows up as spread across the window
    exactly like sensor noise would -- it's not "flat" just because every
    individual step is small, and not "flat" just because every value
    already happens to sit below gravity_hi."""
    gate = GravityGate()
    gravity = 1.014
    for t in (0.0, 6.0, 12.0, 18.0, 24.0):
        gate.update(t_h=t, gravity=gravity, stable_hours=24.0)
        gravity -= 0.002  # still steadily dropping the whole window, well under gravity_hi throughout
    assert gate.satisfied(t_h=24.0, stable_hours=24.0, threshold=1.016) is False


def test_gravity_gate_a_stall_above_threshold_is_flat_but_not_satisfied():
    """The safety-net case this whole redesign exists for: gravity
    plateaus early, well above where the profile expects it to finish --
    genuinely flat by the self-relative check, but that must never be
    mistaken for "fermentation is done" just because it's flat."""
    stage = {"end_mode": "gravity", "gravity_hi": 1.016, "gravity_stable_hours": 24.0}
    gate = GravityGate()
    for t in (0.0, 6.0, 12.0, 18.0, 24.0):
        gate.update(t_h=t, gravity=1.030, stable_hours=24.0)  # stalled, well above gravity_hi
    finished, reason = stage_finished(stage, elapsed_h=24.0, t_h=24.0, gravity_gate=gate)
    assert (finished, reason) == (False, "gravity")


def test_gravity_gate_satisfied_well_below_threshold_too():
    gate = GravityGate()
    for t in (0.0, 6.0, 12.0, 18.0, 24.0):
        gate.update(t_h=t, gravity=1.008, stable_hours=24.0)  # well below the threshold is still fine
    assert gate.satisfied(t_h=24.0, stable_hours=24.0, threshold=1.016) is True


def test_temp_hold_end_mode():
    stage = {"end_mode": "temp_hold", "hold_temp_f": 70.0, "hold_hours": 6.0}
    gate = TempHoldGate()
    gate.update(t_h=0.0, beer_temp_f=70.2, hold_temp_f=70.0)  # within the trigger band
    finished, reason = stage_finished(stage, elapsed_h=0.0, t_h=0.0, temp_hold_gate=gate)
    assert (finished, reason) == (False, "temp_hold")

    finished, reason = stage_finished(stage, elapsed_h=6.0, t_h=6.0, temp_hold_gate=gate)
    assert (finished, reason) == (True, "temp_hold")


def test_temp_hold_gate_resets_when_beer_drifts_outside_the_band():
    gate = TempHoldGate()
    gate.update(t_h=0.0, beer_temp_f=70.0, hold_temp_f=70.0)
    gate.update(t_h=3.0, beer_temp_f=71.5, hold_temp_f=70.0)  # drifted out -- resets
    assert gate.satisfied(t_h=6.0, hold_hours=6.0) is False
