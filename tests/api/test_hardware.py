"""Real HTTP -> real IPC -> real daemon (discovery + auto-resolve) -> real
SQLite, against the Manual/Simulator mock platforms -- the exact chain the
M1 demo script exercises before any real hardware driver exists.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from httpx import AsyncClient

from krauken.daemon.app import Daemon
from krauken.platforms.simulator.service import SimulatorService

STAGES = [
    {"name": "Primary", "temp_mode": "constant", "temp_f": 68.0, "end_mode": "time", "end_hours": 4.0, "advance_mode": "auto"},
]


async def test_mapping_empty_before_any_scan(client: AsyncClient):
    resp = await client.get("/api/v1/hardware/mapping")
    assert resp.status_code == 200
    roles = resp.json()["roles"]
    assert all(r["device_id"] is None for r in roles.values())


async def test_scan_discovers_manual_and_simulator_devices(scan_and_wait: Callable[[], Awaitable[dict]]):
    result = await scan_and_wait()
    assert result["platform_status"]["manual"]["state"] == "ok"
    assert result["platform_status"]["simulator"]["state"] == "ok"
    device_ids = {d["device_id"] for d in result["devices"]}
    assert {"manual:chamber", "manual:tilt", "simulator:chamber", "simulator:tilt"} <= device_ids


async def test_devices_endpoint_reflects_the_scan_cache(client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]):
    await scan_and_wait()
    resp = await client.get("/api/v1/hardware/devices")
    assert resp.status_code == 200
    device_ids = {d["device_id"] for d in resp.json()}
    assert "simulator:chamber" in device_ids


async def test_available_tests_survive_the_candidate_to_row_projection(
    client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]
):
    # DeviceCandidate.available_tests only exists on the platform-driver
    # contract, not as its own devices-table column -- it has to make it
    # into the persisted metadata blob, or the Hardware Setup wizard has no
    # way to know which discovered devices support a fire/identify test.
    await scan_and_wait()
    resp = await client.get("/api/v1/hardware/devices")
    by_id = {d["device_id"]: d for d in resp.json()}
    assert by_id["simulator:chamber"]["metadata"]["available_tests"] == ["fire_outlet", "identify_probes"]
    assert by_id["simulator:tilt"]["metadata"]["available_tests"] == ["live_read"]
    assert by_id["manual:chamber"]["metadata"]["available_tests"] == ["fire_outlet", "identify_probes"]


async def test_save_mapping_auto_fills_chamber_bundle_and_unlocks_state(
    client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]
):
    await scan_and_wait()

    save_resp = await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
    )
    assert save_resp.status_code == 200
    body = save_resp.json()
    assert body["valid"] is True
    assert body["roles"]["chamber_cooling"] == "simulator:chamber"
    assert body["roles"]["chamber_heating"] == "simulator:chamber"
    assert body["roles"]["beer_temp"] == "simulator:tilt"
    assert body["auto_resolved"] == []

    mapping_resp = await client.get("/api/v1/hardware/mapping")
    assert mapping_resp.json()["roles"]["chamber_cooling"]["device_id"] == "simulator:chamber"

    state_resp = await client.get("/api/v1/state")
    state = state_resp.json()
    assert state["setup_needed"] is False
    assert state["can_start_fermentation"] is True
    roles_by_name = {r["role"]: r for r in state["roles"]}
    assert roles_by_name["chamber_temp"]["device_name"] == "Simulated chamber controller"
    assert roles_by_name["chamber_temp"]["health"] == "ok"


async def test_conflicting_bundle_devices_auto_resolve(client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]):
    await scan_and_wait()

    first = await client.put(
        "/api/v1/hardware/mapping", json={"roles": {"chamber_temp": "manual:chamber"}}
    )
    assert first.json()["roles"]["chamber_cooling"] == "manual:chamber"

    # PUT is a full-replacement contract (the frontend holds a complete
    # draft and sends it whole, per the plan's HardwareDraftContext) -- so
    # this simulates the user having chamber_temp still pointed at
    # manual:chamber in their draft while reassigning chamber_cooling to
    # simulator:chamber, not a partial patch against server-side state.
    second = await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "manual:chamber", "chamber_cooling": "simulator:chamber"}},
    )
    body = second.json()
    # chamber_temp's incumbent device is the tie-breaker (per the resolved
    # rule): manual:chamber sits in chamber_temp in this draft, so it wins
    # the WHOLE bundle and simulator:chamber's chamber_cooling claim is the
    # one that gets cleared -- not the other way around. A real UI would
    # normally pre-resolve this client-side before the user ever gets to
    # Save (so this exact ordering wouldn't surprise anyone in practice),
    # but the server's own resolve() must get the tie-break rule right
    # regardless of what submitted it.
    assert body["roles"]["chamber_temp"] == "manual:chamber"
    assert body["roles"]["chamber_cooling"] == "manual:chamber"
    assert body["roles"]["chamber_heating"] == "manual:chamber"
    assert len(body["auto_resolved"]) == 1
    assert body["auto_resolved"][0]["device_id"] == "simulator:chamber"


async def test_chamber_status_is_unmapped_before_any_role_is_saved(client: AsyncClient):
    resp = await client.get("/api/v1/hardware/chamber_status")
    assert resp.status_code == 200
    assert resp.json() == {"commanded_target_f": None, "mapped": False}


async def test_chamber_status_reports_no_target_when_mapped_but_nothing_ever_commanded(
    client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]
):
    await scan_and_wait()
    await client.put("/api/v1/hardware/mapping", json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}})

    resp = await client.get("/api/v1/hardware/chamber_status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mapped"] is True
    assert body["commanded_target_f"] is None


async def test_stop_chamber_is_blocked_while_a_fermentation_is_active(
    client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]
):
    await scan_and_wait()
    await client.put("/api/v1/hardware/mapping", json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}})
    await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})

    resp = await client.post("/api/v1/hardware/stop_chamber")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "fermentation_already_active"


async def test_stop_chamber_is_a_safe_no_op_when_nothing_is_mapped(client: AsyncClient):
    resp = await client.post("/api/v1/hardware/stop_chamber")
    assert resp.status_code == 200
    assert resp.json()["stopped"] is True


async def test_stop_chamber_clears_the_commanded_target_and_status_reflects_it(
    client: AsyncClient,
    simulator_service: SimulatorService,
    scan_and_wait: Callable[[], Awaitable[dict]],
    tick: Callable[[], Awaitable[None]],
):
    await scan_and_wait()
    await client.put("/api/v1/hardware/mapping", json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}})
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await tick()  # commands a real chamber target at least once
    # simulator_service.engine is the SAME SimPlantEngine the daemon's
    # simulator_client reaches over IPC -- both live in this one test
    # process (see tests/api/conftest.py).
    assert simulator_service.engine._chamber_target_f is not None

    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")  # only stage -- completes it

    status_before = (await client.get("/api/v1/hardware/chamber_status")).json()
    assert status_before["mapped"] is True
    assert status_before["commanded_target_f"] is not None

    resp = await client.post("/api/v1/hardware/stop_chamber")
    assert resp.status_code == 200
    assert resp.json()["stopped"] is True

    # The actual point of this feature: the driver was really told to
    # de-energize, not just some flag flipped.
    assert simulator_service.engine._chamber_target_f is None

    status_after = (await client.get("/api/v1/hardware/chamber_status")).json()
    assert status_after["commanded_target_f"] is None
    assert status_after["mapped"] is True  # still mapped -- just idle now


async def test_stop_chamber_is_idempotent(client: AsyncClient, daemon: Daemon, scan_and_wait: Callable[[], Awaitable[dict]]):
    await scan_and_wait()
    await client.put("/api/v1/hardware/mapping", json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}})
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")

    first = await client.post("/api/v1/hardware/stop_chamber")
    second = await client.post("/api/v1/hardware/stop_chamber")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"stopped": True}
