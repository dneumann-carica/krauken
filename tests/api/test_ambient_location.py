"""Real HTTP -> real SQLite -> real control_tick() proving the
chamber_location setting (written by the Hardware Setup wizard's location
step) actually reaches the SimPlant engine's ambient model, not just that
the setting round-trips through GET/PUT (already covered by
tests/api/test_settings.py).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from httpx import AsyncClient

from krauken.platforms.simulator.live import AMBIENT_PRESETS
from krauken.platforms.simulator.plant import AmbientParams
from krauken.platforms.simulator.service import SimulatorService

STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 999.0, "advance_mode": "manual",
    },
]


async def test_chamber_location_setting_reaches_the_sim_engine(
    client: AsyncClient,
    simulator_service: SimulatorService,
    scan_and_wait: Callable[[], Awaitable[dict]],
    tick: Callable[[], Awaitable[None]],
):
    # simulator_service.engine is the SAME SimPlantEngine the daemon's
    # simulator_client reaches over IPC -- both live in this one test
    # process (see tests/api/conftest.py) -- so reading it directly here
    # is a legitimate, un-round-tripped assertion, not a mock.
    assert simulator_service.engine.params.ambient == AmbientParams()  # generic default before any location is set

    await scan_and_wait()
    await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
    )
    await client.post("/api/v1/fermentations", json={"name": "Ambient test", "stages": STAGES})

    await client.put("/api/v1/settings/chamber_location", json={"value": "Garage"})
    await tick()
    assert simulator_service.engine.params.ambient == AMBIENT_PRESETS["Garage"]

    await client.put("/api/v1/settings/chamber_location", json={"value": "Kitchen"})
    await tick()
    assert simulator_service.engine.params.ambient == AMBIENT_PRESETS["Kitchen"]
