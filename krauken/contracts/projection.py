"""Forward projection for the chart's live mode: a rough preview of where
beer/chamber temp are headed, computed server-side once so the physics
lives in exactly one place (contracts.cascade + the Simulator's plant
model) rather than being duplicated in TypeScript, per the project plan's
explicit M2 decision.

This is a preview, not a literal prediction, in two ways:
- It ignores relay-timing protection (min-on/min-off/lockout) -- a dashed
  preview line doesn't need to model exactly when a compressor is allowed
  to click on, and the original UI mockup's own client-side preview made
  the same simplification.
- It reuses the Simulator's thermal coefficients (heat-transfer, beer/
  chamber coupling, ambient, exotherm) as the physics. That's honestly
  applicable today because every fermentation this software can currently
  produce IS running on the Simulator (no real hardware driver exists
  yet) -- once a real Krauken/BrewPi driver lands, projecting a REAL
  batch's temperature with fictional simulator coefficients would need
  revisiting, but that's a problem for that milestone, not this one.

Gravity is deliberately NOT projected via the plant model's sigmoid
(gravity_at) -- that curve is fit to specific og/terminal/midpoint/
steepness values that have nothing to do with a real user's actual yeast
and batch, so extrapolating it would be a genuine fabrication, not an
approximation. The projection simply holds gravity flat at its last known
value.
"""
from __future__ import annotations

from typing import Any

from krauken.contracts.stages import target_rate_f_per_h, target_temp_f
from krauken.platforms.simulator import plant

PROJECTION_STEP_H = 0.5
# How far past a gravity/gravity_below-with-no-hold-info stage's own start
# to keep projecting if it has no max_hours set -- purely bounds how far the
# dashed line reaches, not a claim about when that stage will actually
# finish. MUST match the frontend's own UNKNOWN_DURATION_FALLBACK_HOURS
# (chart/geometry.ts) -- that constant sizes the same stage's ribbon
# segment from the same authored fields, so a mismatch here doesn't just
# affect this preview's own accuracy, it makes the dashed projection visibly
# drift away from the ribbon boundaries it's supposed to line up with.
OPEN_ENDED_STAGE_HORIZON_H = 96.0


def _stage_horizon_h(stage: dict[str, Any]) -> float:
    """Mirrors chart/geometry.ts's plannedDurationHours exactly (same field
    priority, same fallback) -- the two are independent implementations (no
    shared runtime between the Python backend and the TS frontend), so this
    docstring's fidelity to that function is the ONLY thing keeping the
    projection's dashed lines aligned with the ribbon's stage boundaries.

    max_hours wins outright regardless of end_mode, matching
    stage_finished()'s own real completion rule (contracts/stages.py) --
    checking end_mode first, as an earlier version of this function did,
    would ignore a max_hours cap set alongside a time-based end_mode."""
    if stage.get("max_hours") is not None:
        return stage["max_hours"]
    if stage["end_mode"] == "time":
        return stage["end_hours"] if stage["end_hours"] is not None else OPEN_ENDED_STAGE_HORIZON_H
    if stage["end_mode"] in ("temp_hold", "gravity_below"):
        return stage.get("hold_hours") or OPEN_ENDED_STAGE_HORIZON_H
    return OPEN_ENDED_STAGE_HORIZON_H


def project_forward(
    *,
    beer_temp_f: float,
    chamber_temp_f: float,
    gravity: float | None,
    stages: list[dict[str, Any]],
    current_stage_seq: int,
    elapsed_h_into_current: float,
    step_h: float = PROJECTION_STEP_H,
) -> list[dict[str, Any]]:
    """Returns points {t_h_from_now, beer_temp_f, chamber_temp_f, gravity,
    effective_target_f} stepping forward from "now" through the end of the
    last stage in `stages` (the current one, plus every pending one after
    it)."""
    params = plant.PlantParams()
    state = plant.PlantState(t_h=0.0, beer_temp_f=beer_temp_f, chamber_temp_f=chamber_temp_f, gravity=gravity or 1.0, mode="idle")

    points: list[dict[str, Any]] = []
    remaining = [s for s in stages if s["seq"] >= current_stage_seq]
    for i, stage in enumerate(remaining):
        t_in_stage = elapsed_h_into_current if i == 0 else 0.0
        horizon_h = _stage_horizon_h(stage)
        # No stretching the current stage's OWN target past "now" even
        # once elapsed time has already blown past its predicted horizon
        # (a manual gate nobody's clicked, a slow gravity read, ...) --
        # that used to draw a flat continuation of the current stage right
        # where the next one should visually begin, reading as "there's
        # more work left in this stage" when the honest answer is "we
        # don't know, it could end at any moment." t_in_stage already >=
        # horizon_h here just means zero more points from this stage --
        # the loop below falls through immediately and the NEXT stage's
        # preview begins right at "now" instead. See chart/geometry.ts's
        # projectStageSchedule, which clamps the ribbon segment to end at
        # "now" the same way, so the two stay aligned.
        while t_in_stage < horizon_h:
            target = target_temp_f(stage, t_in_stage)
            rate = target_rate_f_per_h(stage, t_in_stage)
            state = plant.step(state, params, step_h, target, rate)
            t_in_stage += step_h
            points.append(
                {
                    "t_h_from_now": state.t_h,
                    "beer_temp_f": state.beer_temp_f,
                    "chamber_temp_f": state.chamber_temp_f,
                    "gravity": gravity,  # held flat -- see module docstring
                    "effective_target_f": target,
                }
            )
    return points
