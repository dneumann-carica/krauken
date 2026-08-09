"""Direct-DB unit tests for db/queries.py's samples-reading functions --
specifically the reused-fermentation-id guard and the projection's time
anchor, both found via live investigation of a real dev instance rather
than guessed: `fermentations.id` is a plain INTEGER PRIMARY KEY (no
AUTOINCREMENT), so SQLite reuses an id once its row is gone. If whatever
deleted the old row didn't have PRAGMA foreign_keys=ON (the app's own
connection always does -- db/connection.py -- but a direct-SQL cleanup
against the db file might not), that old fermentation's `samples` rows
survive as orphans and get silently inherited by whichever new
fermentation next reuses the id.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from krauken.db import queries, writes
from krauken.db.connection import open_rw
from krauken.db.migrate import migrate

STAGE = {
    "stage_type": "primary", "name": "Primary", "temp_mode": "constant", "temp_f": 66.0,
    "end_mode": "time", "end_hours": 100.0, "advance_mode": "auto",
}


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "krauken.db"
    migrate(db_path)
    c = open_rw(db_path)
    yield c
    c.close()


def _make_active_fermentation(conn, *, started_at: str) -> tuple[int, int]:
    profile_id = writes.create_profile(
        conn, name="Test", yeast_id=None, yeast_name=None, definition=[STAGE], created_at=started_at,
    )
    fermentation_id = writes.create_fermentation(
        conn, name="Test batch", profile_id=profile_id, started_at=started_at, og=None,
        simulated=True, created_at=started_at,
    )
    stage_id = writes.create_stage(
        conn, fermentation_id=fermentation_id, seq=0, stage=STAGE, state="running", started_at=started_at,
    )
    return fermentation_id, stage_id


def _sample(conn, *, fermentation_id: int, ts: str, stage_id: int, beer=68.0, chamber=68.0, gravity=1.050):
    writes.insert_sample(
        conn, fermentation_id=fermentation_id, ts=ts, beer_temp_f=beer, chamber_temp_f=chamber, gravity=gravity,
        chamber_mode="idle", effective_target_f=66.0, target_source="profile", beer_temp_ok=True,
        chamber_temp_ok=True, gravity_ok=True, stage_id=stage_id, write_reason="change",
    )


def test_latest_sample_ignores_a_row_stamped_before_started_at(conn):
    fermentation_id, stage_id = _make_active_fermentation(conn, started_at="2026-08-05T00:00:00+00:00")
    # An orphaned sample from a previous fermentation that once held this
    # same (reused) id, timestamped a full day before this one started.
    _sample(conn, fermentation_id=fermentation_id, ts="2026-08-04T00:00:00+00:00", gravity=1.090, stage_id=stage_id)
    _sample(conn, fermentation_id=fermentation_id, ts="2026-08-05T01:00:00+00:00", gravity=1.048, stage_id=stage_id)

    latest = queries.latest_sample(conn, fermentation_id)
    assert latest["ts"] == "2026-08-05T01:00:00+00:00"
    assert latest["gravity"] == 1.048


def test_series_excludes_samples_from_before_started_at(conn):
    fermentation_id, stage_id = _make_active_fermentation(conn, started_at="2026-08-05T00:00:00+00:00")
    _sample(conn, fermentation_id=fermentation_id, ts="2026-08-04T00:00:00+00:00", gravity=1.090, stage_id=stage_id)
    _sample(conn, fermentation_id=fermentation_id, ts="2026-08-05T01:00:00+00:00", gravity=1.048, stage_id=stage_id)
    _sample(conn, fermentation_id=fermentation_id, ts="2026-08-05T02:00:00+00:00", gravity=1.047, stage_id=stage_id)

    series = queries.fermentation_series(conn, fermentation_id)
    assert series["point_count"] == 2
    assert series["ts"] == ["2026-08-05T01:00:00+00:00", "2026-08-05T02:00:00+00:00"]
    assert series["gravity"] == [1.048, 1.047]


def test_projection_anchors_to_the_last_sample_ts_not_wall_clock(conn):
    # started_at/sample ts are both set far from real wall-clock "now" --
    # if _compute_projection ever regresses to datetime.now(), the
    # projection's first point will land near real-world today instead of
    # near this fixture's fictional dates, and this assertion catches it.
    fermentation_id, stage_id = _make_active_fermentation(conn, started_at="2030-01-01T00:00:00+00:00")
    _sample(conn, fermentation_id=fermentation_id, ts="2030-01-01T06:00:00+00:00", stage_id=stage_id)

    series = queries.fermentation_series(conn, fermentation_id)
    proj = series["projection"]
    assert proj is not None
    assert proj["ts"][0].startswith("2030-01-01")
