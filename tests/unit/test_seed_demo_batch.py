from __future__ import annotations

from pathlib import Path

import pytest

from krauken.db.connection import open_ro
from krauken.db.migrate import migrate
from krauken.db.seed import seed_demo_batch


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "krauken.db"
    migrate(db_path)
    seed_demo_batch(db_path)
    return db_path


def test_demo_fermentation_is_marked_demo_and_simulated_and_completed(seeded_db: Path):
    conn = open_ro(seeded_db)
    row = conn.execute("SELECT * FROM fermentations").fetchone()
    assert row["demo"] == 1
    assert row["simulated"] == 1
    assert row["status"] == "completed"
    assert row["ended_at"] is not None
    # og is auto-detected (contracts/og_detection.py), not hardcoded, so it
    # lands close to the simulator's true OG (1.052) but not exactly on it.
    assert row["og"] == pytest.approx(1.052, abs=0.01)
    assert 1.003 < row["fg"] < 1.008


def test_all_five_stages_present_in_order_and_finished(seeded_db: Path):
    conn = open_ro(seeded_db)
    stages = conn.execute("SELECT * FROM fermentation_stages ORDER BY seq").fetchall()
    assert [s["name"] for s in stages] == [
        "Primary fermentation", "Free rise", "Diacetyl rest", "Conditioning", "Cold crash",
    ]
    assert all(s["state"] == "finished" for s in stages)
    assert stages[0]["end_actual_reason"] == "gravity"
    assert all(s["end_actual_reason"] == "time" for s in stages[1:])


def test_stage_boundaries_are_contiguous_and_monotonic(seeded_db: Path):
    conn = open_ro(seeded_db)
    stages = conn.execute("SELECT started_at, ended_at FROM fermentation_stages ORDER BY seq").fetchall()
    for a, b in zip(stages, stages[1:]):
        assert a["ended_at"] == b["started_at"]


def test_samples_use_variable_interval_sampling_not_fixed_cadence(seeded_db: Path):
    conn = open_ro(seeded_db)
    reasons = {r["write_reason"] for r in conn.execute("SELECT DISTINCT write_reason FROM samples")}
    # A real exercise of the sampling policy produces all four reasons, not
    # just one -- if this ever collapses to a single reason, the policy
    # isn't actually being exercised by realistic data anymore.
    assert reasons == {"boot", "change", "heartbeat", "mode_change"}

    count = conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"]
    assert count > 500  # a real multi-week run at this sampling density


def test_no_sample_gap_exceeds_a_few_heartbeats(seeded_db: Path):
    # The storage policy's own invariant (design doc section 9): consecutive
    # written rows are never more than ~heartbeat_s apart. A couple of
    # heartbeats of slack accounts for stage-boundary rounding.
    import datetime

    conn = open_ro(seeded_db)
    rows = conn.execute("SELECT ts FROM samples ORDER BY ts").fetchall()
    timestamps = [datetime.datetime.fromisoformat(r["ts"]) for r in rows]
    max_gap_s = max((b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:]))
    assert max_gap_s <= 3 * 20 * 60


def test_beer_temp_and_gravity_stay_within_physically_sane_bounds(seeded_db: Path):
    conn = open_ro(seeded_db)
    rows = conn.execute("SELECT beer_temp_f, chamber_temp_f, gravity FROM samples").fetchall()
    assert all(20.0 < r["beer_temp_f"] < 100.0 for r in rows)
    assert all(20.0 < r["chamber_temp_f"] < 100.0 for r in rows)
    assert all(1.0 < r["gravity"] < 1.06 for r in rows)  # new terminal is 1.005, approached asymptotically


def test_seeding_twice_is_not_expected_to_be_idempotent_but_must_not_crash(tmp_path: Path):
    # seed_demo_batch() is a one-shot admin script, not designed to be
    # re-run against a DB that already has a demo batch -- calling it twice
    # produces two fermentations rather than erroring, since the unique-
    # active-fermentation constraint only applies to 'active' status, and
    # these are both 'completed'. Documenting this rather than asserting
    # idempotency, which was never a design goal here.
    db_path = tmp_path / "krauken.db"
    migrate(db_path)
    seed_demo_batch(db_path)
    seed_demo_batch(db_path)
    conn = open_ro(db_path)
    count = conn.execute("SELECT COUNT(*) c FROM fermentations").fetchone()["c"]
    assert count == 2
