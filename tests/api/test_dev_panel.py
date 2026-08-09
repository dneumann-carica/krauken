"""Real HTTP -> real IPC -> real daemon for the Manual driver's dev panel.
Disabled by default (Config.dev_panel_enabled=False, matching the `client`
fixture's default Config) -- enabling it per-test via monkeypatch mirrors
how a real deployment would flip it on via KRAUKEN_DEV_PANEL=1.
"""
from __future__ import annotations

import dataclasses

import pytest
from httpx import AsyncClient

from krauken.api import deps
from krauken.contracts.clock import SimulatorClock
from krauken.daemon.app import Daemon


@pytest.fixture
def dev_panel_enabled(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "_config", dataclasses.replace(deps.get_config(), dev_panel_enabled=True))


async def test_disabled_by_default(client: AsyncClient):
    resp = await client.get("/api/v1/dev/manual")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "dev_panel_disabled"


async def test_set_also_gated_when_disabled(client: AsyncClient):
    resp = await client.put("/api/v1/dev/manual/tilt", json={"temp_f": 80.0})
    assert resp.status_code == 403


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_get_readings_reflects_defaults(client: AsyncClient):
    resp = await client.get("/api/v1/dev/manual")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chamber"]["health"] == "ok"
    assert body["tilt"]["temp_f"] == 68.0
    assert body["tilt"]["gravity_sg"] == 1.050


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_set_tilt_temp_and_health_round_trips(client: AsyncClient):
    resp = await client.put("/api/v1/dev/manual/tilt", json={"temp_f": 75.0, "health": "unreachable"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["temp_f"] == 75.0
    assert body["health"] == "unreachable"

    again = await client.get("/api/v1/dev/manual")
    assert again.json()["tilt"]["temp_f"] == 75.0
    assert again.json()["tilt"]["health"] == "unreachable"


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_set_chamber_mode_directly(client: AsyncClient):
    resp = await client.put("/api/v1/dev/manual/chamber", json={"mode": "cool", "temp_f": 55.0})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "cool"
    assert resp.json()["temp_f"] == 55.0


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_rejects_a_field_not_valid_for_the_target(client: AsyncClient):
    resp = await client.put("/api/v1/dev/manual/tilt", json={"mode": "cool"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_unknown_target_field_404_or_422(client: AsyncClient):
    resp = await client.put("/api/v1/dev/manual/nonsense", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_simulator_probe2_toggle_round_trips(client: AsyncClient):
    resp = await client.get("/api/v1/dev/simulator")
    assert resp.status_code == 200
    assert resp.json()["probe2_enabled"] is False

    resp = await client.put("/api/v1/dev/simulator/probe2", json={"enabled": True, "temp_f": 71.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["probe2_enabled"] is True
    assert body["probe2_temp_f"] == 71.0

    again = await client.get("/api/v1/dev/simulator")
    assert again.json()["probe2_enabled"] is True
    assert again.json()["probe2_temp_f"] == 71.0


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_get_clock_reflects_whatever_clock_the_daemon_is_actually_running(client: AsyncClient, daemon: Daemon):
    # No advance/speed dial anymore -- /dev/clock is a plain read of
    # whatever Clock implementation this daemon selected (ProductionClock
    # here, since this suite's `daemon` fixture maps no hardware at all;
    # see daemon/app.py's _select_clock). Swapping in a SimulatorClock and
    # advancing it directly confirms the route just reflects ctx.clock,
    # generically, regardless of which implementation is active.
    daemon.ctx.clock = SimulatorClock()
    before = (await client.get("/api/v1/dev/clock")).json()["now"]
    daemon.ctx.clock.advance(21600.0)
    after = (await client.get("/api/v1/dev/clock")).json()["now"]
    assert after > before


@pytest.mark.usefixtures("dev_panel_enabled")
async def test_speed_and_advance_routes_no_longer_exist(client: AsyncClient):
    # Retired entirely -- neither means anything now that a Simulator-only
    # hardware mapping runs its own clock at full speed automatically (see
    # daemon/app.py's _select_clock and contracts/clock.py's SimulatorClock).
    #
    # Status codes here are unreliable to assert on directly: the
    # frontend's SPA-fallback catch-all route (`/{full_path:path}`,
    # GET-only) matches any path pattern under /api/v1/*, so an unmatched
    # GET actually falls through to it and serves the SPA shell (200), while
    # POST/PUT against that same path 405s (method doesn't match the
    # catch-all's GET-only registration) rather than 404ing. Either way, the
    # real invariant is "no JSON dev-panel response with a speed/now value
    # comes back" -- assert on content, not on the routing quirk's status
    # code.
    post_resp = await client.post("/api/v1/dev/clock/advance", json={"seconds": 60.0})
    assert post_resp.status_code != 200

    get_resp = await client.get("/api/v1/dev/speed")
    assert "application/json" not in get_resp.headers.get("content-type", "")

    put_resp = await client.put("/api/v1/dev/speed", json={"speed": 100})
    assert put_resp.status_code != 200
