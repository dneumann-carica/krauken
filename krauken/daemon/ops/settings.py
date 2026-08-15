"""IPC ops for the generic settings table. Only one key exists today
(chamber_location, written by the Hardware Setup wizard) but the op and
storage are generic -- a single key/value table, not a bespoke column --
since the next setting won't be the last.
"""
from __future__ import annotations

from typing import Any, Mapping

from krauken.daemon.timefmt import iso_now as _iso_now
from krauken.db import writes
from krauken.ipc.server import op


@op("settings.save", mutating=True)
async def _settings_save(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    key, value = args["key"], args["value"]
    async with ctx.db_lock:
        writes.write_setting(ctx.conn, key, value, _iso_now(ctx.clock))
    return {"key": key, "value": value}
