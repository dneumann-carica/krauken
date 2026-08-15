"""Real HTTP -> real SQLite for GET /fermentations/{id}/alerts, exercising
the full path through a real control_tick() rather than inserting events by
hand: the Manual driver's health field genuinely drives last_good_ts (see
platforms/manual/live.py's module docstring), which is what makes this
exercisable without real hardware ever going unresponsive.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from httpx import AsyncClient

from krauken.contracts.models import Health
from krauken.daemon.app import Daemon
from krauken.platforms.manual.service import ManualService

STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 999.0, "advance_mode": "manual",
    },
]


async def test_no_alerts_while_the_manual_driver_stays_healthy(
    client: AsyncClient, daemon: Daemon, scan_and_wait: Callable[[], Awaitable[dict]], tick: Callable[[], Awaitable[None]]
):
    await scan_and_wait()
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "manual:chamber", "beer_temp": "manual:tilt"}},
    )
    start = await client.post("/api/v1/fermentations", json={"name": "Alert test", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await tick()
    resp = await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_beer_temp_lost_opens_an_alert_and_recovery_closes_it(
    client: AsyncClient,
    manual_service: ManualService,
    scan_and_wait: Callable[[], Awaitable[dict]],
    tick: Callable[[], Awaitable[None]],
):
    await scan_and_wait()
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "manual:chamber", "beer_temp": "manual:tilt"}},
    )
    start = await client.post("/api/v1/fermentations", json={"name": "Alert test", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await tick()  # healthy tick first, matching real timing (health starts OK)
    # Mutating the Manual process's panel directly, in-process -- this is
    # the SAME object the daemon's manual_client reaches over IPC (both
    # live in this one test process; see tests/api/conftest.py's
    # manual_service fixture), just without the wire round trip a real
    # dev-panel PUT would add for no benefit here.
    manual_service.panel.tilt.health = Health.UNREACHABLE
    await tick()

    alerts = (await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")).json()
    assert len(alerts) == 1
    assert alerts[0]["field"] == "beer_temp"

    manual_service.panel.tilt.health = Health.OK
    await tick()
    alerts = (await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")).json()
    assert alerts == []
