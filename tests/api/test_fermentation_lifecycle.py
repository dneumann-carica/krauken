"""Real HTTP -> real IPC -> real daemon -> real SQLite for the M2
fermentation lifecycle endpoints. Control-loop *timing* (auto-advance,
sampling) is covered by tests/scenarios/test_full_fermentation.py, which
uses a ManualClock; this file's `client`/`daemon` fixtures use a real
SystemClock (see conftest.py), so these tests only exercise the lifecycle
ops themselves (start/terminate/manual-advance/edit-stages), not waiting
for a real tick to fire.
"""
from __future__ import annotations

import asyncio

from httpx import AsyncClient

from krauken.daemon.app import Daemon
from krauken.daemon.control_loop import control_tick

STAGES = [
    {
        "name": "Primary", "temp_mode": "constant", "temp_f": 68.0,
        "end_mode": "time", "end_hours": 4.0, "advance_mode": "auto",
    },
    {
        "name": "Cold crash", "temp_mode": "constant", "temp_f": 55.0,
        "end_mode": "time", "end_hours": 2.0, "advance_mode": "manual",
    },
]


async def _scan_and_map(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/hardware/scan")
    scan_id = resp.json()["scan_id"]
    for _ in range(50):
        status = (await client.get(f"/api/v1/hardware/scan/{scan_id}")).json()
        if status["state"] == "complete":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("scan never completed")

    save = await client.put(
        "/api/v1/hardware/mapping",
        json={"roles": {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
    )
    assert save.json()["valid"] is True


async def test_start_requires_hardware_mapped(client: AsyncClient):
    resp = await client.post("/api/v1/fermentations", json={"name": "Too early", "stages": STAGES})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "hardware_incomplete"


async def test_start_then_get_reflects_the_new_fermentation(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "og": 1.055, "stages": STAGES})
    assert start.status_code == 200
    fermentation_id = start.json()["fermentation_id"]
    assert len(start.json()["stage_ids"]) == 2

    detail = await client.get(f"/api/v1/fermentations/{fermentation_id}")
    body = detail.json()
    assert body["name"] == "My IPA"
    assert body["status"] == "active"
    assert body["stages"][0]["state"] == "running"
    assert body["stages"][1]["state"] == "pending"

    state = await client.get("/api/v1/state")
    assert state.json()["active_fermentation_id"] == fermentation_id


async def test_cannot_start_a_second_fermentation_while_one_is_active(client: AsyncClient):
    await _scan_and_map(client)
    await client.post("/api/v1/fermentations", json={"name": "First", "stages": STAGES})
    second = await client.post("/api/v1/fermentations", json={"name": "Second", "stages": STAGES})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "fermentation_already_active"


async def test_manual_advance_moves_to_the_next_stage(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    advance = await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")
    assert advance.status_code == 200
    assert advance.json()["advanced"] is True

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][0]["state"] == "finished"
    assert detail["stages"][0]["end_actual_reason"] == "manual"
    assert detail["stages"][0]["criteria_met_at"] is None  # advanced early -- criteria weren't actually met
    assert detail["stages"][1]["state"] == "running"


async def test_advancing_past_the_last_stage_completes_the_fermentation(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")
    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["status"] == "completed"
    assert detail["ended_at"] is not None

    state = await client.get("/api/v1/state")
    assert state.json()["active_fermentation_id"] is None


async def test_terminate_ends_the_batch_and_skips_the_running_stage(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    resp = await client.post(f"/api/v1/fermentations/{fermentation_id}/terminate", json={"reason": "infection"})
    assert resp.status_code == 200
    assert resp.json()["terminated"] is True

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["status"] == "terminated"
    assert detail["end_reason"] == "infection"
    assert detail["stages"][0]["state"] == "skipped"
    assert detail["stages"][0]["end_actual_reason"] == "terminated"


async def test_terminate_on_a_non_active_fermentation_404s(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    await client.post(f"/api/v1/fermentations/{fermentation_id}/terminate", json={})

    again = await client.post(f"/api/v1/fermentations/{fermentation_id}/terminate", json={})
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "no_active_fermentation"


async def test_edit_running_stage_updates_its_fields(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    running_stage_id = detail["stages"][0]["id"]

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages",
        json={"stages": {str(running_stage_id): {"temp_f": 64.0, "end_hours": 6.0}}},
    )
    assert resp.status_code == 200
    assert resp.json()["updated_stage_ids"] == [running_stage_id]

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][0]["temp_f"] == 64.0
    assert detail["stages"][0]["end_hours"] == 6.0


async def test_cannot_edit_a_finished_stage(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    first_stage_id = detail["stages"][0]["id"]

    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")  # finishes stage 1

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages",
        json={"stages": {str(first_stage_id): {"temp_f": 60.0}}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_turning_off_a_pending_stage_marks_it_skipped(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    pending_stage_id = detail["stages"][1]["id"]

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages/{pending_stage_id}/enabled", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json() == {"stage_id": pending_stage_id, "enabled": False}

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][1]["state"] == "skipped"
    assert detail["stages"][1]["end_actual_reason"] == "skipped"
    assert detail["stages"][1]["started_at"] is None


async def test_turning_a_skipped_stage_back_on_reverts_to_pending(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    pending_stage_id = detail["stages"][1]["id"]

    await client.put(f"/api/v1/fermentations/{fermentation_id}/stages/{pending_stage_id}/enabled", json={"enabled": False})
    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages/{pending_stage_id}/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][1]["state"] == "pending"
    assert detail["stages"][1]["end_actual_reason"] is None


async def test_cannot_turn_off_the_active_stage(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    running_stage_id = detail["stages"][0]["id"]

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages/{running_stage_id}/enabled", json={"enabled": False}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_advancing_skips_over_a_stage_turned_off(client: AsyncClient):
    three_stages = STAGES + [
        {
            "name": "Conditioning", "temp_mode": "constant", "temp_f": 60.0,
            "end_mode": "time", "end_hours": 1.0, "advance_mode": "manual",
        },
    ]
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": three_stages})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    middle_stage_id = detail["stages"][1]["id"]

    await client.put(f"/api/v1/fermentations/{fermentation_id}/stages/{middle_stage_id}/enabled", json={"enabled": False})
    advance = await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")
    assert advance.status_code == 200

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][0]["state"] == "finished"
    assert detail["stages"][1]["state"] == "skipped"  # untouched by the advance -- still skipped, not started
    assert detail["stages"][2]["state"] == "running"  # advance landed here, not on the skipped middle stage


async def test_series_projection_appears_once_a_sample_exists_for_an_active_batch(
    client: AsyncClient, daemon: Daemon
):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    # _compute_projection needs a real sample to project forward from --
    # ticking once directly (rather than waiting on the real-time
    # background loop) keeps this test fast without touching Clock/timing.
    async with daemon.server.state_lock:
        await control_tick(daemon.ctx)

    series = (await client.get(f"/api/v1/fermentations/{fermentation_id}/series")).json()
    assert series["projection"] is not None
    assert len(series["projection"]["ts"]) > 0
    assert len(series["projection"]["beer_temp_f"]) == len(series["projection"]["ts"])


async def test_insert_stage_appends_at_the_end_by_default(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    resp = await client.post(
        f"/api/v1/fermentations/{fermentation_id}/stages",
        json={"stage": {"name": "Conditioning", "temp_mode": "constant", "temp_f": 60.0,
                         "end_mode": "time", "end_hours": 24.0, "advance_mode": "auto"}},
    )
    assert resp.status_code == 200
    new_stage_id = resp.json()["stage_id"]

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert [s["name"] for s in detail["stages"]] == ["Primary", "Cold crash", "Conditioning"]
    assert detail["stages"][2]["id"] == new_stage_id
    assert detail["stages"][2]["state"] == "pending"


async def test_insert_stage_after_a_specific_stage_shifts_later_ones(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    running_stage_id = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()["stages"][0]["id"]

    resp = await client.post(
        f"/api/v1/fermentations/{fermentation_id}/stages",
        json={
            "after_stage_id": running_stage_id,
            "stage": {"name": "Diacetyl rest", "temp_mode": "constant", "temp_f": 70.0,
                      "end_mode": "time", "end_hours": 12.0, "advance_mode": "auto"},
        },
    )
    assert resp.status_code == 200

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    # Inserted directly after the running stage -- "Cold crash" (originally
    # seq 1) must have shifted to make room, not been overwritten or duplicated.
    assert [s["name"] for s in detail["stages"]] == ["Primary", "Diacetyl rest", "Cold crash"]
    assert [s["seq"] for s in detail["stages"]] == [0, 1, 2]


async def test_insert_stage_after_a_finished_stage_is_rejected(client: AsyncClient):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]
    running_stage_id = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()["stages"][0]["id"]
    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")  # finishes stage 1

    resp = await client.post(
        f"/api/v1/fermentations/{fermentation_id}/stages",
        json={
            "after_stage_id": running_stage_id,
            "stage": {"name": "Too late", "temp_mode": "constant", "temp_f": 60.0,
                      "end_mode": "time", "end_hours": 1.0, "advance_mode": "auto"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_reorder_stages_reassigns_seq_to_match_the_requested_order(client: AsyncClient):
    three_stages = STAGES + [
        {"name": "Conditioning", "temp_mode": "constant", "temp_f": 60.0,
         "end_mode": "time", "end_hours": 1.0, "advance_mode": "manual"},
    ]
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": three_stages})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    running_id, cold_crash_id, conditioning_id = (s["id"] for s in detail["stages"])

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages/order",
        json={"stage_ids": [conditioning_id, cold_crash_id]},
    )
    assert resp.status_code == 200
    assert resp.json()["stage_ids"] == [conditioning_id, cold_crash_id]

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert [s["name"] for s in detail["stages"]] == ["Primary", "Conditioning", "Cold crash"]
    # The running stage's own seq/identity is untouched by a reorder.
    assert detail["stages"][0]["id"] == running_id


async def test_reorder_stages_rejects_a_set_that_doesnt_match_exactly(client: AsyncClient):
    three_stages = STAGES + [
        {"name": "Conditioning", "temp_mode": "constant", "temp_f": 60.0,
         "end_mode": "time", "end_hours": 1.0, "advance_mode": "manual"},
    ]
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": three_stages})
    fermentation_id = start.json()["fermentation_id"]
    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    _, cold_crash_id, _ = (s["id"] for s in detail["stages"])

    resp = await client.put(
        f"/api/v1/fermentations/{fermentation_id}/stages/order",
        json={"stage_ids": [cold_crash_id]},  # missing conditioning_id
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_series_projection_is_none_for_a_completed_batch(client: AsyncClient, daemon: Daemon):
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "My IPA", "stages": STAGES})
    fermentation_id = start.json()["fermentation_id"]

    async with daemon.server.state_lock:
        await control_tick(daemon.ctx)
    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")
    await client.post(f"/api/v1/fermentations/{fermentation_id}/advance")  # completes it

    series = (await client.get(f"/api/v1/fermentations/{fermentation_id}/series")).json()
    assert series["projection"] is None


async def test_gravity_below_stage_round_trips(client: AsyncClient):
    """API-level acceptance of the new end_mode -- reuses gravity_hi (the
    threshold) and hold_hours (the duration), no new fields. Control-loop
    *timing* for this gate has its own dedicated unit coverage in
    test_stages.py, matching how temp_hold is covered (that one has no
    scenario-level test either)."""
    gravity_below_stages = [
        {
            "name": "Primary", "temp_mode": "constant", "temp_f": 66.0,
            "end_mode": "gravity_below", "gravity_hi": 1.020, "hold_hours": 12.0, "advance_mode": "auto",
        },
    ]
    await _scan_and_map(client)
    start = await client.post("/api/v1/fermentations", json={"name": "Threshold batch", "stages": gravity_below_stages})
    assert start.status_code == 200
    fermentation_id = start.json()["fermentation_id"]

    detail = (await client.get(f"/api/v1/fermentations/{fermentation_id}")).json()
    assert detail["stages"][0]["end_mode"] == "gravity_below"
    assert detail["stages"][0]["gravity_hi"] == 1.020
    assert detail["stages"][0]["hold_hours"] == 12.0
