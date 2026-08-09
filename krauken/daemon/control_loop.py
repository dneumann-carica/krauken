"""The real control loop (M2). One tick: read sensors, evaluate the current
stage's criteria, compute the beer-temp cascade, command the chamber
driver, auto-advance if criteria are met and the stage allows it, and
persist via the sampling policy. The Daemon runs this under
IPCServer.state_lock (see ipc/server.py's module docstring) so a tick never
interleaves with an in-progress mutating IPC op; ctx.db_lock additionally
guards the SQLite reads/writes here against a discovery scan's own
background write task, which state_lock doesn't cover (see
DaemonContext.db_lock's docstring).
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

from krauken.contracts import failsafe, og_detection, stages as stages_mod
from krauken.contracts.cascade import beer_relay_demand, chamber_target_for
from krauken.daemon import drivers, fermentation
from krauken.daemon.sampler import SampleCandidate, SamplingPolicy
from krauken.db import queries, writes

log = logging.getLogger("krauken.daemon.control_loop")

DEFAULT_CONTROL_TICK_INTERVAL_S = 30.0
_SAMPLING_POLICY = SamplingPolicy()

_HEALTH_EVENT_NAMES = {
    "beer_temp": ("beer_temp_lost", "beer_temp_recovered"),
    "chamber_temp": ("chamber_temp_lost", "chamber_temp_recovered"),
    "gravity": ("gravity_lost", "gravity_recovered"),
}


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _parse_iso(s: str) -> float:
    return datetime.datetime.fromisoformat(s).timestamp()


def _log_health_edge(ctx: Any, fermentation_id: int, field_name: str, ok: bool | None, now_iso: str) -> None:
    if ok is None:
        return  # unmapped -- not a health state, nothing to log
    prev = ctx.control_state.last_health.get(field_name)
    if prev is not None and prev != ok:
        lost_name, recovered_name = _HEALTH_EVENT_NAMES[field_name]
        writes.record_event(
            ctx.conn, fermentation_id=fermentation_id, ts=now_iso,
            type=recovered_name if ok else lost_name, severity="info" if ok else "warning", payload={},
        )
    ctx.control_state.last_health[field_name] = ok


async def control_tick(ctx: Any) -> None:
    async with ctx.db_lock:
        await _control_tick_locked(ctx)


async def _control_tick_locked(ctx: Any) -> None:
    fermentation_row = queries.active_fermentation(ctx.conn)
    if fermentation_row is None:
        return
    fermentation_id = fermentation_row["id"]

    current = queries.current_stage(ctx.conn, fermentation_id)
    if current is None:
        log.warning("fermentation %s is active with no running stage -- skipping tick", fermentation_id)
        return

    # Cheap and idempotent -- a no-op for Manual-driver setups (no ambient
    # concept at all) and harmless if the Simulator isn't actually the
    # mapped driver this tick.
    ctx.sim_engine.set_ambient_location(queries.setting(ctx.conn, "chamber_location"))

    hw = {r["role"]: r for r in queries.hardware_config(ctx.conn)}
    chamber = drivers.chamber_driver(ctx, hw["chamber_temp"]["platform"])
    beer_source = drivers.beer_temp_source(ctx, hw["beer_temp"]["platform"])
    gravity_mapped = hw["beer_gravity"]["platform"] is not None
    gravity_source = drivers.gravity_source(ctx, hw["beer_gravity"]["platform"]) if gravity_mapped else None

    now = ctx.clock.now()
    now_iso = _iso(now)
    beer_reading = await beer_source.read() if beer_source is not None else None
    chamber_reading = await chamber.read_chamber() if chamber is not None else None
    gravity_reading = await gravity_source.read() if gravity_source is not None else None

    health = failsafe.assess_health(
        now=now,
        beer_last_good_ts=beer_reading.last_good_ts if beer_reading else None,
        chamber_last_good_ts=chamber_reading.last_good_ts if chamber_reading else None,
        gravity_last_good_ts=gravity_reading.last_good_ts if gravity_reading else None,
        gravity_mapped=gravity_mapped,
    )
    _log_health_edge(ctx, fermentation_id, "beer_temp", health.beer_temp_ok, now_iso)
    _log_health_edge(ctx, fermentation_id, "chamber_temp", health.chamber_temp_ok, now_iso)
    _log_health_edge(ctx, fermentation_id, "gravity", health.gravity_ok, now_iso)

    absolute_h = (now - _parse_iso(fermentation_row["started_at"])) / 3600.0
    elapsed_h = (now - _parse_iso(current["started_at"])) / 3600.0
    beer_target = stages_mod.target_temp_f(current, elapsed_h)

    # OG auto-detection (contracts/og_detection.py): runs every tick,
    # independent of which stage is current, until it's locked in. A
    # fermentation started WITH an explicit og already has this column
    # non-null, so this block never runs for it -- explicit og always
    # wins over auto-detection.
    if fermentation_row["og"] is None and gravity_mapped:
        og_detector = ctx.control_state.og_detector
        if health.gravity_ok and gravity_reading is not None and gravity_reading.gravity_sg is not None:
            og_detector.update(absolute_h, gravity_reading.gravity_sg)
        else:
            og_detector.reset()
        if og_detector.locked_og is None and absolute_h >= og_detection.OG_DETECTION_MAX_H:
            og_detector.force_lock()
        if og_detector.locked_og is not None:
            writes.set_fermentation_og(ctx.conn, fermentation_id, og=og_detector.locked_og)
            writes.record_event(
                ctx.conn, fermentation_id=fermentation_id, ts=now_iso, type="og_locked",
                payload={"og": og_detector.locked_og},
            )

    beer_ok = health.beer_temp_ok and beer_reading is not None and beer_reading.temp_f is not None
    if beer_ok:
        mode = beer_relay_demand(beer_reading.temp_f, beer_target, ctx.control_state.last_relay_mode)
        chamber_target = chamber_target_for(mode, beer_target)
        target_source = "profile"
        ctx.control_state.last_relay_mode = mode
        ctx.control_state.last_chamber_target_f = chamber_target
    else:
        # Failsafe: beer temp is lost or absent -- hold the last commanded
        # chamber target rather than recompute a new one from a stale (or
        # missing) reading. See contracts/failsafe.py's module docstring.
        chamber_target = ctx.control_state.last_chamber_target_f
        target_source = "failsafe"

    if chamber is not None:
        await chamber.set_target(chamber_target)

    # Stage-advance gate (gravity/temp_hold only -- 'time' needs no gate).
    gate = ctx.control_state.gates.get(current["id"])
    if current["end_mode"] == "gravity":
        if gate is None:
            gate = stages_mod.GravityGate()
            ctx.control_state.gates[current["id"]] = gate
        if health.gravity_ok and gravity_reading is not None and gravity_reading.gravity_sg is not None:
            gate.update(absolute_h, gravity_reading.gravity_sg, current["gravity_stable_hours"])
        else:
            gate.reset()  # a stale/missing reading must not count toward "stable"
    elif current["end_mode"] == "temp_hold":
        if gate is None:
            gate = stages_mod.TempHoldGate()
            ctx.control_state.gates[current["id"]] = gate
        if beer_ok:
            gate.update(absolute_h, beer_reading.temp_f, current["hold_temp_f"])
        else:
            gate.reset()

    finished, reason = stages_mod.stage_finished(
        current, elapsed_h, absolute_h,
        gravity_gate=gate if current["end_mode"] == "gravity" else None,
        temp_hold_gate=gate if current["end_mode"] == "temp_hold" else None,
    )
    if finished:
        writes.mark_criteria_met(ctx.conn, current["id"], now_iso)
        if current["advance_mode"] == "auto":
            await fermentation.advance(ctx, fermentation_id, current, reason=reason, now=now_iso, criteria_met=False)

    candidate = SampleCandidate(
        ts=now,
        beer_temp_f=beer_reading.temp_f if beer_reading else None,
        chamber_temp_f=chamber_reading.temp_f if chamber_reading else None,
        gravity=gravity_reading.gravity_sg if gravity_reading else None,
        chamber_mode=chamber_reading.mode.value if chamber_reading else "unknown",
    )
    write_reason = _SAMPLING_POLICY.should_write(candidate, ctx.control_state.last_sample)
    if write_reason is not None:
        writes.insert_sample(
            ctx.conn, fermentation_id=fermentation_id, ts=now_iso, beer_temp_f=candidate.beer_temp_f,
            chamber_temp_f=candidate.chamber_temp_f, gravity=candidate.gravity, chamber_mode=candidate.chamber_mode,
            effective_target_f=beer_target, target_source=target_source, beer_temp_ok=health.beer_temp_ok,
            chamber_temp_ok=health.chamber_temp_ok, gravity_ok=health.gravity_ok, stage_id=current["id"],
            write_reason=write_reason,
        )
        ctx.control_state.last_sample = candidate

    writes.write_live_state(
        ctx.conn, now_iso,
        {
            "status": "alive",
            "fermentation_id": fermentation_id,
            "stage_id": current["id"],
            "stage_name": current["name"],
            "beer_temp_f": candidate.beer_temp_f,
            "chamber_temp_f": candidate.chamber_temp_f,
            "gravity": candidate.gravity,
            "chamber_mode": candidate.chamber_mode,
            "effective_target_f": beer_target,
            "target_source": target_source,
            "beer_temp_ok": health.beer_temp_ok,
            "chamber_temp_ok": health.chamber_temp_ok,
            "gravity_ok": health.gravity_ok,
        },
    )
