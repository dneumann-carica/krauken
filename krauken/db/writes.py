"""All write SQL. Imported ONLY by the daemon -- this is the single-writer
rule made structural. tests/db/test_write_boundary.py greps krauken.api for
imports of this module and fails the build if it finds one.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def write_live_state(conn: sqlite3.Connection, as_of: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE live_state SET as_of = ?, payload = ? WHERE id = 1",
        (as_of, json.dumps(payload)),
    )


def write_setting(conn: sqlite3.Connection, key: str, value: Any, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), updated_at),
    )


def upsert_device(conn: sqlite3.Connection, device: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO devices (device_id, platform, name, kind, capabilities, is_bundle,
                              health, first_seen_at, last_seen_at, metadata, last_reading)
        VALUES (:device_id, :platform, :name, :kind, :capabilities, :is_bundle,
                :health, :first_seen_at, :last_seen_at, :metadata, :last_reading)
        ON CONFLICT (device_id) DO UPDATE SET
            platform=excluded.platform, name=excluded.name, kind=excluded.kind,
            capabilities=excluded.capabilities, is_bundle=excluded.is_bundle,
            health=excluded.health, last_seen_at=excluded.last_seen_at,
            metadata=excluded.metadata, last_reading=excluded.last_reading
        """,
        {
            **device,
            "capabilities": json.dumps(device["capabilities"]),
            "metadata": json.dumps(device.get("metadata", {})),
            "last_reading": json.dumps(device.get("last_reading", {})),
        },
    )


def save_hardware_mapping(
    conn: sqlite3.Connection,
    roles: dict[str, tuple[str | None, str | None, dict[str, Any]]],
    updated_at: str,
) -> None:
    """roles: role -> (platform, device_id, platform_config). All 5 rows are
    always present (seeded by the initial migration) -- this is always an
    UPDATE over existing PK rows, never an insert/delete, so there's no
    window where the mapping is momentarily invalid."""
    for role, (platform, device_id, platform_config) in roles.items():
        conn.execute(
            "UPDATE hardware_config SET platform = ?, device_id = ?, platform_config = ?, updated_at = ? "
            "WHERE role = ?",
            (platform, device_id, json.dumps(platform_config), updated_at, role),
        )


def record_event(
    conn: sqlite3.Connection,
    *,
    fermentation_id: int | None,
    ts: str,
    type: str,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (fermentation_id, ts, type, severity, payload) VALUES (?, ?, ?, ?, ?)",
        (fermentation_id, ts, type, severity, json.dumps(payload or {})),
    )


def create_profile(
    conn: sqlite3.Connection, *, name: str, yeast_id: str | None, yeast_name: str | None,
    definition: list[dict[str, Any]], created_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO profiles (name, yeast_id, yeast_name, definition, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, yeast_id, yeast_name, json.dumps(definition), created_at, created_at),
    )
    return cur.lastrowid


def create_fermentation(
    conn: sqlite3.Connection, *, name: str, profile_id: int, started_at: str, og: float | None,
    simulated: bool, created_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO fermentations (name, profile_id, status, started_at, og, simulated, demo, created_at) "
        "VALUES (?, ?, 'active', ?, ?, ?, 0, ?)",
        (name, profile_id, started_at, og, int(simulated), created_at),
    )
    return cur.lastrowid


def set_fermentation_og(conn: sqlite3.Connection, fermentation_id: int, *, og: float) -> None:
    """Sets OG once it's been auto-detected (contracts/og_detection.py) --
    a fermentation started without an explicit OG has this column NULL
    until the control loop locks one in. `AND og IS NULL` mirrors
    mark_criteria_met's own idempotency style -- a safe no-op if this is
    ever somehow called twice for the same fermentation."""
    conn.execute("UPDATE fermentations SET og = ? WHERE id = ? AND og IS NULL", (og, fermentation_id))


def create_stage(
    conn: sqlite3.Connection, *, fermentation_id: int, seq: int, stage: dict[str, Any],
    state: str = "pending", started_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO fermentation_stages (fermentation_id, seq, stage_type, name, temp_mode, temp_f, "
        "temp_from_f, temp_to_f, ramp_hours, end_mode, end_hours, hold_temp_f, hold_hours, gravity_lo, "
        "gravity_hi, gravity_stable_hours, min_hours, max_hours, advance_mode, state, started_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fermentation_id, seq, stage["stage_type"], stage["name"], stage["temp_mode"], stage.get("temp_f"),
            stage.get("temp_from_f"), stage.get("temp_to_f"), stage.get("ramp_hours"), stage["end_mode"],
            stage.get("end_hours"), stage.get("hold_temp_f"), stage.get("hold_hours"), stage.get("gravity_lo"),
            stage.get("gravity_hi"), stage.get("gravity_stable_hours"), stage.get("min_hours"),
            stage.get("max_hours"), stage["advance_mode"], state, started_at,
        ),
    )
    return cur.lastrowid


def start_stage(conn: sqlite3.Connection, stage_id: int, started_at: str) -> None:
    conn.execute(
        "UPDATE fermentation_stages SET state = 'running', started_at = ? WHERE id = ?",
        (started_at, stage_id),
    )


def mark_criteria_met(conn: sqlite3.Connection, stage_id: int, ts: str) -> None:
    conn.execute(
        "UPDATE fermentation_stages SET criteria_met_at = ? WHERE id = ? AND criteria_met_at IS NULL",
        (ts, stage_id),
    )


def finish_stage(
    conn: sqlite3.Connection, stage_id: int, *, ended_at: str, end_actual_reason: str, finished_state: str = "finished",
) -> None:
    """Does NOT touch criteria_met_at -- call mark_criteria_met() yourself
    first if the criteria genuinely were met (the control loop does this the
    moment it detects it, regardless of advance_mode, so criteria_met_at
    reflects reality even when a manual-advance stage sits finished-but-
    waiting for a while first). A termination or an early manual override
    must NOT imply criteria were met."""
    conn.execute(
        "UPDATE fermentation_stages SET state = ?, ended_at = ?, end_actual_reason = ? WHERE id = ?",
        (finished_state, ended_at, end_actual_reason, stage_id),
    )


def reenable_stage(conn: sqlite3.Connection, stage_id: int) -> None:
    """Reverts a stage that was turned off via set_stage_enabled back to
    pending -- the only path that can call this already guarantees the
    stage never actually started, so there's no started_at to restore."""
    conn.execute(
        "UPDATE fermentation_stages SET state = 'pending', ended_at = NULL, end_actual_reason = NULL WHERE id = ?",
        (stage_id,),
    )


def update_stage_fields(conn: sqlite3.Connection, stage_id: int, fields: dict[str, Any]) -> None:
    """Backs the running-profile edit -- the sole intervention mechanism
    (overrides were struck; see the project plan's resolved decisions).
    `fields` is whatever subset of editable columns the caller validated."""
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    conn.execute(f"UPDATE fermentation_stages SET {set_clause} WHERE id = ?", (*fields.values(), stage_id))


def complete_fermentation(conn: sqlite3.Connection, fermentation_id: int, *, ended_at: str, fg: float | None) -> None:
    conn.execute(
        "UPDATE fermentations SET status = 'completed', ended_at = ?, fg = ? WHERE id = ?",
        (ended_at, fg, fermentation_id),
    )


def terminate_fermentation(
    conn: sqlite3.Connection, fermentation_id: int, *, ended_at: str, end_reason: str, fg: float | None,
) -> None:
    conn.execute(
        "UPDATE fermentations SET status = 'terminated', ended_at = ?, end_reason = ?, fg = ? WHERE id = ?",
        (ended_at, end_reason, fg, fermentation_id),
    )


def insert_sample(
    conn: sqlite3.Connection, *, fermentation_id: int, ts: str, beer_temp_f: float | None,
    chamber_temp_f: float | None, gravity: float | None, chamber_mode: str, effective_target_f: float | None,
    target_source: str, beer_temp_ok: bool, chamber_temp_ok: bool, gravity_ok: bool | None, stage_id: int | None,
    write_reason: str,
) -> None:
    conn.execute(
        "INSERT INTO samples (fermentation_id, ts, beer_temp_f, chamber_temp_f, gravity, chamber_mode, "
        "effective_target_f, target_source, beer_temp_ok, chamber_temp_ok, gravity_ok, stage_id, write_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fermentation_id, ts, beer_temp_f, chamber_temp_f, gravity, chamber_mode, effective_target_f,
            target_source, int(beer_temp_ok), int(chamber_temp_ok), None if gravity_ok is None else int(gravity_ok),
            stage_id, write_reason,
        ),
    )
