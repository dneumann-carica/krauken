"""daemon/ops/dev_panel.py's own remaining op, dev_panel.get_clock -- the
one dev-panel concern that's actually a daemon-level fact (which Clock
implementation the daemon selected), not Manual/Simulator state. The
manual.*/simulator.* ops that used to live here moved to their own
out-of-process servers; see tests/unit/test_manual_service.py and
tests/unit/test_simulator_service.py for their coverage now."""
from __future__ import annotations

from krauken.contracts.clock import SimulatorClock
from krauken.daemon.ops.dev_panel import _get_clock


class _FakeCtx:
    def __init__(self):
        self.clock = SimulatorClock()


async def test_get_clock_reads_the_current_time():
    ctx = _FakeCtx()
    ctx.clock.advance(3600.0)
    result = await _get_clock(ctx, {})
    assert result["now"]  # a real ISO timestamp string, non-empty
