"""All read SQL. Used by both the API tier (read-only connection) and the
daemon (for rebuilding state on startup). No writes in this module -- see
writes.py, which is daemon-only by convention (enforced by
tests/db/test_write_boundary.py)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def live_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT as_of, payload FROM live_state WHERE id = 1").fetchone()
    return {"as_of": row["as_of"], "payload": json.loads(row["payload"])}


_ALERT_FIELDS = ("beer_temp", "chamber_temp", "gravity")
_ALERT_MESSAGES = {
    "beer_temp": "Beer temp reading is lost -- holding the last known chamber target rather than reacting to a stale value.",
    "chamber_temp": "Chamber temp reading is lost.",
    "gravity": "Gravity reading is lost -- a gravity-gated stage will fall back to its time cap instead.",
}


def active_alerts(conn: sqlite3.Connection, fermentation_id: int) -> list[dict[str, Any]]:
    """A field has an open alert if the MOST RECENT *_lost/*_recovered event
    for it is a *_lost with no later *_recovered -- i.e. control_loop.py's
    health-edge events (see its module docstring) haven't logged a recovery
    since. Walking events in ts order and keeping only the latest per field
    is simpler and just as correct as a fancier "last lost after last
    recovered" SQL query would be, for the event volumes one fermentation
    produces."""
    types = [f"{f}_lost" for f in _ALERT_FIELDS] + [f"{f}_recovered" for f in _ALERT_FIELDS]
    rows = conn.execute(
        f"SELECT type, ts FROM events WHERE fermentation_id = ? AND type IN ({','.join('?' * len(types))}) ORDER BY ts",
        (fermentation_id, *types),
    ).fetchall()

    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        field = r["type"].removesuffix("_lost").removesuffix("_recovered")
        latest[field] = {"type": r["type"], "ts": r["ts"]}

    return [
        {"field": field, "since": info["ts"], "message": _ALERT_MESSAGES[field]}
        for field, info in latest.items()
        if info["type"].endswith("_lost")
    ]


def setting(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row is not None else None


def hardware_mapping(conn: sqlite3.Connection) -> dict[str, Any]:
    """Reshaped hardware_config for the API's GET /hardware/mapping -- a
    plain table read with no business logic, so it goes straight to SQLite
    like any other read, never through the daemon (mapping_save, the WRITE
    side, is daemon-only since it runs the auto-resolve algorithm)."""
    return {
        "roles": {
            # hardware_config() already returns platform_config parsed (its
            # rows are plain dicts, not sqlite3.Row) -- re-parsing it here
            # was a real bug (json.loads on an already-decoded dict raises).
            r["role"]: {
                "device_id": r["device_id"],
                "platform": r["platform"],
                "platform_config": r["platform_config"],
            }
            for r in hardware_config(conn)
        }
    }


def hardware_config(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT role, platform, device_id, platform_config, updated_at "
        "FROM hardware_config ORDER BY role"
    ).fetchall()
    return [
        {
            "role": r["role"],
            "platform": r["platform"],
            "device_id": r["device_id"],
            "platform_config": json.loads(r["platform_config"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def active_fermentation(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM fermentations WHERE status = 'active' LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _with_abv(row: dict[str, Any]) -> dict[str, Any]:
    from krauken.contracts.units import abv_pct

    row = dict(row)
    row["abv_pct"] = abv_pct(row["og"], row["fg"]) if row["og"] is not None and row["fg"] is not None else None
    return row


def fermentations_list(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sql = (
        "SELECT f.*, p.yeast_name FROM fermentations f JOIN profiles p ON p.id = f.profile_id"
    )
    params: list[Any] = []
    if status is not None:
        sql += " WHERE f.status = ?"
        params.append(status)
    sql += " ORDER BY f.started_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_with_abv(dict(r)) for r in rows]


def fermentation_detail(conn: sqlite3.Connection, fermentation_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT f.*, p.yeast_name, p.name AS profile_name FROM fermentations f "
        "JOIN profiles p ON p.id = f.profile_id WHERE f.id = ?",
        (fermentation_id,),
    ).fetchone()
    if row is None:
        return None
    detail = _with_abv(dict(row))
    detail["stages"] = fermentation_stages(conn, fermentation_id)
    return detail


def fermentation_stages(conn: sqlite3.Connection, fermentation_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM fermentation_stages WHERE fermentation_id = ? ORDER BY seq", (fermentation_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def current_stage(conn: sqlite3.Connection, fermentation_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM fermentation_stages WHERE fermentation_id = ? AND state = 'running' LIMIT 1",
        (fermentation_id,),
    ).fetchone()
    return dict(row) if row else None


def latest_sample(conn: sqlite3.Connection, fermentation_id: int) -> dict[str, Any] | None:
    # The `ts >= started_at` guard excludes any sample stamped before this
    # fermentation began -- `fermentations.id` is a plain (non-AUTOINCREMENT)
    # INTEGER PRIMARY KEY, so SQLite reuses an id once its row is gone; a
    # prior fermentation's samples can outlive it if whatever deleted that
    # row didn't have foreign_keys enforcement on (the app's own connection
    # always does -- see db/connection.py -- but an ad hoc direct-SQL
    # cleanup against the db file might not). Without this guard, a reused
    # id would silently inherit a stale predecessor's leftover readings.
    row = conn.execute(
        "SELECT * FROM samples WHERE fermentation_id = ? "
        "AND ts >= (SELECT started_at FROM fermentations WHERE id = ?) "
        "ORDER BY ts DESC LIMIT 1",
        (fermentation_id, fermentation_id),
    ).fetchone()
    return dict(row) if row else None


def _compute_projection(conn: sqlite3.Connection, fermentation_id: int) -> dict[str, Any] | None:
    """None for anything but an active fermentation -- a completed or
    terminated batch has no "future" left to preview. See
    contracts/projection.py's module docstring for what this is (and isn't)
    an honest preview of."""
    import datetime

    from krauken.contracts.projection import project_forward

    fermentation_row = conn.execute("SELECT status, started_at FROM fermentations WHERE id = ?", (fermentation_id,)).fetchone()
    if fermentation_row is None or fermentation_row["status"] != "active":
        return None

    current = current_stage(conn, fermentation_id)
    if current is None:
        return None

    last = latest_sample(conn, fermentation_id)
    if last is None or last["beer_temp_f"] is None or last["chamber_temp_f"] is None:
        return None

    stages = fermentation_stages(conn, fermentation_id)
    # Anchor "now" to the last real sample's own timestamp, not wall-clock
    # time -- this function runs in the API process, which has no handle on
    # the daemon's Clock (see contracts/clock.py), so "now" here can only
    # mean "as of the most recent reading we actually have." That's also
    # the only anchor that stays internally consistent if the daemon's
    # clock has a dev-panel offset applied (KRAUKEN_DEV_PANEL's
    # OffsettableSystemClock): stage_started and the sample ts are both
    # stamped from that same (possibly offset) clock, so this subtraction
    # cancels the offset out; comparing against real wall time wouldn't.
    now = datetime.datetime.fromisoformat(last["ts"])
    stage_started = datetime.datetime.fromisoformat(current["started_at"])
    elapsed_h_into_current = max(0.0, (now - stage_started).total_seconds() / 3600.0)

    points = project_forward(
        beer_temp_f=last["beer_temp_f"], chamber_temp_f=last["chamber_temp_f"], gravity=last["gravity"],
        stages=stages, current_stage_seq=current["seq"], elapsed_h_into_current=elapsed_h_into_current,
    )
    return {
        "ts": [(now + datetime.timedelta(hours=p["t_h_from_now"])).isoformat() for p in points],
        "beer_temp_f": [p["beer_temp_f"] for p in points],
        "chamber_temp_f": [p["chamber_temp_f"] for p in points],
        "gravity": [p["gravity"] for p in points],
        "effective_target_f": [p["effective_target_f"] for p in points],
    }


def fermentation_series(
    conn: sqlite3.Connection, fermentation_id: int, *, heartbeat_s: float = 20 * 60
) -> dict[str, Any]:
    """Columnar (not array-of-objects) series data, plus SERVER-computed
    duty cycle and gaps -- not the client's job. Two things the mockup's
    client-side simulator gets away with that real, variable-interval data
    cannot: (1) duty cycle must be duration-weighted (mode.duration summed
    per mode / total duration), not row-counted -- row-counting silently
    overstates the duty of any period sampled more densely than another,
    which on-change sampling guarantees will happen; (2) a gap is a real
    signal the daemon stopped, computed once here so the chart can visibly
    break the line instead of every client re-deriving its own threshold.
    """
    import datetime

    # Same reused-id guard as latest_sample() above -- excludes any sample
    # stamped before this fermentation's own started_at.
    rows = conn.execute(
        "SELECT ts, beer_temp_f, chamber_temp_f, gravity, effective_target_f, chamber_mode, "
        "beer_temp_ok, target_source FROM samples WHERE fermentation_id = ? "
        "AND ts >= (SELECT started_at FROM fermentations WHERE id = ?) ORDER BY ts",
        (fermentation_id, fermentation_id),
    ).fetchall()

    if not rows:
        return {
            "fermentation_id": fermentation_id, "point_count": 0, "ts": [], "beer_temp_f": [],
            "chamber_temp_f": [], "gravity": [], "effective_target_f": [], "chamber_mode": [],
            "beer_temp_ok": [], "target_source": [], "projection": _compute_projection(conn, fermentation_id),
            "duty": {"window_hours": 0.0, "cool_pct": 0.0, "heat_pct": 0.0, "idle_pct": 0.0},
            "gaps": [],
        }

    parsed_ts = [datetime.datetime.fromisoformat(r["ts"]) for r in rows]
    duration_by_mode: dict[str, float] = {}
    gaps = []
    gap_threshold_s = 2.5 * heartbeat_s
    for i in range(len(rows) - 1):
        dt_s = (parsed_ts[i + 1] - parsed_ts[i]).total_seconds()
        mode = rows[i]["chamber_mode"]
        duration_by_mode[mode] = duration_by_mode.get(mode, 0.0) + dt_s
        if dt_s > gap_threshold_s:
            gaps.append({"from": rows[i]["ts"], "to": rows[i + 1]["ts"], "minutes": round(dt_s / 60, 1)})

    total_s = sum(duration_by_mode.values())

    def pct(mode: str) -> float:
        return round(100 * duration_by_mode.get(mode, 0.0) / total_s, 1) if total_s else 0.0

    return {
        "fermentation_id": fermentation_id,
        "point_count": len(rows),
        "ts": [r["ts"] for r in rows],
        "beer_temp_f": [r["beer_temp_f"] for r in rows],
        "chamber_temp_f": [r["chamber_temp_f"] for r in rows],
        "gravity": [r["gravity"] for r in rows],
        "effective_target_f": [r["effective_target_f"] for r in rows],
        "chamber_mode": [r["chamber_mode"] for r in rows],
        "beer_temp_ok": [bool(r["beer_temp_ok"]) for r in rows],
        "target_source": [r["target_source"] for r in rows],
        "projection": _compute_projection(conn, fermentation_id),
        "duty": {
            "window_hours": round(total_s / 3600, 2),
            "cool_pct": pct("cool"),
            "heat_pct": pct("heat"),
            "idle_pct": pct("idle"),
        },
        "gaps": gaps,
    }


def devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM devices ORDER BY device_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["capabilities"] = json.loads(d["capabilities"])
        d["metadata"] = json.loads(d["metadata"])
        d["last_reading"] = json.loads(d["last_reading"])
        out.append(d)
    return out


def device_by_id(conn: sqlite3.Connection, device_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["capabilities"] = json.loads(d["capabilities"])
    d["metadata"] = json.loads(d["metadata"])
    d["last_reading"] = json.loads(d["last_reading"])
    return d


def devices_as_candidates(conn: sqlite3.Connection) -> dict[str, "DeviceCandidate"]:
    """The cached `devices` table rows, as the same DeviceCandidate shape
    resolve() consumes -- so mapping resolution works against whatever was
    last seen in a scan without needing a live re-scan on every call."""
    from krauken.contracts.models import DeviceCandidate, Health
    from krauken.contracts.roles import CHAMBER_BUNDLE, Role

    out: dict[str, DeviceCandidate] = {}
    for d in devices(conn):
        out[d["device_id"]] = DeviceCandidate(
            device_id=d["device_id"],
            platform=d["platform"],
            display_name=d["name"],
            kind_label=d["kind"],
            capabilities=frozenset(Role(r) for r in d["capabilities"]),
            bundled_roles=CHAMBER_BUNDLE if d["is_bundle"] else frozenset(),
            health=Health(d["health"]),
            identity=d["metadata"],
            readings=d["last_reading"],
            last_seen_ts=None,
            simulated=bool(d["metadata"].get("simulated", False)),
        )
    return out
