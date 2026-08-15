from __future__ import annotations

import time

import pytest

from krauken.contracts.clock import ProductionClock, RemoteClock, SimulatorClock


def test_production_clock_tracks_real_time():
    clock = ProductionClock()
    before = clock.monotonic()
    real_before = time.monotonic()
    assert clock.monotonic() - before == pytest.approx(time.monotonic() - real_before, abs=0.05)


async def test_production_clock_sleep_really_waits():
    clock = ProductionClock()
    real_before = time.monotonic()
    await clock.sleep(0.05)
    assert time.monotonic() - real_before == pytest.approx(0.05, abs=0.03)


def test_simulator_clock_starts_at_the_given_value():
    clock = SimulatorClock(start=1_700_000_000.0)
    assert clock.now() == 1_700_000_000.0
    assert clock.monotonic() == 1_700_000_000.0


def test_simulator_clock_advance_moves_now_and_monotonic_together():
    clock = SimulatorClock()
    clock.advance(3600.0)
    assert clock.now() == pytest.approx(3600.0)
    assert clock.monotonic() == pytest.approx(3600.0)


async def test_simulator_clock_sleep_advances_fully_with_no_real_wait():
    clock = SimulatorClock()
    real_before = time.monotonic()
    await clock.sleep(3600.0 * 24 * 30)  # a full 30 simulated days
    assert clock.monotonic() == pytest.approx(3600.0 * 24 * 30)
    # Genuinely never waited -- this returns in a small fraction of a
    # second regardless of how much simulated time was requested.
    assert time.monotonic() - real_before < 0.5


async def test_simulator_clock_sleep_yields_to_the_event_loop():
    # A bare advance() with no suspension point would busy-spin any
    # `while True: ...; await clock.sleep(...)` loop, starving every other
    # task -- confirm sleep() actually yields at least once.
    import asyncio

    clock = SimulatorClock()
    ran = []

    async def other_task():
        ran.append("other")

    task = asyncio.create_task(other_task())
    await clock.sleep(1.0)
    await task
    assert ran == ["other"]


def test_remote_clock_starts_from_a_real_wall_clock_snapshot():
    before = time.monotonic()
    clock = RemoteClock()
    assert clock.monotonic() == pytest.approx(before, abs=0.5)


def test_remote_clock_set_updates_both_fields():
    clock = RemoteClock()
    clock.set(now=1_700_000_000.0, monotonic=1_700_000_000.0)
    assert clock.now() == 1_700_000_000.0
    assert clock.monotonic() == 1_700_000_000.0


def test_remote_clock_fires_on_first_sync_exactly_once():
    # The real bug this guards against: a consumer (SimPlantEngine) anchors
    # its own elapsed-time state via this clock at construction time, using
    # whatever real-wall-clock value RemoteClock started with -- then the
    # daemon's first sync can jump monotonic() to a wildly different value
    # (SimulatorClock's own start=1_700_000_000-ish epoch). on_first_sync
    # is the hook a consumer uses to re-anchor itself the moment that
    # happens, exactly once, not on every subsequent sync.
    clock = RemoteClock()
    calls = []
    clock.on_first_sync = lambda: calls.append(clock.monotonic())

    clock.set(now=1_700_000_000.0, monotonic=1_700_000_000.0)
    assert calls == [1_700_000_000.0]  # fired, AFTER the new value was already applied

    clock.set(now=1_700_000_060.0, monotonic=1_700_000_060.0)
    assert calls == [1_700_000_000.0]  # not fired again


def test_remote_clock_with_no_on_first_sync_set_is_a_safe_no_op():
    clock = RemoteClock()
    clock.set(now=1.0, monotonic=1.0)  # must not raise with on_first_sync left None
    assert clock.now() == 1.0
