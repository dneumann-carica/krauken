from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from krauken.api import deps
from krauken.api.schemas import HealthResponse, RoleStatus, StateResponse, YeastPresetsResponse
from krauken.contracts.errors import DaemonUnavailable
from krauken.contracts.roles import ALL_ROLES, REQUIRED_ROLES
from krauken.db import queries
from krauken.db.migrate import current_version
from krauken.ipc.client import AsyncIPCClient

router = APIRouter()
_start_ts = time.time()
_YEASTS_PATH = Path(__file__).parents[2] / "data" / "yeasts.json"
_yeasts_cache: dict | None = None


@router.get("/yeasts", response_model=YeastPresetsResponse)
async def get_yeasts() -> YeastPresetsResponse:
    global _yeasts_cache
    if _yeasts_cache is None:
        _yeasts_cache = json.loads(_YEASTS_PATH.read_text())
    return YeastPresetsResponse(yeasts=_yeasts_cache)


@router.get("/health", response_model=HealthResponse)
async def get_health(
    client: AsyncIPCClient = Depends(deps.daemon),
) -> HealthResponse:
    try:
        await client.call("system.ping", deadline_ms=1000)
        daemon_status = "ok"
    except DaemonUnavailable:
        daemon_status = "unavailable"
    return HealthResponse(
        daemon=daemon_status,
        db_version=current_version(deps.get_config().db_path),
        uptime_s=time.time() - _start_ts,
    )


@router.get("/state", response_model=StateResponse)
async def get_state() -> StateResponse:
    return await deps.run_ro(_build_state)


def _build_state(conn: sqlite3.Connection) -> StateResponse:
    # Runs entirely inside deps.run_ro()'s single threadpool call -- see
    # deps.py's module docstring for why that matters.
    hw_rows = {row["role"]: row for row in queries.hardware_config(conn)}
    devices_by_id = {d["device_id"]: d for d in queries.devices(conn)}
    roles = []
    for role in ALL_ROLES:
        row = hw_rows[role.value]
        device = devices_by_id.get(row["device_id"]) if row["device_id"] else None
        roles.append(
            RoleStatus(
                role=role.value,
                required=role in REQUIRED_ROLES,
                filled=row["platform"] is not None,
                device_id=row["device_id"],
                device_name=device["name"] if device else None,
                health=device["health"] if device else None,
            )
        )
    required_filled = all(r.filled for r in roles if r.required)
    active = queries.active_fermentation(conn)
    live = queries.live_state(conn)

    return StateResponse(
        setup_needed=not required_filled,
        roles=roles,
        can_start_fermentation=required_filled,
        active_fermentation_id=active["id"] if active else None,
        live=live,
    )
