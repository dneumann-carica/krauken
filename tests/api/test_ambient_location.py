"""Real HTTP -> real SQLite -> real control_tick() proving the
chamber_location setting (written by the Hardware Setup wizard's location
step) actually reaches the SimPlant engine's ambient model, not just that
the setting round-trips through GET/PUT (already covered by
tests/api/test_settings.py).
"""
from __future__ import annotations

import asyncio

from httpx import AsyncClient

from krauken.daemon.app import Daemon
from krauken.daemon.control_loop import control_tick
from krauken.platforms.simulator.live import AMBIENT_PRESETS
from krauken.platforms.simulator.plant import AmbientParams

STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 999.0, "advance_mode": "manual",
    },
]


async def _scan_and_wait(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/hardware/scan")
    scan_id = resp.json()["scan_id"]
    for _ in range(50):
        status = (await client.get(f"/api/v1/hardware/scan/{scan_id}")).json()
        if status["state"] == "complete":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("scan never completed")


async def _tick(daemon: Daemon) -> None:
    async with daemon.server.state_lock:
        await control_tick(daemon.ctx)


async def test_chamber_location_setting_reaches_the_sim_engine(client: AsyncClient, daemon: Daemon):
    assert daemon.ctx.sim_engine.params.ambient == AmbientParams()  # generic default before any location is set

    await _scan_and_wait(client)
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
    )
    await client.post("/api/v1/fermentations", json={"name": "Ambient test", "stages": STAGES})

    await client.put("/api/v1/settings/chamber_location", json={"value": "Garage"})
    await _tick(daemon)
    assert daemon.ctx.sim_engine.params.ambient == AMBIENT_PRESETS["Garage"]

    await client.put("/api/v1/settings/chamber_location", json={"value": "Kitchen"})
    await _tick(daemon)
    assert daemon.ctx.sim_engine.params.ambient == AMBIENT_PRESETS["Kitchen"]
