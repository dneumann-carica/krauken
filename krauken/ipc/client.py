"""IPC client. Connection-per-call: connecting to a unix socket is ~30us and
the API tier's call rate is a handful per second, so there's no reconnect
state machine, no half-open detection, and a daemon restart is invisible to
the next call. (The daemon->supervisor hop, once the supervisor exists, is
the opposite -- one persistent connection with heartbeats, since that link's
liveness is itself semantically meaningful.)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

from krauken.contracts.errors import DaemonUnavailable, KraukenError
from krauken.ipc import framing, protocol


class AsyncIPCClient:
    def __init__(self, socket_path: Path | str, *, connect_timeout: float = 1.0):
        self.socket_path = str(socket_path)
        self.connect_timeout = connect_timeout

    async def call(self, op_name: str, args: Mapping[str, Any] | None = None, *, deadline_ms: int = 3000) -> Any:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), self.connect_timeout
            )
        except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
            raise DaemonUnavailable(f"cannot reach daemon at {self.socket_path}: {e}") from e

        try:
            req = protocol.new_request(op_name, args, deadline_ms=deadline_ms)
            await framing.write_obj(writer, req)
            async for resp in framing.read_lines(reader):
                if resp.get("id") != req["id"]:
                    continue  # not ours (shouldn't happen on a fresh connection-per-call, but be safe)
                if resp.get("ok"):
                    return resp.get("result")
                error = resp.get("error", {})
                # The daemon's exact exception TYPE never crosses the wire,
                # only its .code string -- reconstructing a bare
                # KraukenError() here would silently lose that code (it
                # defaults to "internal_error"), which breaks the API
                # tier's error->HTTP-status mapping for every daemon-raised
                # error. Set .code explicitly from what's on the wire; the
                # API side maps by this string, not by Python type, for
                # exactly this reason.
                exc = KraukenError(error.get("message", "unknown error"), error.get("details"))
                exc.code = error.get("code", "internal_error")
                raise exc
            raise DaemonUnavailable("daemon closed the connection without responding")
        finally:
            writer.close()
