"""Direct, isolated unit coverage for control_loop.py's
_dt_h_since_last_tick() -- the PI integral's dt_h source. Deliberately NOT
a full scenario test: this needs nothing but a ControlState instance, no
DB/IPC/simulator physics at all, so the monotonic-clock wiring itself
(distinct from contracts/cascade.py's own already-tested pure functions)
gets fast, exact, deterministic coverage rather than being inferred from a
physics-dependent scenario run.
"""
from __future__ import annotations

from types import SimpleNamespace

from krauken.daemon.control_loop import _dt_h_since_last_tick
from krauken.daemon.control_state import ControlState


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(control_state=ControlState())


def test_first_ever_tick_contributes_zero_dt_h():
    # last_tick_monotonic starts None (fresh ControlState) -- no prior
    # tick to measure from, so this must not fabricate/guess an interval.
    ctx = _ctx()
    assert _dt_h_since_last_tick(ctx, now_mono=1000.0) == 0.0


def test_first_ever_tick_still_records_its_own_monotonic_reading():
    ctx = _ctx()
    _dt_h_since_last_tick(ctx, now_mono=1000.0)
    assert ctx.control_state.last_tick_monotonic == 1000.0


def test_second_tick_reports_the_real_elapsed_hours():
    ctx = _ctx()
    _dt_h_since_last_tick(ctx, now_mono=1000.0)
    dt_h = _dt_h_since_last_tick(ctx, now_mono=1000.0 + 1800.0)  # 30 real minutes later
    assert dt_h == 0.5


def test_dt_h_reflects_whatever_the_real_gap_was_not_a_nominal_interval():
    # An irregularly-late tick (some slow operation delayed it) must
    # report ITS real gap, not a fixed assumed interval -- exactly what
    # makes the integral a true F-hours accumulation rather than an
    # F-per-tick counter.
    ctx = _ctx()
    _dt_h_since_last_tick(ctx, now_mono=0.0)
    dt_h = _dt_h_since_last_tick(ctx, now_mono=7200.0)  # 2 real hours, not the nominal 30s
    assert dt_h == 2.0


def test_each_call_measures_from_the_immediately_preceding_call_not_the_first_one():
    ctx = _ctx()
    _dt_h_since_last_tick(ctx, now_mono=0.0)
    _dt_h_since_last_tick(ctx, now_mono=1800.0)  # 0.5h since the first
    dt_h = _dt_h_since_last_tick(ctx, now_mono=3600.0)  # only 0.5h since the SECOND, not 1.0h since the first
    assert dt_h == 0.5
