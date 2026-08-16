"""Unix-domain-socket IPC server. Ops register via the @op decorator; mutating
ops take `ctx.state_lock` for their duration so they never interleave with an
in-progress control-loop tick -- see krauken.daemon.control_loop (M2) for the
other side of that lock. Long-running work (a discovery scan, an outlet fire
test) must be started as a background task and exposed as a pollable job,
never awaited inline here -- an inline 10s BLE scan would stall every other
op, including `system.ping` health checks.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from krauken.contracts.errors import KraukenError
from krauken.ipc import framing, protocol

log = logging.getLogger("krauken.ipc.server")

Handler = Callable[[Any, Mapping[str, Any]], Awaitable[Any]]

# name -> (handler, mutating). A plain module-level dict is intentionally not
# a plugin-discovery mechanism -- this is one process's op table, greppable.
OPS: dict[str, tuple[Handler, bool]] = {}


def op(name: str, *, mutating: bool = False):
    def deco(fn: Handler) -> Handler:
        OPS[name] = (fn, mutating)
        return fn

    return deco


class IPCServer:
    def __init__(self, socket_path: Path | str, ctx: Any):
        self.socket_path = Path(socket_path)
        self.ctx = ctx
        self.state_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        # Tracked explicitly (not relying on asyncio.Server's own
        # close_clients()/abort_clients(), Python 3.13+-only, while this
        # project supports 3.11+) -- confirmed via Python's own docs:
        # Server.close() only stops the LISTENING socket; existing client
        # connections are left open. Since Python 3.12, Server.wait_closed()
        # ALSO waits for those to finish -- a real behavior change from
        # pre-3.12, where it returned as soon as the listener closed. A
        # long-lived client (e.g. the daemon's own persistent
        # IpcPlatformConnection to this exact service, sitting idle
        # between polls) never gets told to close on its own, so
        # wait_closed() would otherwise hang until systemd's
        # TimeoutStopSec gives up and SIGKILLs the process -- measured on
        # real hardware: exactly 90s, on both krauken-manual and
        # krauken-simulator, every single restart (nearly half of one
        # real install's total time).
        self._connections: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_conn, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o660)
        log.info("IPC server listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()  # stop accepting new connections first, avoiding a race with one arriving mid-shutdown
        # Abort every currently-open connection -- deliberately not a
        # graceful close (no waiting for in-flight ops to finish): a
        # dropped connection mid-request is already a case every IPC
        # client here handles (framing.read_lines raising, or the
        # daemon's own "one bad tick must never kill control"
        # resilience retrying next tick), so there's nothing to gain by
        # waiting, and everything to lose (this exact hang).
        for writer in list(self._connections):
            with contextlib.suppress(Exception):
                writer.close()
        if self._server is not None:
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.add(writer)
        try:
            async for raw in framing.read_lines(reader):
                await self._dispatch(raw, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._connections.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, raw: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        try:
            req = protocol.decode_request(raw)
        except (KeyError, TypeError) as e:
            await framing.write_obj(writer, protocol.err("unknown", "malformed_request", str(e)))
            return

        entry = OPS.get(req.op)
        if entry is None:
            await framing.write_obj(writer, protocol.err(req.id, "unknown_op", req.op))
            return
        handler, mutating = entry

        try:
            if mutating:
                async with self.state_lock:
                    result = await asyncio.wait_for(handler(self.ctx, req.args), req.deadline_s)
            else:
                result = await asyncio.wait_for(handler(self.ctx, req.args), req.deadline_s)
            await framing.write_obj(writer, protocol.ok(req.id, result))
        except asyncio.TimeoutError:
            await framing.write_obj(writer, protocol.err(req.id, "deadline_exceeded", f"{req.op} exceeded its deadline"))
        except KraukenError as e:
            await framing.write_obj(writer, protocol.err(req.id, e.code, e.message, e.details))
        except Exception as e:  # noqa: BLE001 -- last-resort boundary, must not crash the server
            log.exception("op %s failed", req.op)
            await framing.write_obj(writer, protocol.err(req.id, "internal_error", str(e)))


@op("system.ping")
async def _ping(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"pong": True}
