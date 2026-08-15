"""platforms/manual/service.py's own build_service()/ManualService -- the
real entry point wiring. Covers the relocated manual.* dev-panel ops
(formerly daemon/ops/dev_panel.py, now served directly by this process --
see api/routers/dev_panel.py on the client side)."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from krauken.contracts.errors import KraukenError
from krauken.ipc.persistent_client import PersistentIPCClient
from krauken.platforms.manual.service import build_service


@pytest_asyncio.fixture
async def rig():
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-ipc-"))
    service = build_service(socket_path=socket_dir / "manual.sock")
    await service.start()

    client = PersistentIPCClient(service.server.socket_path, heartbeat_interval_s=100)
    await client.start()
    await asyncio.sleep(0.05)

    yield service, client

    await client.stop()
    await service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_discover_reports_manual_devices(rig):
    _service, client = rig
    result = await client.call("platform.discover")
    ids = {c["device_id"] for c in result["candidates"]}
    assert ids == {"manual:chamber", "manual:tilt"}


async def test_manual_get_and_set_reading(rig):
    service, client = rig
    readings = await client.call("manual.get_readings")
    assert readings["chamber"]["temp_f"] == 68.0

    updated = await client.call("manual.set_reading", {"field": "chamber", "values": {"temp_f": 55.0, "mode": "cool"}})
    assert updated["temp_f"] == 55.0
    assert updated["mode"] == "cool"
    assert service.panel.chamber.temp_f == 55.0


async def test_manual_set_reading_rejects_unknown_field(rig):
    # Same contract as every other op error crossing the wire: only .code
    # survives, never the original ValidationError subclass.
    _service, client = rig
    with pytest.raises(KraukenError) as exc_info:
        await client.call("manual.set_reading", {"field": "chamber", "values": {"bogus": 1}})
    assert exc_info.value.code == "validation_error"


async def test_heater_disabled_forces_heating_off(rig):
    _service, client = rig
    await client.call("manual.set_reading", {"field": "chamber", "values": {"heating_on": True, "heating_enabled": True}})
    updated = await client.call(
        "manual.set_reading", {"field": "chamber", "values": {"heating_enabled": False}}
    )
    assert updated["heating_enabled"] is False
    assert updated["heating_on"] is False
