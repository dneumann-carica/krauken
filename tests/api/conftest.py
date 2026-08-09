from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from krauken.api import deps
from krauken.api.app import create_app
from krauken.config import Config
from krauken.daemon.app import Daemon, build_daemon


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    db_path = tmp_path / "krauken.db"
    # AF_UNIX socket paths have a real length limit (~104 bytes on macOS/BSD)
    # -- pytest's own tmp_path is nested deep enough (pytest-of-<user>/
    # pytest-N/<test-name>N/...) to exceed it, which fails with a genuinely
    # confusing "AF_UNIX path too long" OSError. Sockets specifically need a
    # short-path temp dir; the DB file has no such constraint and stays
    # under tmp_path.
    short_tmp = Path(tempfile.mkdtemp(prefix="kr-"))
    socket_path = short_tmp / "d.sock"
    d = build_daemon(db_path=db_path, socket_path=socket_path, heartbeat_interval_s=3600)
    await d.start()
    yield d
    await d.stop()
    shutil.rmtree(short_tmp, ignore_errors=True)


@pytest_asyncio.fixture
async def client(daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    # Point the API tier's dependencies at the SAME db/socket the daemon
    # fixture just created -- this is a real integration test (real HTTP ->
    # real IPC -> real daemon -> real SQLite), nothing mocked.
    test_config = Config(
        db_path=daemon.ctx.db_path,
        daemon_socket=daemon.server.socket_path,
        supervisor_socket=Path(f"/tmp/krauken-test-supervisor-{uuid.uuid4().hex}.sock"),
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
