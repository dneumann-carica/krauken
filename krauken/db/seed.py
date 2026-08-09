"""Demo-batch generation. Runs the shipped US-05 yeast profile through the
plant model (platforms/simulator/plant.py) for its full duration and
persists real fermentations/fermentation_stages/samples/events rows -- not
fixtured data. The sampling policy (daemon/sampler.py) decides which
computed ticks actually become `samples` rows, so this demo batch exercises
the real variable-interval storage policy, not evenly-spaced fake data.

This is a one-shot administrative script (run once at build/deploy time,
via `krauken-db seed-demo`), not part of the live daemon's control loop --
calling datetime.now() directly here is fine; the "always use the injected
Clock" rule is specifically about the control loop's test-compression
requirement, which doesn't apply to a script that runs once and exits.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from krauken.contracts.stages import GravityGate, stage_finished, target_temp_f
from krauken.daemon.sampler import SampleCandidate, SamplingPolicy
from krauken.db.connection import open_rw
from krauken.platforms.simulator import plant

DT_H = 1.0 / 60.0  # 1 simulated minute per internal step
SAFETY_CAP_H = 800.0  # generation stops here even if a gate never satisfies (should never trigger)

# The five stages exactly as the mockup's blankPlan() defines them --
# primary is the only one that can't be disabled. max_hours is set on every
# stage as a sane authoring default, even though it is no longer a
# mandatory field (the safety-cap requirement was relaxed to match the
# shipped UI, which has no field for it -- see the project plan's resolved
# decisions).
DEMO_STAGES: list[dict[str, Any]] = [
    {
        "stage_type": "primary", "name": "Primary fermentation",
        "temp_mode": "constant", "temp_f": 66.0,
        "end_mode": "gravity", "gravity_hi": 1.016,
        "gravity_stable_hours": 24.0, "max_hours": 240.0,
        "advance_mode": "auto",
    },
    {
        "stage_type": "free_rise", "name": "Free rise",
        "temp_mode": "stepped", "temp_from_f": 66.0, "temp_to_f": 70.0, "ramp_hours": 24.0,
        "end_mode": "time", "end_hours": 24.0,
        "advance_mode": "auto",
    },
    {
        "stage_type": "diacetyl_rest", "name": "Diacetyl rest",
        "temp_mode": "constant", "temp_f": 70.0,
        "end_mode": "time", "end_hours": 48.0,
        "advance_mode": "auto",
    },
    {
        "stage_type": "conditioning", "name": "Conditioning",
        "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 168.0,
        "advance_mode": "auto",
    },
    {
        "stage_type": "cold_crash", "name": "Cold crash",
        "temp_mode": "stepped", "temp_from_f": 68.0, "temp_to_f": 38.0, "ramp_hours": 96.0,
        "end_mode": "time", "end_hours": 96.0,
        "advance_mode": "auto",
    },
]


def _simulate() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (stage_results, sample_rows). stage_results carries each
    stage's actual started/ended-at (as hours-from-start, converted to real
    timestamps by the caller) and end_actual_reason."""
    params = plant.PlantParams(total_hours=SAFETY_CAP_H)
    state = plant.initial_state(params)
    policy = SamplingPolicy()

    stage_results = []
    sample_rows = []
    last_written: SampleCandidate | None = None
    stage_start_h = 0.0
    gate = GravityGate()

    for stage_idx, stage in enumerate(DEMO_STAGES):
        while True:
            elapsed_h = state.t_h - stage_start_h
            target = target_temp_f(stage, elapsed_h)
            state = plant.step(state, params, DT_H, target)

            if stage["end_mode"] == "gravity":
                gate.update(state.t_h, state.gravity, stage["gravity_stable_hours"])

            candidate = SampleCandidate(
                ts=state.t_h * 3600.0, beer_temp_f=state.beer_temp_f, chamber_temp_f=state.chamber_temp_f,
                gravity=state.gravity, chamber_mode=state.mode,
            )
            reason = policy.should_write(candidate, last_written)
            if reason is not None:
                sample_rows.append(
                    {
                        "t_h": state.t_h, "beer_temp_f": state.beer_temp_f, "chamber_temp_f": state.chamber_temp_f,
                        "gravity": state.gravity, "chamber_mode": state.mode, "effective_target_f": target,
                        "write_reason": reason, "stage_idx": stage_idx,
                    }
                )
                last_written = candidate

            finished, reason_code = stage_finished(stage, elapsed_h, state.t_h, gravity_gate=gate)
            if finished or state.t_h >= SAFETY_CAP_H:
                stage_results.append({"stage_idx": stage_idx, "start_h": stage_start_h, "end_h": state.t_h, "end_actual_reason": reason_code})
                stage_start_h = state.t_h
                break

    return stage_results, sample_rows


def seed_demo_batch(db_path: Path | str) -> None:
    stage_results, sample_rows = _simulate()
    total_hours = stage_results[-1]["end_h"]

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    started_at = now - datetime.timedelta(hours=total_hours)

    def h_to_iso(h: float) -> str:
        return (started_at + datetime.timedelta(hours=h)).isoformat()

    yeasts = json.loads((Path(__file__).parent.parent / "data" / "yeasts.json").read_text())
    yeast = yeasts["us05"]

    conn = open_rw(db_path)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "INSERT INTO profiles (name, yeast_id, yeast_name, definition, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Citra Pale Ale demo profile", "us05", yeast["name"], json.dumps(DEMO_STAGES), h_to_iso(0), h_to_iso(0)),
        )
        profile_id = cur.lastrowid

        final_gravity = sample_rows[-1]["gravity"] if sample_rows else plant.PlantParams().gravity.terminal
        cur = conn.execute(
            "INSERT INTO fermentations (name, profile_id, status, started_at, ended_at, end_reason, "
            "og, fg, simulated, demo, created_at) VALUES (?, ?, 'completed', ?, ?, NULL, ?, ?, 1, 1, ?)",
            (
                "Sample batch - Citra Pale Ale", profile_id, h_to_iso(0), h_to_iso(total_hours),
                plant.PlantParams().gravity.og, round(final_gravity, 4), h_to_iso(0),
            ),
        )
        fermentation_id = cur.lastrowid

        stage_ids = []
        for sr in stage_results:
            stage = DEMO_STAGES[sr["stage_idx"]]
            cur = conn.execute(
                "INSERT INTO fermentation_stages (fermentation_id, seq, stage_type, name, temp_mode, temp_f, "
                "temp_from_f, temp_to_f, ramp_hours, end_mode, end_hours, hold_temp_f, hold_hours, gravity_lo, "
                "gravity_hi, gravity_stable_hours, min_hours, max_hours, advance_mode, state, started_at, "
                "ended_at, criteria_met_at, end_actual_reason) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'finished', ?, ?, ?, ?)",
                (
                    fermentation_id, sr["stage_idx"], stage["stage_type"], stage["name"], stage["temp_mode"],
                    stage.get("temp_f"), stage.get("temp_from_f"), stage.get("temp_to_f"), stage.get("ramp_hours"),
                    stage["end_mode"], stage.get("end_hours"), stage.get("hold_temp_f"), stage.get("hold_hours"),
                    stage.get("gravity_lo"), stage.get("gravity_hi"), stage.get("gravity_stable_hours"),
                    stage.get("min_hours"), stage.get("max_hours"), stage["advance_mode"],
                    h_to_iso(sr["start_h"]), h_to_iso(sr["end_h"]), h_to_iso(sr["end_h"]), sr["end_actual_reason"],
                ),
            )
            stage_ids.append(cur.lastrowid)
            conn.execute(
                "INSERT INTO events (fermentation_id, ts, type, severity, payload) VALUES (?, ?, 'stage_advanced', 'info', ?)",
                (fermentation_id, h_to_iso(sr["end_h"]), json.dumps({"reason": sr["end_actual_reason"], "stage": stage["name"]})),
            )

        for row in sample_rows:
            conn.execute(
                "INSERT INTO samples (fermentation_id, ts, beer_temp_f, chamber_temp_f, gravity, chamber_mode, "
                "effective_target_f, target_source, beer_temp_ok, chamber_temp_ok, gravity_ok, stage_id, write_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'profile', 1, 1, 1, ?, ?)",
                (
                    fermentation_id, h_to_iso(row["t_h"]), round(row["beer_temp_f"], 2), round(row["chamber_temp_f"], 2),
                    round(row["gravity"], 4), row["chamber_mode"], round(row["effective_target_f"], 2),
                    stage_ids[row["stage_idx"]], row["write_reason"],
                ),
            )

        conn.execute(
            "INSERT INTO events (fermentation_id, ts, type, severity, payload) VALUES (?, ?, 'fermentation_started', 'info', '{}')",
            (fermentation_id, h_to_iso(0)),
        )
        conn.execute(
            "INSERT INTO events (fermentation_id, ts, type, severity, payload) VALUES (?, ?, 'fermentation_completed', 'info', '{}')",
            (fermentation_id, h_to_iso(total_hours)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
