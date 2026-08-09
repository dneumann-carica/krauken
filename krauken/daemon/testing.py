"""Scenario-test composition root. build_scenario_daemon() calls the exact
same build_daemon() factory production uses, just parameterized with a
SimulatorClock and (usually) a much coarser control-tick interval -- there
is no separate test-only code path inside the daemon itself, only different
constructor arguments. That coarser interval matters for test speed: a
30-second production tick interval would mean tens of thousands of ticks to
cover a multi-week fermentation; SimulatorClock.sleep() doesn't really wait,
but the control loop's while-True still executes real Python + SQLite work
on every single iteration, so fewer, coarser ticks make a compressed-time
scenario test fast without changing what it's testing.
"""
from __future__ import annotations

from pathlib import Path

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.control_constants import ControlTuning
from krauken.daemon.app import Daemon, build_daemon

# An arbitrary-but-real past Unix timestamp (2023-11-14T22:13:20Z) rather
# than epoch 0 -- purely so timestamps in test output/debugging look like
# real dates instead of 1970-01-01.
DEFAULT_SCENARIO_START_TS = 1_700_000_000.0


def build_scenario_daemon(
    *,
    db_path: Path,
    socket_path: Path,
    start_ts: float = DEFAULT_SCENARIO_START_TS,
    control_tick_interval_s: float = 600.0,
    control_tuning: ControlTuning | None = None,
) -> tuple[Daemon, SimulatorClock]:
    clock = SimulatorClock(start=start_ts)
    daemon = build_daemon(
        db_path=db_path,
        clock=clock,
        socket_path=socket_path,
        heartbeat_interval_s=3600.0,
        control_tick_interval_s=control_tick_interval_s,
        control_tuning=control_tuning,
    )
    return daemon, clock
