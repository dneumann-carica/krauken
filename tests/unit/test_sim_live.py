from __future__ import annotations

import pytest

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.control_constants import ControlTuning
from krauken.contracts.models import ChamberMode
from krauken.platforms.simulator.live import (
    AMBIENT_PRESETS,
    SimBeerTempSource,
    SimChamberDriver,
    SimGravitySource,
    SimPlantEngine,
)
from krauken.platforms.simulator.plant import AmbientParams, PlantParams

TUNING = ControlTuning(min_on_s=10, min_off_s=10, opposite_lockout_s=20)


def _engine(clock: SimulatorClock) -> SimPlantEngine:
    return SimPlantEngine(clock, params=PlantParams(chamber_start_f=69.0, beer_start_f=68.0), tuning=TUNING)


async def test_chamber_engages_and_cools_toward_the_commanded_target():
    clock = SimulatorClock()
    engine = _engine(clock)
    driver = SimChamberDriver(engine)

    await driver.set_target(60.0)  # well below current chamber temp -- should cool
    clock.advance(15)  # past min_off (was idle/never-run, so min_off applies -- satisfied)
    reading = await driver.read_chamber()
    assert reading.mode == ChamberMode.COOL
    assert reading.temp_f < 69.0  # actually moved toward the target


async def test_min_on_keeps_the_relay_engaged_even_if_target_clears():
    clock = SimulatorClock()
    engine = _engine(clock)
    driver = SimChamberDriver(engine)

    await driver.set_target(60.0)
    clock.advance(15)
    first = await driver.read_chamber()
    assert first.mode == ChamberMode.COOL

    await driver.set_target(None)  # daemon releases immediately
    clock.advance(2)  # short of min_on (10s) since engaging
    still_running = await driver.read_chamber()
    assert still_running.mode == ChamberMode.COOL  # min-on protects it anyway

    clock.advance(20)
    released = await driver.read_chamber()
    assert released.mode == ChamberMode.IDLE


async def test_opposite_lockout_blocks_an_immediate_reversal():
    clock = SimulatorClock()
    engine = _engine(clock)
    driver = SimChamberDriver(engine)

    await driver.set_target(60.0)
    clock.advance(15)
    assert (await driver.read_chamber()).mode == ChamberMode.COOL

    await driver.set_target(None)
    clock.advance(15)  # satisfies min_on -- releases to idle
    assert (await driver.read_chamber()).mode == ChamberMode.IDLE

    await driver.set_target(80.0)  # now wants heat -- opposite of the last cool run
    clock.advance(10)  # satisfies min_off (10s) but NOT opposite_lockout (20s)
    assert (await driver.read_chamber()).mode == ChamberMode.IDLE
    clock.advance(15)  # now past opposite_lockout (25s total since idle began)
    assert (await driver.read_chamber()).mode == ChamberMode.HEAT


async def test_idle_chamber_drifts_toward_ambient_not_toward_a_stale_target():
    clock = SimulatorClock()
    engine = _engine(clock)
    driver = SimChamberDriver(engine)
    baseline = (await driver.read_chamber()).temp_f

    await driver.set_target(None)
    clock.advance(3600)
    reading = await driver.read_chamber()
    # ambient_f's base is 74F, well above the 69F start -- an idle chamber
    # with no target should drift up toward it, not sit frozen or drift cold.
    assert reading.temp_f > baseline


def test_set_ambient_location_swaps_the_preset():
    clock = SimulatorClock()
    engine = _engine(clock)
    assert engine.params.ambient == AmbientParams()  # generic default to start

    engine.set_ambient_location("Garage")
    assert engine.params.ambient == AMBIENT_PRESETS["Garage"]

    engine.set_ambient_location("Kitchen")
    assert engine.params.ambient == AMBIENT_PRESETS["Kitchen"]


def test_set_ambient_location_falls_back_to_generic_default_for_unknown_or_unset():
    clock = SimulatorClock()
    engine = _engine(clock)
    engine.set_ambient_location("Garage")

    engine.set_ambient_location(None)
    assert engine.params.ambient == AmbientParams()

    engine.set_ambient_location("Nonexistent Place")
    assert engine.params.ambient == AmbientParams()


async def test_a_colder_ambient_preset_pulls_an_idle_chamber_down_further():
    clock = SimulatorClock()
    engine = _engine(clock)
    engine.set_ambient_location("Garage")  # base_f=62, well below the 69F chamber start
    driver = SimChamberDriver(engine)

    await driver.set_target(None)
    clock.advance(3600 * 6)
    reading = await driver.read_chamber()
    assert reading.temp_f < 69.0  # drifted down toward the colder garage ambient, not up toward 74F


async def test_beer_and_gravity_sources_share_the_same_engine_state():
    clock = SimulatorClock()
    engine = _engine(clock)
    chamber = SimChamberDriver(engine)
    beer = SimBeerTempSource(engine)
    gravity = SimGravitySource(engine)

    await chamber.set_target(60.0)
    clock.advance(3600 * 40)  # let exotherm + gravity drop meaningfully
    await chamber.read_chamber()  # advance chamber's own tracker so beer's coupling term sees it moved

    beer_reading = await beer.read()
    gravity_reading = await gravity.read()
    assert beer_reading.temp_f == pytest.approx(engine.beer_temp_f)
    assert gravity_reading.gravity_sg < PlantParams().gravity.og  # dropped from OG


# --- Independent per-role tracking: the actual fix for the "reading beer
# also re-runs chamber's relay/physics" bug (see live.py's module docstring)


async def test_reading_beer_and_gravity_does_not_advance_chambers_physics_or_relay():
    clock = SimulatorClock()
    engine = _engine(clock)
    chamber = SimChamberDriver(engine)
    beer = SimBeerTempSource(engine)
    gravity = SimGravitySource(engine)

    await chamber.set_target(60.0)
    clock.advance(15)
    engaged = await chamber.read_chamber()
    assert engaged.mode == ChamberMode.COOL
    temp_after_chamber_read = engine.chamber_temp_f
    relay_held_s_after_chamber_read = engine.relay.held_s

    # Advance real elapsed time and read beer/gravity several times WITHOUT
    # ever reading chamber again -- under the old shared-tick design this
    # would silently re-run next_relay_state() and the chamber Euler step
    # too, since all three reads funneled through one shared _tick().
    clock.advance(5)
    await beer.read()
    await gravity.read()
    await beer.read()

    assert engine.chamber_temp_f == temp_after_chamber_read
    assert engine.relay.held_s == relay_held_s_after_chamber_read


async def test_reading_chamber_does_not_advance_beers_physics():
    clock = SimulatorClock()
    engine = _engine(clock)
    chamber = SimChamberDriver(engine)
    beer = SimBeerTempSource(engine)

    await beer.read()
    beer_temp_after_first_read = engine.beer_temp_f

    clock.advance(3600)
    await chamber.set_target(60.0)
    await chamber.read_chamber()
    await chamber.read_chamber()

    # Beer was never read again -- its own tracker never saw this elapsed
    # time, so its temp must not have moved just because chamber ticked.
    assert engine.beer_temp_f == beer_temp_after_first_read


async def test_gravity_reads_are_jittered_and_trend_toward_the_new_terminal():
    clock = SimulatorClock()
    engine = _engine(clock)
    gravity = SimGravitySource(engine)

    readings = [(await gravity.read()).gravity_sg for _ in range(20)]
    # Freshly re-rolled jitter every read -- 20 consecutive reads at the
    # same instant should not all be identical.
    assert len(set(readings)) > 1

    clock.advance(3600 * 200)  # well past midpoint_h -- should be near terminal
    late_reading = (await gravity.read()).gravity_sg
    assert late_reading == pytest.approx(PlantParams().gravity.terminal, abs=0.01)
    assert late_reading < 1.02  # nowhere near OG (1.052) or the old terminal (1.011)
