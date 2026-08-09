"""Real HTTP -> real IPC -> real daemon -> real SQLite, nothing mocked.
Covers the M0 done-check from the project plan: the page loads end to end,
and killing the daemon degrades /health without blanking /state.
"""
from __future__ import annotations

from httpx import AsyncClient

from krauken.daemon.app import Daemon


async def test_health_reports_daemon_ok(client: AsyncClient):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api"] == "ok"
    assert body["daemon"] == "ok"
    assert body["db_version"] >= 1


async def test_state_reflects_unconfigured_hardware(client: AsyncClient):
    resp = await client.get("/api/v1/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_needed"] is True
    assert body["can_start_fermentation"] is False
    assert body["active_fermentation_id"] is None
    roles_by_name = {r["role"]: r for r in body["roles"]}
    assert roles_by_name["chamber_temp"]["required"] is True
    assert roles_by_name["chamber_temp"]["filled"] is False
    assert roles_by_name["chamber_heating"]["required"] is False


async def test_state_keeps_working_after_daemon_dies(client: AsyncClient, daemon: Daemon):
    # This is the architectural property the read/write split exists for:
    # history/state reads go straight to SQLite and must survive a daemon
    # restart. Kill the daemon out from under a live client and confirm
    # /state is unaffected while /health correctly reports the daemon gone.
    await daemon.stop()

    state_resp = await client.get("/api/v1/state")
    assert state_resp.status_code == 200
    assert state_resp.json()["setup_needed"] is True

    health_resp = await client.get("/api/v1/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["daemon"] == "unavailable"
