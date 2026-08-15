"""What's left of the daemon's own dev-panel op surface once
manual.*/simulator.* moved to their own out-of-process servers (see
platforms/manual/service.py, platforms/simulator/service.py, and
api/routers/dev_panel.py's own module docstring for why): just
dev_panel.get_clock, which reports the DAEMON's own clock selection (real
vs. compressed -- daemon/app.py's _select_clock) rather than any
Manual/Simulator state, so it never belonged to either of those processes
in the first place.
"""
from __future__ import annotations

from typing import Any, Mapping

from krauken.daemon.timefmt import iso_now
from krauken.ipc.server import op


@op("dev_panel.get_clock")
async def _get_clock(ctx: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"now": iso_now(ctx.clock)}
