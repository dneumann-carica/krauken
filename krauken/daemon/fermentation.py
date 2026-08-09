"""Fermentation lifecycle: start, terminate, manual stage-advance, and
editing a running profile's stages -- per the project's resolved decision,
editing the running profile is the SOLE intervention mechanism (the
override subsystem described in older planning docs was struck once the
UI grew an Edit Profile button; see the project plan's resolved decisions).

Shared with the control loop's own auto-advance path (advance() below), so
a criteria-met transition looks identical in the events/samples log
whether a human clicked "advance now" or the control loop fired it
automatically.
"""
from __future__ import annotations

import datetime
from typing import Any

from krauken.contracts.errors import (
    FermentationAlreadyActive,
    HardwareIncomplete,
    NoActiveFermentation,
    StageNotRunning,
    ValidationError,
)
from krauken.contracts.roles import Role, resolve
from krauken.db import queries, writes

EDITABLE_STAGE_FIELDS = {
    "name", "temp_mode", "temp_f", "temp_from_f", "temp_to_f", "ramp_hours", "end_mode", "end_hours",
    "hold_temp_f", "hold_hours", "gravity_lo", "gravity_hi", "gravity_stable_hours", "min_hours", "max_hours",
    "advance_mode",
}


def _iso_now(clock: Any) -> str:
    return datetime.datetime.fromtimestamp(clock.now(), tz=datetime.timezone.utc).isoformat()


def _is_simulated(devices: dict[str, Any], resolution: Any) -> bool:
    mapped_ids = {v for v in resolution.roles.values() if v is not None}
    return any(devices[d].simulated for d in mapped_ids if d in devices)


async def start_fermentation(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    stages = args.get("stages") or []
    if not stages:
        raise ValidationError("a fermentation needs at least one stage")

    async with ctx.db_lock:
        if queries.active_fermentation(ctx.conn) is not None:
            raise FermentationAlreadyActive("a fermentation is already active")

        devices = queries.devices_as_candidates(ctx.conn)
        draft = {Role(r["role"]): r["device_id"] for r in queries.hardware_config(ctx.conn)}
        resolution = resolve(draft, devices)
        if not resolution.valid:
            raise HardwareIncomplete(
                "hardware mapping is incomplete", {"blocking": [b.code for b in resolution.blocking]}
            )

        now = _iso_now(ctx.clock)
        profile_id = writes.create_profile(
            ctx.conn, name=f"{name} profile", yeast_id=args.get("yeast_id"), yeast_name=args.get("yeast_name"),
            definition=stages, created_at=now,
        )
        fermentation_id = writes.create_fermentation(
            ctx.conn, name=name, profile_id=profile_id, started_at=now, og=args.get("og"),
            simulated=_is_simulated(devices, resolution), created_at=now,
        )

        stage_ids = []
        for seq, stage in enumerate(stages):
            state = "running" if seq == 0 else "pending"
            started_at = now if seq == 0 else None
            stage_ids.append(
                writes.create_stage(ctx.conn, fermentation_id=fermentation_id, seq=seq, stage=stage, state=state, started_at=started_at)
            )

        writes.record_event(ctx.conn, fermentation_id=fermentation_id, ts=now, type="fermentation_started", payload={"name": name})

    ctx.control_state.reset()
    # Re-anchors the Simulator's plant to "now" and resets chamber/beer/relay
    # to their starting values -- see SimPlantEngine.reset_for_new_batch()'s
    # docstring for why this matters (without it, gravity/exotherm silently
    # carry forward from whenever the daemon process itself started, not
    # from this fermentation's actual start). Cheap and idempotent if
    # Simulator isn't even the mapped platform.
    ctx.sim_engine.reset_for_new_batch()
    return {"fermentation_id": fermentation_id, "profile_id": profile_id, "stage_ids": stage_ids}


async def advance(
    ctx: Any, fermentation_id: int, current: dict[str, Any], *, reason: str, now: str, criteria_met: bool,
) -> dict[str, Any] | None:
    """Finishes `current` and starts the next stage, or completes the
    fermentation if there is none. Caller already holds ctx.db_lock."""
    if criteria_met:
        writes.mark_criteria_met(ctx.conn, current["id"], now)
    writes.finish_stage(ctx.conn, current["id"], ended_at=now, end_actual_reason=reason)
    ctx.control_state.gates.pop(current["id"], None)

    stages = queries.fermentation_stages(ctx.conn, fermentation_id)
    # Skip over any stage the user turned off in the running-profile editor
    # (state == 'skipped', never started) -- it's already terminal, so
    # advancing must land on the next stage that's actually meant to run,
    # not silently un-skip it by starting it.
    next_stage = next((s for s in stages if s["seq"] > current["seq"] and s["state"] != "skipped"), None)
    if next_stage is not None:
        writes.start_stage(ctx.conn, next_stage["id"], now)
        writes.record_event(
            ctx.conn, fermentation_id=fermentation_id, ts=now, type="stage_advanced",
            payload={"from": current["name"], "to": next_stage["name"], "reason": reason},
        )
        return next_stage

    latest = queries.latest_sample(ctx.conn, fermentation_id)
    writes.complete_fermentation(ctx.conn, fermentation_id, ended_at=now, fg=(latest["gravity"] if latest else None))
    writes.record_event(ctx.conn, fermentation_id=fermentation_id, ts=now, type="fermentation_completed", payload={})
    return None


async def advance_stage_manual(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    fermentation_id = args["fermentation_id"]
    async with ctx.db_lock:
        fermentation = queries.active_fermentation(ctx.conn)
        if fermentation is None or fermentation["id"] != fermentation_id:
            raise NoActiveFermentation(f"fermentation {fermentation_id} is not active")
        current = queries.current_stage(ctx.conn, fermentation_id)
        if current is None:
            raise StageNotRunning("no stage is currently running")
        now = _iso_now(ctx.clock)
        # A manual advance's criteria_met_at reflects reality, not the act
        # of advancing -- mark_criteria_met was already called by the
        # control loop's own tick if the criteria genuinely were satisfied
        # before the user clicked; an early override leaves it unset.
        next_stage = await advance(ctx, fermentation_id, current, reason="manual", now=now, criteria_met=False)
    return {"advanced": True, "next_stage_id": next_stage["id"] if next_stage else None}


async def terminate_fermentation(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    fermentation_id = args["fermentation_id"]
    reason = args.get("reason") or "user_terminated"
    async with ctx.db_lock:
        fermentation = queries.active_fermentation(ctx.conn)
        if fermentation is None or fermentation["id"] != fermentation_id:
            raise NoActiveFermentation(f"fermentation {fermentation_id} is not active")
        now = _iso_now(ctx.clock)
        current = queries.current_stage(ctx.conn, fermentation_id)
        if current is not None:
            writes.finish_stage(ctx.conn, current["id"], ended_at=now, end_actual_reason="terminated", finished_state="skipped")
        latest = queries.latest_sample(ctx.conn, fermentation_id)
        writes.terminate_fermentation(
            ctx.conn, fermentation_id, ended_at=now, end_reason=reason, fg=(latest["gravity"] if latest else None)
        )
        writes.record_event(
            ctx.conn, fermentation_id=fermentation_id, ts=now, type="fermentation_terminated", severity="warning",
            payload={"reason": reason},
        )
    ctx.control_state.reset()
    return {"terminated": True}


async def update_stages(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Edits pending/running stages of the active fermentation -- the sole
    intervention mechanism. Already-finished stages are history and can't
    be rewritten; a stage's own state/timestamps are never editable this
    way (those are the control loop's to own)."""
    fermentation_id = args["fermentation_id"]
    updates: dict[int, dict[str, Any]] = {int(k): v for k, v in args["stages"].items()}

    async with ctx.db_lock:
        fermentation = queries.active_fermentation(ctx.conn)
        if fermentation is None or fermentation["id"] != fermentation_id:
            raise NoActiveFermentation(f"fermentation {fermentation_id} is not active")

        existing = {s["id"]: s for s in queries.fermentation_stages(ctx.conn, fermentation_id)}
        applied = []
        for stage_id, fields in updates.items():
            stage = existing.get(stage_id)
            if stage is None:
                raise ValidationError(f"stage {stage_id} does not belong to fermentation {fermentation_id}")
            if stage["state"] in ("finished", "skipped"):
                raise ValidationError(f"stage {stage_id!r} ({stage['name']}) is already {stage['state']} and can't be edited")
            unknown = set(fields) - EDITABLE_STAGE_FIELDS
            if unknown:
                raise ValidationError(f"stage {stage_id}: unsupported fields {sorted(unknown)}")
            writes.update_stage_fields(ctx.conn, stage_id, fields)
            applied.append(stage_id)

        now = _iso_now(ctx.clock)
        writes.record_event(
            ctx.conn, fermentation_id=fermentation_id, ts=now, type="profile_edited",
            payload={"stage_ids": applied},
        )
    return {"updated_stage_ids": applied}


async def set_stage_enabled(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """The running-profile editor's on/off switch for a stage that hasn't
    been reached yet -- turning one off marks it skipped without it ever
    having started; turning it back on reverts that. Deliberately narrow:
    only a stage that has never started (state == 'pending', or state ==
    'skipped' with no started_at, meaning it was turned off by this same
    mechanism) qualifies in either direction -- the active stage and
    anything finished/skipped through a real lifecycle event (criteria,
    manual advance, termination) are untouched by this."""
    fermentation_id = args["fermentation_id"]
    stage_id = args["stage_id"]
    enabled = args["enabled"]

    async with ctx.db_lock:
        fermentation_row = queries.active_fermentation(ctx.conn)
        if fermentation_row is None or fermentation_row["id"] != fermentation_id:
            raise NoActiveFermentation(f"fermentation {fermentation_id} is not active")

        stage = next((s for s in queries.fermentation_stages(ctx.conn, fermentation_id) if s["id"] == stage_id), None)
        if stage is None:
            raise ValidationError(f"stage {stage_id} does not belong to fermentation {fermentation_id}")

        now = _iso_now(ctx.clock)
        if not enabled:
            if stage["state"] != "pending":
                raise ValidationError(
                    f"stage {stage_id!r} ({stage['name']}) is not pending -- only a not-yet-reached stage can be turned off"
                )
            writes.finish_stage(ctx.conn, stage_id, ended_at=now, end_actual_reason="skipped", finished_state="skipped")
            event_type = "stage_skipped"
        else:
            if stage["state"] != "skipped" or stage["started_at"] is not None:
                raise ValidationError(
                    f"stage {stage_id!r} ({stage['name']}) can't be turned back on -- it isn't a not-yet-reached stage that was turned off"
                )
            writes.reenable_stage(ctx.conn, stage_id)
            event_type = "stage_reenabled"

        writes.record_event(
            ctx.conn, fermentation_id=fermentation_id, ts=now, type=event_type,
            payload={"stage_id": stage_id, "name": stage["name"]},
        )
    return {"stage_id": stage_id, "enabled": enabled}
