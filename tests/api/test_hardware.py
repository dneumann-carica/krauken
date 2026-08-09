"""Real HTTP -> real IPC -> real daemon (discovery + auto-resolve) -> real
SQLite, against the Manual/Simulator mock platforms -- the exact chain the
M1 demo script exercises before any real hardware driver exists.
"""
from __future__ import annotations

import asyncio

from httpx import AsyncClient


async def _scan_and_wait(client: AsyncClient) -> dict:
    resp = await client.post("/api/v1/hardware/scan")
    assert resp.status_code == 200
    scan_id = resp.json()["scan_id"]

    for _ in range(50):
        status_resp = await client.get(f"/api/v1/hardware/scan/{scan_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body["state"] == "complete":
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("scan never completed")


async def test_mapping_empty_before_any_scan(client: AsyncClient):
    resp = await client.get("/api/v1/hardware/mapping")
    assert resp.status_code == 200
    roles = resp.json()["roles"]
    assert all(r["device_id"] is None for r in roles.values())


async def test_scan_discovers_manual_and_simulator_devices(client: AsyncClient):
    result = await _scan_and_wait(client)
    assert result["platform_status"]["manual"]["state"] == "ok"
    assert result["platform_status"]["simulator"]["state"] == "ok"
    device_ids = {d["device_id"] for d in result["devices"]}
    assert {"manual:chamber", "manual:tilt", "simulator:chamber", "simulator:tilt"} <= device_ids


async def test_devices_endpoint_reflects_the_scan_cache(client: AsyncClient):
    await _scan_and_wait(client)
    resp = await client.get("/api/v1/hardware/devices")
    assert resp.status_code == 200
    device_ids = {d["device_id"] for d in resp.json()}
    assert "simulator:chamber" in device_ids


async def test_available_tests_survive_the_candidate_to_row_projection(client: AsyncClient):
    # DeviceCandidate.available_tests only exists on the platform-driver
    # contract, not as its own devices-table column -- it has to make it
    # into the persisted metadata blob, or the Hardware Setup wizard has no
    # way to know which discovered devices support a fire/identify test.
    await _scan_and_wait(client)
    resp = await client.get("/api/v1/hardware/devices")
    by_id = {d["device_id"]: d for d in resp.json()}
    assert by_id["simulator:chamber"]["metadata"]["available_tests"] == ["fire_outlet", "identify_probes"]
    assert by_id["simulator:tilt"]["metadata"]["available_tests"] == ["live_read"]
    assert by_id["manual:chamber"]["metadata"]["available_tests"] == ["fire_outlet", "identify_probes"]


async def test_save_mapping_auto_fills_chamber_bundle_and_unlocks_state(client: AsyncClient):
    await _scan_and_wait(client)

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


async def test_conflicting_bundle_devices_auto_resolve(client: AsyncClient):
    await _scan_and_wait(client)

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
