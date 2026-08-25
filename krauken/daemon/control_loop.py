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
from krauken.contracts.cascade import chamber_target_for, update_beer_error_integral
from krauken.daemon import drivers, fermentation
from krauken.daemon.sampler import SampleCandidate, SamplingPolicy
from krauken.daemon.timefmt import iso as _iso
from krauken.db import queries, writes

log = logging.getLogger("krauken.daemon.control_loop")

DEFAULT_CONTROL_TICK_INTERVAL_S = 30.0
_SAMPLING_POLICY = SamplingPolicy()

_HEALTH_EVENT_NAMES = {
    "beer_temp": ("beer_temp_lost", "beer_temp_recovered"),
    "chamber_temp": ("chamber_temp_lost", "chamber_temp_recovered"),
    "gravity": ("gravity_lost", "gravity_recovered"),
}


def _parse_iso(s: str) -> float:
    return datetime.datetime.fromisoformat(s).timestamp()


def _dt_h_since_last_tick(ctx: Any, now_mono: float) -> float:
    """Real elapsed hours since the previous tick THIS PROCESS has seen
    (ANY tick, fermentation active or not -- control_tick() runs every
    scheduled tick regardless, per its own comment), for the beer-temp
    PI's integral (contracts/cascade.py's update_beer_error_integral()).
    Deliberately takes an already-fetched now_mono rather than calling
    ctx.clock.monotonic() itself, so a caller that also needs `now` for
    other purposes only reads the clock once per tick.

    Monotonic, never wall-clock -- see contracts/clock.py's Clock.now()
    docstring on why wall-clock time (which an NTP correction can step)
    must never drive timer arithmetic. Returns 0.0 on the very first tick
    this process has ever run (ctx.control_state.last_tick_monotonic is
    still None), rather than fabricating/guessing an interval -- correct
    both right after a fresh daemon start and right after ControlState.
    reset() (a new fermentation), since both leave it None.

    Updates ctx.control_state.last_tick_monotonic as a side effect --
    unconditionally, every call, so a later tick always measures from
    the most recent tick that actually ran, not from whenever the
    integral itself last got to accumulate (see this function's own
    caller for why that distinction matters during a beer-sensor outage)."""
    last = ctx.control_state.last_tick_monotonic
    ctx.control_state.last_tick_monotonic = now_mono
    return (now_mono - last) / 3600.0 if last is not None else 0.0


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
    # Every scheduled tick, not just while a fermentation is active -- an
    # IPC-backed platform's clock must stay fresh even between batches
    # (dev-panel/identify-probes exercise it with nothing fermenting), and
    # _control_tick_locked below returns early exactly in that case. See
    # daemon/drivers.py's sync_remote_clocks and contracts/clock.py's
    # RemoteClock.
    await drivers.sync_remote_clocks(ctx)
    async with ctx.db_lock:
        await _control_tick_locked(ctx)


class _Drivers:
    """The role drivers this tick will read from/write to, resolved once
    per tick from whatever's currently mapped -- a plain holder, not a
    Protocol of its own, since callers just want the four fields."""

    __slots__ = ("chamber", "beer_source", "gravity_mapped", "gravity_source")

    def __init__(self, chamber: Any, beer_source: Any, gravity_mapped: bool, gravity_source: Any):
        self.chamber = chamber
        self.beer_source = beer_source
        self.gravity_mapped = gravity_mapped
        self.gravity_source = gravity_source


def _resolve_drivers(ctx: Any) -> _Drivers:
    hw = queries.hardware_config_by_role(ctx.conn)
    gravity_mapped = hw["beer_gravity"]["platform"] is not None
    return _Drivers(
        chamber=drivers.chamber_driver(ctx, hw["chamber_temp"]["platform"], hw["chamber_temp"]["device_id"]),
        beer_source=drivers.beer_temp_source(ctx, hw["beer_temp"]["platform"], hw["beer_temp"]["device_id"]),
        gravity_mapped=gravity_mapped,
        gravity_source=drivers.gravity_source(ctx, hw["beer_gravity"]["platform"], hw["beer_gravity"]["device_id"])
        if gravity_mapped
        else None,
    )


class _Readings:
    __slots__ = ("beer", "chamber", "gravity")

    def __init__(self, beer: Any, chamber: Any, gravity: Any):
        self.beer = beer
        self.chamber = chamber
        self.gravity = gravity


async def _read_all(d: _Drivers) -> _Readings:
    return _Readings(
        beer=await d.beer_source.read() if d.beer_source is not None else None,
        chamber=await d.chamber.read_chamber() if d.chamber is not None else None,
        gravity=await d.gravity_source.read() if d.gravity_source is not None else None,
    )


def _run_og_detection(
    ctx: Any, fermentation_row: Any, fermentation_id: int, gravity_mapped: bool,
    gravity_ok: bool, gravity_reading: Any, absolute_h: float, now_iso: str,
) -> None:
    """Auto-detects OG (contracts/og_detection.py) -- runs every tick,
    independent of which stage is current, until it's locked in. A
    fermentation started WITH an explicit og already has this column
    non-null, so this is a no-op for it -- explicit og always wins over
    auto-detection."""
    if fermentation_row["og"] is not None or not gravity_mapped:
        return
    og_detector = ctx.control_state.og_detector
    if gravity_ok:
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


# One shared shape (update(t_h, value, threshold) / reset()) covers all
# three gated end_modes -- see contracts/stages.py's GravityGate/
# TempHoldGate/GravityBelowGate -- so this table (which class, and how to
# get its current (ok, value, threshold)) is all that's left of what used
# to be three near-identical create/update/reset blocks. 'time' has no
# entry: it needs no gate at all.
_GATE_CLASSES: dict[str, Any] = {
    "gravity": stages_mod.GravityGate,
    "temp_hold": stages_mod.TempHoldGate,
    "gravity_below": stages_mod.GravityBelowGate,
}


def _update_stage_gate(
    ctx: Any, current: Any, *, beer_ok: bool, beer_temp_f: float | None,
    gravity_ok: bool, gravity_sg: float | None, absolute_h: float,
) -> Any | None:
    """Creates the gate on first use, updates it (or resets it, if this
    tick's relevant reading is unhealthy/missing -- a stale reading must
    never count toward "stable"), and returns it for stage_finished() to
    consult. None for 'time' end_mode, which needs no gate."""
    end_mode = current["end_mode"]
    gate_cls = _GATE_CLASSES.get(end_mode)
    if gate_cls is None:
        return None
    gate = ctx.control_state.gates.get(current["id"])
    if gate is None:
        gate = gate_cls()
        ctx.control_state.gates[current["id"]] = gate

    if end_mode == "temp_hold":
        ok, value, threshold = beer_ok, beer_temp_f, current["hold_temp_f"]
    else:
        threshold = current["gravity_stable_hours"] if end_mode == "gravity" else current["gravity_hi"]
        ok, value = gravity_ok, gravity_sg

    if ok:
        gate.update(absolute_h, value, threshold)
    else:
        gate.reset()
    return gate


async def _control_tick_locked(ctx: Any) -> None:
    fermentation_row = queries.active_fermentation(ctx.conn)
    if fermentation_row is None:
        return
    fermentation_id = fermentation_row["id"]

    current = queries.current_stage(ctx.conn, fermentation_id)
    if current is None:
        log.warning("fermentation %s is active with no running stage -- skipping tick", fermentation_id)
        return

    d = _resolve_drivers(ctx)
    # Cheap and idempotent -- a no-op for drivers with no ambient concept
    # at all (Manual) and harmless if the Simulator isn't actually the
    # mapped chamber driver this tick; see ChamberDriver.set_ambient_location's
    # own docstring for why the control loop calls this uniformly rather
    # than naming a specific platform.
    if d.chamber is not None:
        await d.chamber.set_ambient_location(queries.setting(ctx.conn, "chamber_location"))

    now = ctx.clock.now()
    now_iso = _iso(now)
    # dt_h for the PI integral -- see _dt_h_since_last_tick's own
    # docstring. Computed unconditionally, even while beer_ok turns out
    # False below, so a beer sensor dropping out for a while and then
    # recovering doesn't hand the integral a huge one-shot "catch-up" dt_h
    # once it's healthy again -- only the tick where the reading actually
    # resumes contributes anything, and it contributes a normal-sized dt_h.
    dt_h = _dt_h_since_last_tick(ctx, ctx.clock.monotonic())
    readings = await _read_all(d)

    health = failsafe.assess_health(
        now=now,
        beer_last_good_ts=readings.beer.last_good_ts if readings.beer else None,
        chamber_last_good_ts=readings.chamber.last_good_ts if readings.chamber else None,
        gravity_last_good_ts=readings.gravity.last_good_ts if readings.gravity else None,
        gravity_mapped=d.gravity_mapped,
    )
    _log_health_edge(ctx, fermentation_id, "beer_temp", health.beer_temp_ok, now_iso)
    _log_health_edge(ctx, fermentation_id, "chamber_temp", health.chamber_temp_ok, now_iso)
    _log_health_edge(ctx, fermentation_id, "gravity", health.gravity_ok, now_iso)

    absolute_h = (now - _parse_iso(fermentation_row["started_at"])) / 3600.0
    elapsed_h = (now - _parse_iso(current["started_at"])) / 3600.0
    beer_target = stages_mod.target_temp_f(current, elapsed_h)

    gravity_ok = health.gravity_ok and readings.gravity is not None and readings.gravity.gravity_sg is not None
    _run_og_detection(
        ctx, fermentation_row, fermentation_id, d.gravity_mapped,
        gravity_ok, readings.gravity, absolute_h, now_iso,
    )

    beer_ok = health.beer_temp_ok and readings.beer is not None and readings.beer.temp_f is not None
    if beer_ok:
        ctx.control_state.beer_error_integral = update_beer_error_integral(
            readings.beer.temp_f, beer_target, ctx.control_state.beer_error_integral, dt_h,
        )
        chamber_target = chamber_target_for(readings.beer.temp_f, beer_target, ctx.control_state.beer_error_integral)
        target_source = "profile"
        ctx.control_state.last_chamber_target_f = chamber_target
    else:
        # Failsafe: beer temp is lost or absent -- hold the last commanded
        # chamber target rather than recompute a new one from a stale (or
        # missing) reading. See contracts/failsafe.py's module docstring.
        chamber_target = ctx.control_state.last_chamber_target_f
        target_source = "failsafe"

    if d.chamber is not None:
        await d.chamber.set_target(chamber_target)

    gate = _update_stage_gate(
        ctx, current,
        beer_ok=beer_ok, beer_temp_f=readings.beer.temp_f if beer_ok else None,
        gravity_ok=gravity_ok, gravity_sg=readings.gravity.gravity_sg if gravity_ok else None,
        absolute_h=absolute_h,
    )
    finished, reason = stages_mod.stage_finished(
        current, elapsed_h, absolute_h,
        gravity_gate=gate if current["end_mode"] == "gravity" else None,
        temp_hold_gate=gate if current["end_mode"] == "temp_hold" else None,
        gravity_below_gate=gate if current["end_mode"] == "gravity_below" else None,
    )
    if finished:
        writes.mark_criteria_met(ctx.conn, current["id"], now_iso)
        if current["advance_mode"] == "auto":
            await fermentation.advance(ctx, fermentation_id, current, reason=reason, now=now_iso, criteria_met=False)

    candidate = SampleCandidate(
        ts=now,
        beer_temp_f=readings.beer.temp_f if readings.beer else None,
        chamber_temp_f=readings.chamber.temp_f if readings.chamber else None,
        gravity=readings.gravity.gravity_sg if readings.gravity else None,
        chamber_mode=readings.chamber.mode.value if readings.chamber else "unknown",
    )
    write_reason = _SAMPLING_POLICY.should_write(candidate, ctx.control_state.last_sample)
    if write_reason is not None:
        writes.insert_sample(
            ctx.conn, fermentation_id=fermentation_id, ts=now_iso, beer_temp_f=candidate.beer_temp_f,
            chamber_temp_f=candidate.chamber_temp_f, gravity=candidate.gravity, chamber_mode=candidate.chamber_mode,
            effective_target_f=beer_target, target_source=target_source, beer_temp_ok=health.beer_temp_ok,
            chamber_temp_ok=health.chamber_temp_ok, gravity_ok=health.gravity_ok, stage_id=current["id"],
            write_reason=write_reason, chamber_target_f=chamber_target,
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
            "chamber_target_f": chamber_target,
            "target_source": target_source,
            "beer_temp_ok": health.beer_temp_ok,
            "chamber_temp_ok": health.chamber_temp_ok,
            "gravity_ok": health.gravity_ok,
        },
    )
