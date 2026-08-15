"""Persistent unix-socket IPC client: one long-lived connection per remote
process (Simulator, Manual, and eventually the real Hardware Supervisor) --
unlike client.py's AsyncIPCClient (connection-per-call, right for API->
daemon, where a daemon restart should be invisible to a caller), this
link's liveness IS meaningful. The daemon needs to know whether it's
actually still in control of a given piece of hardware, not just whether
one call happened to succeed -- client.py's own module docstring named
this exact split before either side of it existed.

One reader task per connection demultiplexes responses onto whichever
call() is waiting for that request id, by id -- concurrent calls (e.g.
reading chamber/beer/gravity back-to-back in one control tick) share the
one connection instead of opening three. A background loop keeps
system.ping-ing the far end on its own cadence once connected; several
missed heartbeats in a row (not one) trigger a reconnect, so a single slow
response under real load doesn't flap an otherwise-healthy connection.
Reconnection backs off geometrically and never gives up -- there's no
operator watching to notice a one-shot retry failed, the way there would be
for e.g. a CI job.

Never raises out of start()/stop() for "the remote process isn't up yet" --
same reasoning as krauken-daemon.service not being ordered After= the
supervisor unit: a dependency that happens to be down or slow to start must
never block or crash the process depending on it. Every in-flight and
future call() instead raises PlatformUnavailable for as long as that's
true, which read_chamber()/read()/etc. (platforms/ipc_driver.py) already
know how to turn into a Health.UNREACHABLE reading rather than a crash.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Mapping

from krauken.contracts.errors import KraukenError, PlatformUnavailable
from krauken.ipc import framing, protocol

log = logging.getLogger("krauken.ipc.persistent_client")


class PersistentIPCClient:
    def __init__(
        self,
        socket_path: Path | str,
        *,
        connect_timeout: float = 1.0,
        heartbeat_interval_s: float = 5.0,
        missed_heartbeats_before_reconnect: int = 3,
        reconnect_backoff_s: float = 1.0,
        reconnect_backoff_max_s: float = 30.0,
    ):
        self.socket_path = str(socket_path)
        self.connect_timeout = connect_timeout
        self.heartbeat_interval_s = heartbeat_interval_s
        self.missed_heartbeats_before_reconnect = missed_heartbeats_before_reconnect
        self.reconnect_backoff_s = reconnect_backoff_s
        self.reconnect_backoff_max_s = reconnect_backoff_max_s

        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._connector_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._stopping = False
        self._missed_heartbeats = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def start(self) -> None:
        """Kicks off the background connect/reconnect + heartbeat loop and
        returns immediately -- does NOT wait for the first connection to
        succeed."""
        self._stopping = False
        self._connector_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._connector_task is not None:
            self._connector_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connector_task
            self._connector_task = None
        await self._close_connection()

    async def call(self, op_name: str, args: Mapping[str, Any] | None = None, *, deadline_ms: int = 3000) -> Any:
        if self._writer is None:
            raise PlatformUnavailable(f"cannot reach {self.socket_path}: not connected")
        return await self._raw_call(op_name, args, deadline_ms=deadline_ms)

    async def _run(self) -> None:
        backoff = self.reconnect_backoff_s
        while not self._stopping:
            try:
                await self._connect_once()
                backoff = self.reconnect_backoff_s  # reset only after a real success
                await self._heartbeat_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- this loop must never die; log and retry
                log.warning("connection to %s failed/lost: %s", self.socket_path, e)
            await self._close_connection()
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_backoff_max_s)

    async def _connect_once(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path), self.connect_timeout
        )
        self._writer = writer
        self._missed_heartbeats = 0
        self._reader_task = asyncio.create_task(self._read_loop(reader))
        log.info("connected to %s", self.socket_path)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            async for resp in framing.read_lines(reader):
                fut = self._pending.pop(resp.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(resp)
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _heartbeat_until_disconnected(self) -> None:
        """Waits out one heartbeat interval OR the reader task ending,
        whichever comes first -- a connection the peer actually closed
        (crash, restart) surfaces as EOF on the reader almost immediately,
        and must trigger reconnection right then, not sit unnoticed for up
        to a full heartbeat_interval_s until the next ping happens to fail.
        The periodic ping still matters on its own: it's what catches a
        connection that's silently gone bad (a black hole, not a clean
        close) with nothing to read to signal that."""
        while self._writer is not None:
            sleep_task = asyncio.ensure_future(asyncio.sleep(self.heartbeat_interval_s))
            done, _pending = await asyncio.wait({sleep_task, self._reader_task}, return_when=asyncio.FIRST_COMPLETED)
            # Only the sleep is throwaway -- self._reader_task must live for
            # the whole connection, not get cancelled just because it lost
            # this particular race (a real, previously-shipped bug: treating
            # it like sleep_task and cancelling whichever task didn't win
            # killed the reader every single cycle, one heartbeat_interval_s
            # after every connect).
            if sleep_task in _pending:
                sleep_task.cancel()
            if self._reader_task in done:
                raise ConnectionError("connection closed by peer")
            try:
                await self._raw_call("system.ping", {}, deadline_ms=int(self.heartbeat_interval_s * 1000))
                self._missed_heartbeats = 0
            except (PlatformUnavailable, asyncio.TimeoutError):
                self._missed_heartbeats += 1
                if self._missed_heartbeats >= self.missed_heartbeats_before_reconnect:
                    raise ConnectionError(f"missed {self._missed_heartbeats} heartbeats in a row")

    async def _close_connection(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        # Fail every call still waiting on this connection -- they'll never
        # get an answer now, and a caller blocked on .call() must find out
        # promptly rather than hang until its own deadline.
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(PlatformUnavailable(f"connection to {self.socket_path} lost"))

    async def _raw_call(self, op_name: str, args: Mapping[str, Any] | None, *, deadline_ms: int) -> Any:
        writer = self._writer
        if writer is None:
            raise PlatformUnavailable(f"cannot reach {self.socket_path}: not connected")
        req = protocol.new_request(op_name, args, deadline_ms=deadline_ms)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req["id"]] = fut
        try:
            await framing.write_obj(writer, req)
        except (ConnectionResetError, BrokenPipeError) as e:
            self._pending.pop(req["id"], None)
            raise PlatformUnavailable(f"cannot reach {self.socket_path}: {e}") from e

        try:
            resp = await asyncio.wait_for(fut, deadline_ms / 1000.0)
        except asyncio.TimeoutError:
            self._pending.pop(req["id"], None)
            raise

        if resp.get("ok"):
            return resp.get("result")
        error = resp.get("error", {})
        # Same reasoning as AsyncIPCClient.call(): reconstruct .code from
        # the wire, never let it default to "internal_error" for an error
        # the far end labeled more specifically.
        exc = KraukenError(error.get("message", "unknown error"), error.get("details"))
        exc.code = error.get("code", "internal_error")
        raise exc
