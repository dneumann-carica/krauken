from __future__ import annotations

from krauken.platforms.simulator.projection import OPEN_ENDED_STAGE_HORIZON_H, project_forward

# One point per hour, not the real PROJECTION_STEP_H -- keeps the expected
# t_h_from_now values exact round numbers without touching production
# behavior (step_h is a plain parameter, not a global).
_STEP_H = 1.0


def _horizon_h(stage: dict, elapsed_h_into_current: float = 0.0) -> float:
    """The projected horizon for a single-stage schedule, read back off the
    last point it actually produced -- exercises _stage_horizon_h through
    project_forward's real public surface rather than reaching into the
    private helper directly."""
    full_stage = {"seq": 1, "id": 1, **stage}
    points = project_forward(
        beer_temp_f=68.0,
        chamber_temp_f=68.0,
        gravity=1.050,
        stages=[full_stage],
        current_stage_seq=1,
        elapsed_h_into_current=elapsed_h_into_current,
        step_h=_STEP_H,
    )
    return points[-1]["t_h_from_now"]


def test_time_end_mode_projects_exactly_its_end_hours():
    stage = {"temp_mode": "constant", "temp_f": 66.0, "end_mode": "time", "end_hours": 10.0, "max_hours": None}
    assert _horizon_h(stage) == 10.0


def test_max_hours_wins_outright_even_over_a_time_end_mode():
    # Mirrors stage_finished()'s own real rule (contracts/stages.py) -- an
    # earlier version of this function checked end_mode first and would
    # have ignored max_hours here, silently drifting the projection away
    # from the ribbon's own (max_hours-respecting) sizing.
    stage = {"temp_mode": "constant", "temp_f": 66.0, "end_mode": "time", "end_hours": 999.0, "max_hours": 5.0}
    assert _horizon_h(stage) == 5.0


def test_temp_hold_uses_its_own_hold_hours():
    stage = {
        "temp_mode": "constant", "temp_f": 66.0, "end_mode": "temp_hold",
        "hold_temp_f": 66.0, "hold_hours": 12.0, "max_hours": None,
    }
    assert _horizon_h(stage) == 12.0


def test_gravity_below_uses_its_own_hold_hours():
    stage = {
        "temp_mode": "constant", "temp_f": 66.0, "end_mode": "gravity_below",
        "gravity_hi": 1.010, "hold_hours": 8.0, "max_hours": None,
    }
    assert _horizon_h(stage) == 8.0


def test_gravity_end_mode_falls_back_to_the_shared_open_ended_constant():
    # "gravity" (flatness) has no hours-shaped field to size off at all --
    # this is the case that must match the frontend's own fallback exactly,
    # see OPEN_ENDED_STAGE_HORIZON_H's docstring.
    stage = {
        "temp_mode": "constant", "temp_f": 66.0, "end_mode": "gravity",
        "gravity_hi": 1.010, "gravity_stable_hours": 24.0, "max_hours": None,
    }
    assert _horizon_h(stage) == OPEN_ENDED_STAGE_HORIZON_H


def test_gravity_below_with_no_hold_hours_authored_falls_back_too():
    stage = {
        "temp_mode": "constant", "temp_f": 66.0, "end_mode": "gravity_below",
        "gravity_hi": 1.010, "hold_hours": None, "max_hours": None,
    }
    assert _horizon_h(stage) == OPEN_ENDED_STAGE_HORIZON_H


def test_elapsed_time_into_the_current_stage_shortens_the_remaining_horizon():
    # The stage's own horizon is still 10h total -- 4 of them already spent
    # before "now" -- so only 6h of projection remain to draw.
    stage = {"temp_mode": "constant", "temp_f": 66.0, "end_mode": "time", "end_hours": 10.0, "max_hours": None}
    assert _horizon_h(stage, elapsed_h_into_current=4.0) == 6.0


def _two_stage_points(current_advance_mode: str, elapsed_h_into_current: float = 0.0) -> list[dict]:
    current = {
        "seq": 1, "id": 1, "temp_mode": "constant", "temp_f": 66.0,
        "end_mode": "gravity", "gravity_hi": 1.010, "gravity_stable_hours": 24.0,
        "max_hours": None, "advance_mode": current_advance_mode,
    }
    nxt = {
        "seq": 2, "id": 2, "temp_mode": "constant", "temp_f": 70.0,
        "end_mode": "time", "end_hours": 10.0, "max_hours": None, "advance_mode": "auto",
    }
    return project_forward(
        beer_temp_f=66.0, chamber_temp_f=66.0, gravity=1.010,
        stages=[current, nxt], current_stage_seq=1,
        elapsed_h_into_current=elapsed_h_into_current, step_h=_STEP_H,
    )


def test_current_stage_projects_into_the_next_stage_once_its_own_horizon_is_spent():
    # gravity's own OPEN_ENDED_STAGE_HORIZON_H (96h) exhausted, then 10h more
    # into the next (70F) stage -- the 70F target shows up in the preview.
    # Same whether the current stage is auto- or manual-advance: the
    # projection always shows the WHOLE plan, never just the current stage.
    for mode in ("auto", "manual"):
        points = _two_stage_points(mode)
        assert any(p["effective_target_f"] == 70.0 for p in points)
        assert points[-1]["t_h_from_now"] == OPEN_ENDED_STAGE_HORIZON_H + 10.0


def test_current_stage_running_long_past_its_own_predicted_horizon_shows_the_next_stage_starting_right_at_now():
    # 200h into a stage whose own horizon guess (96h) has long since passed
    # -- a manual gate nobody's clicked, or a slow gravity read, etc. A
    # stage's predicted duration was never a hard fact, so once elapsed
    # time has already blown past it, there's nothing honest left to draw
    # for THIS stage -- continuing to show its own (flat) target a while
    # longer used to read as "there's more work left here" when the real
    # answer is "we don't know, it could end any moment." The preview
    # should show the NEXT stage's target starting immediately, not a
    # phantom continuation of the current one first.
    points = _two_stage_points("manual", elapsed_h_into_current=200.0)
    assert all(p["effective_target_f"] == 70.0 for p in points)
    assert points[-1]["t_h_from_now"] == 10.0


