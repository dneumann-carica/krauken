from __future__ import annotations

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.models import ChamberMode, Health
from krauken.platforms.manual.live import ManualBeerTempSource, ManualChamberDriver, ManualGravitySource, ManualPanel


async def test_chamber_reading_reflects_whatever_was_hand_set():
    clock = SimulatorClock()
    panel = ManualPanel(clock)
    driver = ManualChamberDriver(panel)

    panel.chamber.temp_f = 61.0
    panel.chamber.mode = "cool"
    reading = await driver.read_chamber()
    assert reading.temp_f == 61.0
    assert reading.mode == ChamberMode.COOL


async def test_set_target_records_but_never_moves_the_hand_set_temp():
    clock = SimulatorClock()
    panel = ManualPanel(clock)
    driver = ManualChamberDriver(panel)
    panel.chamber.temp_f = 68.0

    await driver.set_target(55.0)
    reading = await driver.read_chamber()
    assert reading.commanded_target_f == 55.0
    assert reading.temp_f == 68.0  # unmoved -- no physics here


async def test_beer_and_gravity_sources_read_hand_set_values():
    clock = SimulatorClock()
    panel = ManualPanel(clock)
    beer = ManualBeerTempSource(panel)
    gravity = ManualGravitySource(panel)

    panel.tilt.temp_f = 75.0
    panel.tilt.gravity_sg = 1.020
    panel.tilt.health = Health.DEGRADED

    beer_reading = await beer.read()
    gravity_reading = await gravity.read()
    assert beer_reading.temp_f == 75.0
    assert gravity_reading.gravity_sg == 1.020
    assert gravity_reading.health == Health.DEGRADED


async def test_unhealthy_reading_reports_no_last_good_ts():
    """The one deliberate exception to "purely operator-settable, no
    behavior" -- see the module docstring. This is what lets a developer
    exercise the beer-temp-lost/gravity-lost failsafe paths through the
    dev panel without needing real hardware to actually go unresponsive."""
    clock = SimulatorClock()
    panel = ManualPanel(clock)
    beer = ManualBeerTempSource(panel)

    panel.tilt.health = Health.OK
    assert (await beer.read()).last_good_ts is not None

    panel.tilt.health = Health.UNREACHABLE
    assert (await beer.read()).last_good_ts is None


async def test_unavailable_tilt_reads_as_unreachable_regardless_of_health():
    """The availability toggle simulates the Tilt being out of BLE range or
    powered off entirely -- distinct from (and stronger than) a hand-set
    unhealthy reading, since a real unreachable Tilt has no health field to
    even report."""
    clock = SimulatorClock()
    panel = ManualPanel(clock)
    beer = ManualBeerTempSource(panel)
    gravity = ManualGravitySource(panel)

    panel.tilt.health = Health.OK
    panel.tilt.available = False
    beer_reading = await beer.read()
    gravity_reading = await gravity.read()
    assert beer_reading.health == Health.UNREACHABLE
    assert beer_reading.last_good_ts is None
    assert gravity_reading.health == Health.UNREACHABLE
