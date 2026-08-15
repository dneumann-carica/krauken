"""FastAPI dependencies. All reads go through run_ro() below, which opens a
fresh SQLite connection and runs the whole query in ONE threadpool call --
writes proxy to the daemon over IPC and never touch SQLite directly from
this process (krauken.db.writes is daemon-only, enforced by
tests/db/test_write_boundary.py).

One deliberate exception to "the API only ever talks to the daemon and
SQLite": dev-panel ops (api/routers/dev_panel.py) go straight to
Simulator's/Manual's own out-of-process servers via simulator()/manual()
below, not through the daemon. Those ops mutate state (health, outlet
on/off, probe2) that belongs to the platform process itself, not to
anything the daemon owns -- proxying them through the daemon would just be
relay code with nothing daemon-specific to add, and would mean touching the
ChamberDriver/BeerTempSource/GravitySource Protocols to carry dev-tooling
concerns they have no business knowing about (see platforms/ipc_service.py's
own module docstring).

There used to be a `db_ro()` Depends(...) generator with a thread-local
cached connection, on the theory that a plain `def` endpoint's dependency
resolution and body always land on the same threadpool worker. That theory
was wrong: FastAPI resolves a sync generator dependency and calls the
endpoint body as two SEPARATE anyio.to_thread.run_sync calls, and the
thread pool is free to hand those to different worker threads. Sequential
single-request tests kept reusing the pool's one idle thread and never
caught it; a real page load firing several concurrent requests did,
immediately, with "SQLite objects created in a thread can only be used in
that same thread". run_ro() sidesteps the whole class of bug by never
letting a connection cross a threadpool-call boundary at all.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, TypeVar

import anyio.to_thread

from krauken.config import Config
from krauken.db.connection import open_ro
from krauken.ipc.client import AsyncIPCClient

_config = Config.from_env()

T = TypeVar("T")


def get_config() -> Config:
    return _config


def _open_and_run(fn: Callable[..., T], args: tuple) -> T:
    conn = open_ro(_config.db_path)
    try:
        return fn(conn, *args)
    finally:
        conn.close()


async def run_ro(fn: Callable[..., T], *args) -> T:
    """For `async def` endpoints that need a synchronous SQLite read
    alongside an `await` (e.g. a daemon IPC call) -- opens a fresh
    connection and runs the whole query in ONE threadpool call, so the
    connection is never touched from a different thread than it was
    created on."""
    return await anyio.to_thread.run_sync(_open_and_run, fn, args)


def daemon() -> AsyncIPCClient:
    return AsyncIPCClient(_config.daemon_socket)


def simulator() -> AsyncIPCClient:
    """Straight to the Simulator's own process for dev-panel ops
    (api/routers/dev_panel.py) -- NOT proxied through the daemon. The
    daemon separately holds its own persistent connection to the same
    process for live control (platforms/registry.py's PLATFORM_BINDINGS);
    this is a second, independent, connection-per-call client, same shape
    as daemon() above, just pointed at a different socket."""
    return AsyncIPCClient(_config.simulator_socket)


def manual() -> AsyncIPCClient:
    """Manual's own process, mirroring simulator() above."""
    return AsyncIPCClient(_config.manual_socket)
