from __future__ import annotations

from pathlib import Path

from krauken.db import queries, writes
from krauken.db.connection import open_rw
from krauken.db.migrate import migrate


def _fresh_db(tmp_path: Path):
    db_path = tmp_path / "krauken.db"
    migrate(db_path)
    return open_rw(db_path)


def _start_fermentation(conn) -> int:
    profile_id = writes.create_profile(
        conn, name="test", yeast_id=None, yeast_name=None, definition=[], created_at="2026-01-01T00:00:00+00:00"
    )
    return writes.create_fermentation(
        conn, name="Test batch", profile_id=profile_id, started_at="2026-01-01T00:00:00+00:00", og=None,
        simulated=True, created_at="2026-01-01T00:00:00+00:00",
    )


def test_no_alerts_when_nothing_ever_went_unhealthy(tmp_path: Path):
    conn = _fresh_db(tmp_path)
    fermentation_id = _start_fermentation(conn)
    assert queries.active_alerts(conn, fermentation_id) == []


def test_lost_with_no_recovery_is_an_open_alert(tmp_path: Path):
    conn = _fresh_db(tmp_path)
    fermentation_id = _start_fermentation(conn)
    writes.record_event(
        conn, fermentation_id=fermentation_id, ts="2026-01-02T00:00:00+00:00", type="beer_temp_lost", severity="warning",
    )
    alerts = queries.active_alerts(conn, fermentation_id)
    assert len(alerts) == 1
    assert alerts[0]["field"] == "beer_temp"
    assert alerts[0]["since"] == "2026-01-02T00:00:00+00:00"


def test_lost_then_recovered_closes_the_alert(tmp_path: Path):
    conn = _fresh_db(tmp_path)
    fermentation_id = _start_fermentation(conn)
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T00:00:00+00:00", type="beer_temp_lost")
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T01:00:00+00:00", type="beer_temp_recovered")
    assert queries.active_alerts(conn, fermentation_id) == []


def test_lost_again_after_a_recovery_reopens_it_at_the_new_time(tmp_path: Path):
    conn = _fresh_db(tmp_path)
    fermentation_id = _start_fermentation(conn)
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T00:00:00+00:00", type="gravity_lost")
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T01:00:00+00:00", type="gravity_recovered")
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T02:00:00+00:00", type="gravity_lost")
    alerts = queries.active_alerts(conn, fermentation_id)
    assert len(alerts) == 1
    assert alerts[0]["since"] == "2026-01-02T02:00:00+00:00"


def test_multiple_open_alerts_at_once(tmp_path: Path):
    conn = _fresh_db(tmp_path)
    fermentation_id = _start_fermentation(conn)
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T00:00:00+00:00", type="beer_temp_lost")
    writes.record_event(conn, fermentation_id=fermentation_id, ts="2026-01-02T00:05:00+00:00", type="chamber_temp_lost")
    fields = {a["field"] for a in queries.active_alerts(conn, fermentation_id)}
    assert fields == {"beer_temp", "chamber_temp"}
