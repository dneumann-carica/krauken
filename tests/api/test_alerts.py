"""Real HTTP -> real SQLite for GET /fermentations/{id}/alerts, exercising
the full path through a real control_tick() rather than inserting events by
hand: the Manual driver's health field genuinely drives last_good_ts (see
platforms/manual/live.py's module docstring), which is what makes this
exercisable without real hardware ever going unresponsive.
"""
from __future__ import annotations

import asyncio

from httpx import AsyncClient

from krauken.contracts.models import Health
from krauken.daemon.app import Daemon
from krauken.daemon.control_loop import control_tick


async def _scan_and_wait(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/hardware/scan")
    scan_id = resp.json()["scan_id"]
    for _ in range(50):
        status = (await client.get(f"/api/v1/hardware/scan/{scan_id}")).json()
        if status["state"] == "complete":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("scan never completed")

STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 999.0, "advance_mode": "manual",
    },
]


async def _tick(daemon: Daemon) -> None:
    async with daemon.server.state_lock:
        await control_tick(daemon.ctx)


async def test_no_alerts_while_the_manual_driver_stays_healthy(client: AsyncClient, daemon: Daemon):
    await _scan_and_wait(client)
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "manual:chamber", "beer_temp": "manual:tilt"}},
    )
    start = await client.post("/api/v1/fermentations", json={"name": "Alert test", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await _tick(daemon)
    resp = await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_beer_temp_lost_opens_an_alert_and_recovery_closes_it(client: AsyncClient, daemon: Daemon):
    await _scan_and_wait(client)
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "manual:chamber", "beer_temp": "manual:tilt"}},
    )
    start = await client.post("/api/v1/fermentations", json={"name": "Alert test", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await _tick(daemon)  # healthy tick first, matching real timing (health starts OK)
    daemon.ctx.manual_panel.tilt.health = Health.UNREACHABLE
    await _tick(daemon)

    alerts = (await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")).json()
    assert len(alerts) == 1
    assert alerts[0]["field"] == "beer_temp"

    daemon.ctx.manual_panel.tilt.health = Health.OK
    await _tick(daemon)
    alerts = (await client.get(f"/api/v1/fermentations/{fermentation_id}/alerts")).json()
    assert alerts == []
