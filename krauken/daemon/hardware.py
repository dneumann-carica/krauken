"""Hardware role-mapping: read the current mapping, and save a new one
through the auto-resolve algorithm in contracts/roles.py.
"""
from __future__ import annotations

import datetime
from typing import Any

from krauken.contracts.roles import ALL_ROLES, Role, resolve
from krauken.db import queries, writes


def _iso_now(clock) -> str:
    return datetime.datetime.fromtimestamp(clock.now(), tz=datetime.timezone.utc).isoformat()


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
