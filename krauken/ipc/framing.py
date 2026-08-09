"""Newline-delimited JSON framing over asyncio streams. One compact JSON
object per line, UTF-8. Deliberately not HTTP -- see protocol.py's module
docstring for why."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

MAX_LINE_BYTES = 1024 * 1024  # 1MB; a malformed/runaway client gets disconnected, not OOM'd


async def read_lines(reader) -> AsyncIterator[dict[str, Any]]:
    while True:
        try:
            line = await reader.readline()
        except (ConnectionResetError, BrokenPipeError):
            return
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            raise ValueError(f"line exceeds {MAX_LINE_BYTES} bytes")
        text = line.decode("utf-8").strip()
        if not text:
            continue
        yield json.loads(text)


async def write_obj(writer, obj: dict[str, Any]) -> None:
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()
