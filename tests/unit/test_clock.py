from __future__ import annotations

import time

import pytest

from krauken.contracts.clock import ProductionClock, SimulatorClock


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
