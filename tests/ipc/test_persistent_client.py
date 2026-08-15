"""PersistentIPCClient against a real (if bare-bones) IPCServer -- no daemon
involved. Covers the properties platforms/ipc_driver.py's Ipc* classes rely
on: concurrent calls sharing one connection, PlatformUnavailable while
disconnected, and recovering once the far end comes back."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from krauken.contracts.errors import KraukenError, PlatformUnavailable, ValidationError
from krauken.ipc.persistent_client import PersistentIPCClient
from krauken.ipc.server import IPCServer, op


def _short_socket_dir() -> Path:
    # AF_UNIX path length limit (~104 bytes on macOS/BSD) -- pytest's own
    # tmp_path nests too deep for a socket specifically (see
    # tests/api/conftest.py's `daemon` fixture, same issue).
    return Path(tempfile.mkdtemp(prefix="kr-ipc-"))


@op("test.echo")
async def _echo(ctx, args):
    return {"echo": args.get("value")}


@op("test.sleep")
async def _sleep(ctx, args):
    await asyncio.sleep(args.get("seconds", 0))
    return {"slept": args.get("seconds", 0)}


@op("test.boom")
async def _boom(ctx, args):
    raise ValidationError("deliberately broken")


@pytest_asyncio.fixture
async def server():
    socket_dir = _short_socket_dir()
    srv = IPCServer(socket_dir / "test.sock", ctx=None)
    await srv.start()
    yield srv
    await srv.stop()
    shutil.rmtree(socket_dir, ignore_errors=True)


async def test_call_round_trips(server):
    client = PersistentIPCClient(server.socket_path, heartbeat_interval_s=100)
    await client.start()
    try:
        # start() doesn't wait for the connection -- give the background
        # connector a moment before asserting on it.
        await asyncio.sleep(0.05)
        assert client.connected
        result = await client.call("test.echo", {"value": 42})
        assert result == {"echo": 42}
    finally:
        await client.stop()


async def test_concurrent_calls_share_one_connection(server):
    client = PersistentIPCClient(server.socket_path, heartbeat_interval_s=100)
    await client.start()
    try:
        await asyncio.sleep(0.05)
        results = await asyncio.gather(
            client.call("test.echo", {"value": 1}),
            client.call("test.echo", {"value": 2}),
            client.call("test.sleep", {"seconds": 0.05}),
        )
        assert results == [{"echo": 1}, {"echo": 2}, {"slept": 0.05}]
    finally:
        await client.stop()


async def test_server_error_reraises_with_original_code(server):
    # Same contract as AsyncIPCClient.call(): the exact exception TYPE never
    # crosses the wire, only its .code string -- the client always
    # reconstructs a bare KraukenError with .code set explicitly, never the
    # original ValidationError subclass.
    client = PersistentIPCClient(server.socket_path, heartbeat_interval_s=100)
    await client.start()
    try:
        await asyncio.sleep(0.05)
        with pytest.raises(KraukenError) as exc_info:
            await client.call("test.boom")
        assert exc_info.value.code == "validation_error"
    finally:
        await client.stop()


async def test_call_raises_platform_unavailable_before_first_connect():
    client = PersistentIPCClient("/tmp/krauken-test-does-not-exist.sock", heartbeat_interval_s=100)
    with pytest.raises(PlatformUnavailable):
        await client.call("test.echo", {"value": 1})


async def test_reconnects_after_connection_drop():
    # IPCServer.stop() deliberately leaves already-accepted connections
    # open (that's asyncio.Server.close()'s own documented behavior --
    # "close listening sockets"; "existing incoming client connections are
    # left open") -- stopping the server alone would never actually
    # disconnect this client, so the drop has to be simulated from the
    # client's own transport instead. That's what a real crash/restart on
    # the far end looks like from this side too: the transport just stops
    # working, with no clean FIN.
    socket_dir = _short_socket_dir()
    socket_path = socket_dir / "restart.sock"
    server = IPCServer(socket_path, ctx=None)
    await server.start()

    client = PersistentIPCClient(
        socket_path, heartbeat_interval_s=0.05, reconnect_backoff_s=0.05, reconnect_backoff_max_s=0.05
    )
    await client.start()
    try:
        await asyncio.sleep(0.1)
        assert await client.call("test.echo", {"value": "before"}) == {"echo": "before"}

        client._writer.transport.abort()  # noqa: SLF001 -- simulating a severed connection

        # The reader task should notice EOF almost immediately (well under
        # one heartbeat interval) and start reconnecting on its own.
        for _ in range(100):
            if not client.connected:
                break
            await asyncio.sleep(0.01)
        assert not client.connected
        with pytest.raises(PlatformUnavailable):
            await client.call("test.echo", {"value": "during outage"})

        # Same socket path, fresh listener -- a real process restart.
        await server.stop()
        server = IPCServer(socket_path, ctx=None)
        await server.start()
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.05)
        assert client.connected
        assert await client.call("test.echo", {"value": "after"}) == {"echo": "after"}
    finally:
        await client.stop()
        await server.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)
