from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from krauken.api import deps
from krauken.api.app import create_app
from krauken.config import Config
from krauken.daemon.app import Daemon, build_daemon
from krauken.daemon.control_loop import control_tick
from krauken.platforms.manual.service import ManualService
from krauken.platforms.manual.service import build_service as build_manual_service
from krauken.platforms.simulator.service import SimulatorService
from krauken.platforms.simulator.service import build_service as build_simulator_service


@pytest_asyncio.fixture
async def simulator_service() -> AsyncIterator[SimulatorService]:
    # AF_UNIX socket paths have a real length limit (~104 bytes on macOS/BSD)
    # -- pytest's own tmp_path is nested deep enough (pytest-of-<user>/
    # pytest-N/<test-name>N/...) to exceed it, which fails with a genuinely
    # confusing "AF_UNIX path too long" OSError. Sockets specifically need a
    # short-path temp dir.
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-"))
    service = build_simulator_service(socket_path=socket_dir / "sim.sock")
    await service.start()
    yield service
    await service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def manual_service() -> AsyncIterator[ManualService]:
    socket_dir = Path(tempfile.mkdtemp(prefix="kr-"))
    service = build_manual_service(socket_path=socket_dir / "man.sock")
    await service.start()
    yield service
    await service.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def daemon(
    tmp_path: Path,
    simulator_service: SimulatorService,
    manual_service: ManualService,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Daemon]:
    db_path = tmp_path / "krauken.db"
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))
    socket_path = short_tmp / "d.sock"
    # build_daemon() no longer accepts simulator_socket/manual_socket --
    # ManualIpcConnection/SimulatorIpcConnection resolve their own path
    # from these same env vars (platforms/registry.py's PlatformRegistry
    # constructs them), so pointing the daemon at THIS fixture's specific
    # simulator_service/manual_service instances means setting the env
    # vars they'll read, not passing the path directly.
    monkeypatch.setenv("KRAUKEN_SIMULATOR_SOCKET", str(simulator_service.server.socket_path))
    monkeypatch.setenv("KRAUKEN_MANUAL_SOCKET", str(manual_service.server.socket_path))
    d = build_daemon(
        db_path=db_path,
        socket_path=socket_path,
        heartbeat_interval_s=3600,
    )
    await d.start()
    yield d
    await d.stop()
    shutil.rmtree(short_tmp, ignore_errors=True)


@pytest_asyncio.fixture
async def client(daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    # Point the API tier's dependencies at the SAME db/socket/simulator/
    # manual the daemon fixture just created -- this is a real integration
    # test (real HTTP -> real IPC -> real daemon/simulator/manual -> real
    # SQLite), nothing mocked. daemon.ctx.registry.state_for("simulator")/
    # state_for("manual") already hold these exact socket paths (the
    # `daemon` fixture above set the env vars build_daemon() read them
    # from) -- reading them back here instead of threading
    # simulator_service/manual_service into this fixture too keeps the
    # dependency list to just `daemon`.
    test_config = Config(
        db_path=daemon.ctx.db_path,
        daemon_socket=daemon.server.socket_path,
        supervisor_socket=Path(f"/tmp/krauken-test-supervisor-{uuid.uuid4().hex}.sock"),
        simulator_socket=Path(daemon.ctx.registry.state_for("simulator").socket_path),
        manual_socket=Path(daemon.ctx.registry.state_for("manual").socket_path),
    )
    monkeypatch.setattr(deps, "_config", test_config)

    app = create_app()
    transport = ASGITransport(app=app)
    # Real browser traffic sends this header via the frontend's fetch
    # wrapper (see frontend/src/api/client.ts); it's set as a default here
    # so the many existing integration tests -- which exercise the real
    # HTTP layer directly, not through that wrapper -- don't all need their
    # own per-call header. test_security.py tests the enforcement itself
    # against raw_client below, which deliberately omits this.
    async with AsyncClient(transport=transport, base_url="http://test", headers={"X-Krauken-Client": "1"}) as c:
        yield c


@pytest_asyncio.fixture
async def raw_client(daemon: Daemon, client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """Same wiring as `client` (depending on it ensures the same deps
    monkeypatching has already happened), but with no default headers --
    for testing the anti-CSRF middleware's enforcement itself."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def scan_and_wait(client: AsyncClient) -> Callable[[], Awaitable[dict]]:
    """A callable fixture (not a bare async function) so every test module
    that needs it gets the SAME polling loop by just naming `scan_and_wait`
    as a parameter, instead of each file hand-rolling its own copy of "POST
    a scan, poll until state == complete" -- this used to be duplicated,
    byte-for-byte or near enough, across test_alerts.py,
    test_ambient_location.py, test_hardware.py, and test_fermentation_lifecycle.py."""

    async def _scan_and_wait() -> dict:
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

    return _scan_and_wait


@pytest.fixture
def scan_and_map(
    client: AsyncClient, scan_and_wait: Callable[[], Awaitable[dict]]
) -> Callable[..., Awaitable[None]]:
    """scan_and_wait() plus the hardware-mapping PUT every lifecycle test
    needs before a fermentation can start -- defaults to the
    Simulator-chamber/Simulator-tilt mapping every current caller wants,
    but takes an explicit `roles` mapping for callers (e.g. Manual-backed
    health/alert tests) that need a different one."""

    async def _scan_and_map(roles: dict[str, str] | None = None) -> None:
        await scan_and_wait()
        save = await client.put(
            "/api/v1/hardware/mapping",
            json={"roles": roles or {"chamber_temp": "simulator:chamber", "beer_temp": "simulator:tilt"}},
        )
        assert save.json()["valid"] is True

    return _scan_and_map


@pytest.fixture
def tick(daemon: Daemon) -> Callable[[], Awaitable[None]]:
    """Runs one real control_tick() under the same state_lock the daemon's
    own control loop holds while ticking -- for tests that need a tick's
    side effects (a commanded chamber target, a persisted sample, a
    health-event log entry) without waiting on the real-time background
    loop to fire one itself."""

    async def _tick() -> None:
        async with daemon.server.state_lock:
            await control_tick(daemon.ctx)

    return _tick
