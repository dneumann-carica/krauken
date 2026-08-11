"""Demo-batch generation. Runs the shipped US-05 yeast profile through a
REAL daemon -- the exact same build_daemon()/control_loop.py production
uses, with a SimulatorClock racing through time as fast as possible (see
daemon/testing.py's build_scenario_daemon()) -- rather than a separately-
invented physics approximation, so this demo batch is a genuine exercise
of whatever relay-timing protection, gravity-stability/OG-detection, and
plant physics are actually tuned into the system at any given moment, not
a second copy that silently drifts out of sync with them. (An earlier
version of this module did exactly that: it drove platforms/simulator/
plant.py's step() directly, in its own loop, with none of
contracts/protection.py's relay-timing state machine, none of the active-
vs-idle chamber coefficient split, no gravity jitter, and a hardcoded OG
-- every one of the physics fixes made against the real simulator this
project went through simply didn't apply to the shipped demo batch.)

Runs the simulation into a throwaway scratch database (so the hardware
scan/mapping it needs doesn't leak into hardware_config/devices in the
REAL target database -- a fresh install must still show "no hardware
mapped yet"), then copies just the resulting profile/fermentation/stages/
samples/events rows into the target db, with a timestamp shift so the
batch reads as "already finished, ending right about now," and fresh,
non-colliding ids -- the same fresh-id-per-table pattern this project's
own scratch-run import tooling already uses.

This is a one-shot administrative script (run once at build/deploy time,
via `krauken-db seed-demo`), not part of the live daemon's own control
loop -- calling datetime.now() directly here, and spinning up an entire
throwaway daemon+socket, is fine for a script that runs once and exits.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from krauken.daemon.testing import build_scenario_daemon
from krauken.db.connection import open_ro, open_rw
from krauken.ipc.client import AsyncIPCClient

# The five stages exactly as the mockup's blankPlan() defines them --
# primary is the only one that can't be disabled. max_hours is set on every
# stage as a sane authoring default, even though it is no longer a
# mandatory field (the safety-cap requirement was relaxed to match the
# shipped UI, which has no field for it -- see the project plan's resolved
# decisions).
DEMO_STAGES: list[dict[str, Any]] = [
    {
        "name": "Primary fermentation",
        "temp_mode": "constant", "temp_f": 66.0,
        "end_mode": "gravity", "gravity_hi": 1.016,
        "gravity_stable_hours": 24.0, "max_hours": 240.0,
        "advance_mode": "auto",
    },
    {
        "name": "Free rise",
        "temp_mode": "stepped", "temp_from_f": 66.0, "temp_to_f": 70.0, "ramp_hours": 24.0,
        "end_mode": "time", "end_hours": 24.0,
        "advance_mode": "auto",
    },
    {
        "name": "Diacetyl rest",
        "temp_mode": "constant", "temp_f": 70.0,
        "end_mode": "time", "end_hours": 48.0,
        "advance_mode": "auto",
    },
    {
        "name": "Conditioning",
        "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 168.0,
        "advance_mode": "auto",
    },
    {
        "name": "Cold crash",
        "temp_mode": "stepped", "temp_from_f": 68.0, "temp_to_f": 38.0, "ramp_hours": 96.0,
        "end_mode": "time", "end_hours": 96.0,
        "advance_mode": "auto",
    },
]

# Overall safety cap on simulated time, checked in the polling loop below --
# catches a stuck gate (gravity never stabilizing, OG never locking), not a
# normal completion path. Matches the old generator's own SAFETY_CAP_H.
MAX_SIMULATED_HOURS = 800.0
# 5 simulated minutes/tick -- coarse enough to keep tick count reasonable
# over a multi-week profile, fine enough to stay well under the sampler's
# gap-detection threshold (see tests/scenarios/test_full_fermentation.py's
# own docstring for the exact math this was tuned against).
CONTROL_TICK_INTERVAL_S = 300.0


async def _scan_and_wait(client: AsyncIPCClient) -> None:
    result = await client.call("hardware.scan_start")
    scan_id = result["scan_id"]
    for _ in range(50):
        status = await client.call("hardware.scan_status", {"scan_id": scan_id})
        if status["state"] == "complete":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("scan never completed")


async def _generate(scratch_db: Path, socket_path: Path) -> int:
    """Runs a real daemon through the demo profile inside `scratch_db`,
    end to end via IPC exactly as a real client would -- returns the
    resulting fermentation_id."""
    daemon, clock = build_scenario_daemon(
        db_path=scratch_db, socket_path=socket_path, control_tick_interval_s=CONTROL_TICK_INTERVAL_S,
    )
    await daemon.start()
    client = AsyncIPCClient(socket_path)
    try:
        await _scan_and_wait(client)
        mapping = await client.call(
            "hardware.mapping_save",
            {"roles": {
                "chamber_temp": "simulator:chamber",
                "beer_temp": "simulator:tilt",
                "beer_gravity": "simulator:tilt",
            }},
        )
        if not mapping["valid"]:
            raise RuntimeError(f"demo hardware mapping came back invalid: {mapping}")

        yeasts = json.loads((Path(__file__).parent.parent / "data" / "yeasts.json").read_text())
        yeast = yeasts["us05"]
        started = await client.call(
            "fermentation.start",
            {
                "name": "Sample batch - Citra Pale Ale", "stages": DEMO_STAGES,
                "yeast_id": "us05", "yeast_name": yeast["name"],
                # Auto-detected, same as any real fermentation started
                # without one -- see contracts/og_detection.py -- not
                # hardcoded, so the demo batch is an honest exercise of
                # that too.
                "og": None,
            },
        )
        fermentation_id = started["fermentation_id"]
        start_now = clock.now()

        while True:
            await asyncio.sleep(0.01)  # cooperative yield -- SimulatorClock does the actual compression
            async with daemon.ctx.db_lock:
                row = daemon.ctx.conn.execute(
                    "SELECT status FROM fermentations WHERE id = ?", (fermentation_id,)
                ).fetchone()
            if row["status"] != "active":
                break
            elapsed_h = (clock.now() - start_now) / 3600.0
            if elapsed_h > MAX_SIMULATED_HOURS:
                raise RuntimeError(
                    f"demo batch generation did not complete within {MAX_SIMULATED_HOURS} simulated "
                    f"hours (last status: {row['status']}) -- likely a stuck stage-advance or gate bug"
                )
        return fermentation_id
    finally:
        await daemon.stop()


def _next_id(conn, table: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM {table}").fetchone()
    return row["next_id"]


def _copy_into(scratch_db: Path, target_db_path: Path, fermentation_id: int) -> None:
    """Copies just the one fermentation's profile/stages/samples/events
    rows out of the scratch db into the target db, with fresh ids and a
    timestamp shift so the batch reads as ending right about now."""
    src = open_ro(scratch_db)
    fermentation = dict(src.execute("SELECT * FROM fermentations WHERE id = ?", (fermentation_id,)).fetchone())
    profile = dict(src.execute("SELECT * FROM profiles WHERE id = ?", (fermentation["profile_id"],)).fetchone())
    stages = [dict(r) for r in src.execute(
        "SELECT * FROM fermentation_stages WHERE fermentation_id = ? ORDER BY seq", (fermentation_id,)
    ).fetchall()]
    samples = [dict(r) for r in src.execute(
        "SELECT * FROM samples WHERE fermentation_id = ? ORDER BY ts", (fermentation_id,)
    ).fetchall()]
    events = [dict(r) for r in src.execute(
        "SELECT * FROM events WHERE fermentation_id = ? ORDER BY ts", (fermentation_id,)
    ).fetchall()]
    src.close()

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    shift = now - datetime.datetime.fromisoformat(fermentation["ended_at"])

    def shifted(ts: str | None) -> str | None:
        return (datetime.datetime.fromisoformat(ts) + shift).isoformat() if ts is not None else None

    dst = open_rw(target_db_path)
    try:
        new_profile_id = _next_id(dst, "profiles")
        new_fermentation_id = _next_id(dst, "fermentations")
        stage_id_map: dict[int, int] = {}
        next_stage_id = _next_id(dst, "fermentation_stages")
        for s in stages:
            stage_id_map[s["id"]] = next_stage_id
            next_stage_id += 1
        next_sample_id = _next_id(dst, "samples")
        next_event_id = _next_id(dst, "events")

        dst.execute("BEGIN")
        dst.execute(
            "INSERT INTO profiles (id, name, yeast_id, yeast_name, definition, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_profile_id, profile["name"], profile["yeast_id"], profile["yeast_name"],
             profile["definition"], shifted(profile["created_at"]), shifted(profile["updated_at"])),
        )
        dst.execute(
            "INSERT INTO fermentations (id, name, profile_id, status, started_at, ended_at, end_reason, og, fg, "
            "simulated, demo, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (new_fermentation_id, fermentation["name"], new_profile_id, fermentation["status"],
             shifted(fermentation["started_at"]), shifted(fermentation["ended_at"]), fermentation["end_reason"],
             fermentation["og"], fermentation["fg"], fermentation["simulated"], fermentation["notes"],
             shifted(fermentation["created_at"])),
        )
        for s in stages:
            dst.execute(
                "INSERT INTO fermentation_stages (id, fermentation_id, seq, name, temp_mode, temp_f, "
                "temp_from_f, temp_to_f, ramp_hours, end_mode, end_hours, hold_temp_f, hold_hours, "
                "gravity_hi, gravity_stable_hours, min_hours, max_hours, advance_mode, state, started_at, ended_at, "
                "criteria_met_at, end_actual_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stage_id_map[s["id"]], new_fermentation_id, s["seq"], s["name"], s["temp_mode"],
                 s["temp_f"], s["temp_from_f"], s["temp_to_f"], s["ramp_hours"], s["end_mode"], s["end_hours"],
                 s["hold_temp_f"], s["hold_hours"], s["gravity_hi"], s["gravity_stable_hours"],
                 s["min_hours"], s["max_hours"], s["advance_mode"], s["state"], shifted(s["started_at"]),
                 shifted(s["ended_at"]), shifted(s["criteria_met_at"]), s["end_actual_reason"]),
            )
        for s in samples:
            dst.execute(
                "INSERT INTO samples (id, fermentation_id, ts, beer_temp_f, chamber_temp_f, gravity, chamber_mode, "
                "effective_target_f, target_source, beer_temp_ok, chamber_temp_ok, gravity_ok, stage_id, "
                "write_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (next_sample_id, new_fermentation_id, shifted(s["ts"]), s["beer_temp_f"], s["chamber_temp_f"],
                 s["gravity"], s["chamber_mode"], s["effective_target_f"], s["target_source"], s["beer_temp_ok"],
                 s["chamber_temp_ok"], s["gravity_ok"], stage_id_map.get(s["stage_id"]), s["write_reason"]),
            )
            next_sample_id += 1
        for e in events:
            dst.execute(
                "INSERT INTO events (id, fermentation_id, ts, type, severity, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (next_event_id, new_fermentation_id, shifted(e["ts"]), e["type"], e["severity"], e["payload"]),
            )
            next_event_id += 1
        dst.execute("COMMIT")
    except Exception:
        dst.execute("ROLLBACK")
        raise
    finally:
        dst.close()


def seed_demo_batch(db_path: Path | str) -> None:
    db_path = Path(db_path)
    short_tmp = Path(tempfile.mkdtemp(prefix="krseed-"))  # AF_UNIX path-length limit -- see tests/api/conftest.py
    scratch_db = short_tmp / "scratch.db"
    socket_path = short_tmp / "d.sock"
    try:
        fermentation_id = asyncio.run(_generate(scratch_db, socket_path))
        _copy_into(scratch_db, db_path, fermentation_id)
    finally:
        shutil.rmtree(short_tmp, ignore_errors=True)
