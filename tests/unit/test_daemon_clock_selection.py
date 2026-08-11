"""_select_clock() (daemon/app.py) picks WHICH Clock a fresh daemon process
runs on, based on the current hardware mapping. This file covers the
SimulatorClock branch's own start-value choice specifically -- see that
branch's inline comment for why it's anchored to real wall-clock "now"
rather than SimulatorClock's own start=0.0 default.
"""
from __future__ import annotations

import time

from krauken.contracts.clock import SimulatorClock
from krauken.daemon.app import _select_clock
from krauken.db.connection import open_rw
from krauken.db.migrate import migrate


def _map_simulator_chamber(db_path) -> None:
    migrate(db_path)
    conn = open_rw(db_path)
    try:
        conn.execute(
            "UPDATE hardware_config SET platform = 'simulator', device_id = 'simulator:chamber' "
            "WHERE role = 'chamber_temp'"
        )
    finally:
        conn.close()


def test_selects_a_simulator_clock_anchored_to_real_now_not_epoch_zero(tmp_path):
    db_path = tmp_path / "krauken.db"
    _map_simulator_chamber(db_path)

    real_before = time.time()
    clock = _select_clock(db_path)
    real_after = time.time()

    assert isinstance(clock, SimulatorClock)
    # Anchored to wall-clock "now" at construction, not 1970-01-01 -- a
    # fermentation started right after a fresh daemon restart must never
    # land decades before real (or previously-seeded) data, which is
    # exactly what the bare start=0.0 default used to produce.
    assert real_before <= clock.now() <= real_after + 0.1


def test_selects_a_production_clock_when_nothing_is_mapped(tmp_path):
    db_path = tmp_path / "krauken.db"
    migrate(db_path)  # every role starts unmapped
    clock = _select_clock(db_path)
    assert not isinstance(clock, SimulatorClock)
