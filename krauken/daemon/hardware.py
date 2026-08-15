"""Hardware role-mapping: read the current mapping, and save a new one
through the auto-resolve algorithm in contracts/roles.py. Also the mapped
chamber's own on/off status, independent of any specific fermentation --
see chamber_status()/stop_chamber() below.
"""
from __future__ import annotations

from typing import Any

from krauken.contracts.errors import FermentationAlreadyActive
from krauken.contracts.roles import ALL_ROLES, Role, resolve
from krauken.daemon import drivers
from krauken.daemon.timefmt import iso_now as _iso_now
from krauken.db import queries, writes


async def save_mapping(ctx: Any, draft: dict[str, str | None]) -> dict[str, Any]:
    """draft: role value -> device_id or None. Resolves through the
    auto-resolve algorithm, always writes whatever the resolution produced
    (an unrepresentable split is impossible after resolve(), so there's
    nothing left to reject except genuinely missing/invalid data), and
    reports what happened either way."""
    devices = queries.devices_as_candidates(ctx.conn)
    typed_draft = {Role(k): v for k, v in draft.items()}
    result = resolve(typed_draft, devices)

    now = _iso_now(ctx.clock)
    to_write: dict[str, tuple[str | None, str | None, dict[str, Any]]] = {}
    for role in ALL_ROLES:
        device_id = result.roles.get(role)
        if device_id is None:
            to_write[role.value] = (None, None, {})
        else:
            dev = devices.get(device_id)
            to_write[role.value] = (dev.platform if dev else None, device_id, dict(dev.platform_config) if dev else {})

    async with ctx.db_lock:
        writes.save_hardware_mapping(ctx.conn, to_write, now)

    return {
        "valid": result.valid,
        "roles": {role.value: result.roles.get(role) for role in ALL_ROLES},
        "auto_resolved": [
            {
                "device_id": n.device_id,
                "device_name": n.device_name,
                "roles_cleared": [r.value for r in n.roles_cleared],
                "reason": n.reason,
                "message": n.message,
            }
            for n in result.auto_resolved
        ],
        "blocking": [{"code": b.code, "message": b.message, "details": b.details} for b in result.blocking],
        "warnings": [{"code": w.code, "message": w.message, "details": w.details} for w in result.warnings],
    }


def _mapped_chamber(ctx: Any):
    hw = queries.hardware_config_by_role(ctx.conn)
    chamber_role = hw.get("chamber_temp")
    return drivers.chamber_driver(ctx, chamber_role["platform"], chamber_role["device_id"]) if chamber_role else None


async def chamber_status(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Whatever the mapped chamber driver is currently holding as its
    target, right now -- a pure peek (ChamberDriver.commanded_target(), not
    read_chamber()), so this is safe to poll on demand with none of the
    timing concerns a physics-advancing read would raise (see that
    method's own docstring). Independent of any specific fermentation --
    there's exactly one physical chamber, and whether it's currently
    holding a setpoint is a fact about IT, not about whichever batch last
    told it to. Backs the UI's "chamber is on with nothing fermenting"
    banner; stop_chamber() below is its matching release action."""
    async with ctx.db_lock:
        chamber = _mapped_chamber(ctx)
        commanded_target_f = await chamber.commanded_target() if chamber is not None else None
    return {"commanded_target_f": commanded_target_f, "mapped": chamber is not None}


async def stop_chamber(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Explicitly tells the mapped chamber driver to stop maintaining
    whatever it's currently holding (ChamberDriver.set_target(None) -- "no
    target, de-energize," per that method's own docstring). Nothing does
    this on its own: once a fermentation completes/terminates, the control
    loop just stops ticking for it entirely (control_loop.py's module
    docstring), leaving the chamber sitting at whatever it was last
    commanded to, indefinitely -- on the simulator it just freezes (nothing
    left calling read_chamber() to advance it at all); on real hardware the
    Hardware Supervisor's own protection loop keeps independently holding
    that stale target forever, since it has no notion of "the batch ended."
    This is the deliberate, separate, user-triggered release valve for
    that.

    Blocked while ANY fermentation is active -- "exactly one active batch
    at a time" is a DB-level invariant, so checking that generically (not
    "is some specific fermentation active") is sufficient to guarantee
    this can never de-energize a chamber a real, running fermentation
    still needs."""
    async with ctx.db_lock:
        if queries.active_fermentation(ctx.conn) is not None:
            raise FermentationAlreadyActive("a fermentation is active -- the chamber is still needed")
        chamber = _mapped_chamber(ctx)
        if chamber is not None:
            await chamber.set_target(None)
        now = _iso_now(ctx.clock)
        writes.record_event(ctx.conn, fermentation_id=None, ts=now, type="chamber_stopped", payload={})
    return {"stopped": True}
