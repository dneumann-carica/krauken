"""Outlet fire-test / probe identify-test jobs, against the Manual/Simulator
mock platforms. Real HTTP -> real IPC -> real daemon, with tiny durations
so the suite doesn't spend real wall-clock time on countdowns."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from httpx import AsyncClient

from krauken.daemon.app import Daemon
from krauken.platforms.simulator.service import SimulatorService


async def _poll_until_done(client: AsyncClient, device_id: str, test_id: str, *, patience_s: float = 1.0) -> dict:
    attempts = int(patience_s / 0.01)
    for _ in range(attempts):
        resp = await client.get(f"/api/v1/hardware/devices/{device_id}/test/{test_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("test job never finished")


async def test_fire_outlet_completes_with_server_owned_countdown(client: AsyncClient):
    start = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "fire_outlet", "params": {"duration_s": 0.05, "outlet": 1}},
    )
    assert start.status_code == 200
    body = start.json()
    assert body["state"] == "running"
    assert body["ends_at"] is not None

    final = await _poll_until_done(client, "simulator:chamber", body["test_id"])
    assert final["state"] == "completed"
    assert final["result"]["outlet"] == 1


async def test_identify_probes_reports_a_moved_probe(
    client: AsyncClient, simulator_service: SimulatorService, scan_and_wait: Callable[[], Awaitable[dict]]
):
    # probe_addresses now come from the device's own last-scanned metadata,
    # not a caller-supplied param -- a scan with probe2 enabled is what
    # gives simulator:chamber two addresses to tell apart.
    # simulator_service.engine is the SAME SimPlantEngine the daemon's
    # simulator_client reaches over IPC -- both live in this one test
    # process (see tests/api/conftest.py).
    simulator_service.engine.set_probe2_enabled(True)
    simulator_service.engine.set_probe2_temp(65.0)
    await scan_and_wait()

    start = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "identify_probes", "params": {"window_s": 3.0}},
    )
    body = start.json()
    # Real polling cadence (IDENTIFY_PROBES_POLL_S) means the job's first
    # sample lands ~1s of real wall-clock time after it starts -- nudge
    # probe 2 now so that first sample already sees the jump.
    simulator_service.engine.set_probe2_temp(70.0)

    final = await _poll_until_done(client, "simulator:chamber", body["test_id"], patience_s=5.0)
    assert final["state"] == "completed"
    assert final["result"]["identified_address"] == "sim-probe-2"


async def test_identify_probes_reports_a_live_reading_while_running(client: AsyncClient, daemon: Daemon):
    # No scan yet -- simulator:chamber has never been discovered, so it has
    # no probe_addresses in its metadata. This is the single-probe "just
    # confirm it responds" path, which should still publish a live reading
    # while the job is running, not just once it completes.
    start = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "identify_probes", "params": {"window_s": 1.5}},
    )
    test_id = start.json()["test_id"]

    saw_live_reading = False
    for _ in range(150):
        resp = await client.get(f"/api/v1/hardware/devices/simulator:chamber/test/{test_id}")
        body = resp.json()
        if body["state"] == "running" and body["result"] is not None:
            saw_live_reading = True
            break
        if body["state"] != "running":
            break
        await asyncio.sleep(0.01)

    assert saw_live_reading
    final = await _poll_until_done(client, "simulator:chamber", test_id, patience_s=3.0)
    assert final["state"] == "completed"


async def test_cancel_stops_a_running_test(client: AsyncClient):
    start = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "fire_outlet", "params": {"duration_s": 5.0}},
    )
    test_id = start.json()["test_id"]

    cancel = await client.post(f"/api/v1/hardware/devices/simulator:chamber/test/{test_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["state"] == "cancelled"


async def test_cannot_start_two_tests_on_the_same_device_at_once(client: AsyncClient):
    first = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "fire_outlet", "params": {"duration_s": 5.0}},
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/hardware/devices/simulator:chamber/test",
        json={"action": "fire_outlet", "params": {"duration_s": 5.0}},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "test_already_running"

    # Clean up the still-running job so it doesn't outlive the test.
    await client.post(f"/api/v1/hardware/devices/simulator:chamber/test/{first.json()['test_id']}/cancel")


async def test_unknown_test_id_is_404(client: AsyncClient):
    resp = await client.get("/api/v1/hardware/devices/simulator:chamber/test/nonexistent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_test"
