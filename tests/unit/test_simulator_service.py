"""platforms/simulator/service.py's own build_service()/SimulatorService --
the real entry point wiring, as opposed to test_ipc_service.py's
hand-assembled rig. Covers the relocated simulator.* dev-panel ops
(formerly daemon/ops/dev_panel.py, now served directly by this process --
see api/routers/dev_panel.py on the client side)."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest_asyncio

from krauken.ipc.persistent_client import PersistentIPCClient
from krauken.platforms.ipc_driver import IpcChamberDriver
from krauken.platforms.simulator.service import build_service


@pytest_asyncio.fixture
async def rig():
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    service = build_service(socket_path=socket_dir / "sim.sock")
    await service.start()

    client = PersistentIPCClient(service.server.socket_path, heartbeat_interval_s=100)
    await client.start()
    await asyncio.sleep(0.05)

    yield service, client

    await client.stop()
    await service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_discover_and_chamber_ops_work_end_to_end(rig):
    service, client = rig
    candidates = await client.call("platform.discover")
    ids = {c["device_id"] for c in candidates["candidates"]}
    assert ids == {"simulator:chamber", "simulator:tilt"}

    driver = IpcChamberDriver(client)
    assert await driver.commanded_target() is None


async def test_simulator_dev_panel_ops(rig):
    service, client = rig
    readings = await client.call("simulator.get_readings")
    assert readings["probe2_enabled"] is False
    assert readings["chamber_temp_f"] is not None

    updated = await client.call("simulator.set_probe2", {"enabled": True, "temp_f": 71.5})
    assert updated == {"probe2_enabled": True, "probe2_temp_f": 71.5}
    assert service.engine.probe2_enabled is True
    assert service.engine.probe2_temp_f == 71.5

    readings = await client.call("simulator.get_readings")
    assert readings["probe2_enabled"] is True
    assert readings["probe2_temp_f"] == 71.5
