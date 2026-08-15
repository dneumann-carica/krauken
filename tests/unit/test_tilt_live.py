from __future__ import annotations

import pytest

pytest.importorskip("aioblescan")

from krauken.contracts.clock import SimulatorClock  # noqa: E402
from krauken.contracts.models import Health  # noqa: E402
from krauken.platforms.tilt.live import TiltBeerTempSource, TiltGravitySource  # noqa: E402
from krauken.platforms.tilt.scanner import TiltReading, TiltScanner  # noqa: E402


def _scanner_with_reading(clock: SimulatorClock, color: str, temp_f: float, gravity_sg: float) -> TiltScanner:
    s = TiltScanner(clock, hci_device=0)
    s._readings[color] = TiltReading(
        temp_f=temp_f, gravity_sg=gravity_sg, rssi=-58, last_seen_monotonic=clock.monotonic()
    )
    return s


async def test_beer_temp_source_reads_the_only_detected_color():
    clock = SimulatorClock()
    scanner = _scanner_with_reading(clock, "orange", 39.0, 1.008)
    reading = await TiltBeerTempSource(scanner).read()
    assert reading.health == Health.OK
    assert reading.temp_f == 39.0


async def test_gravity_source_reads_the_only_detected_color():
    clock = SimulatorClock()
    scanner = _scanner_with_reading(clock, "orange", 39.0, 1.008)
    reading = await TiltGravitySource(scanner).read()
    assert reading.health == Health.OK
    assert reading.gravity_sg == 1.008


async def test_both_sources_are_unreachable_when_nothing_has_ever_been_detected():
    scanner = TiltScanner(SimulatorClock(), hci_device=0)
    beer = await TiltBeerTempSource(scanner).read()
    gravity = await TiltGravitySource(scanner).read()
    assert beer.health == Health.UNREACHABLE
    assert beer.temp_f is None
    assert gravity.health == Health.UNREACHABLE
    assert gravity.gravity_sg is None


async def test_sources_go_unreachable_once_the_only_detected_color_drops_out():
    from krauken.platforms.tilt.scanner import DROPOUT_TIMEOUT_S

    clock = SimulatorClock()
    scanner = _scanner_with_reading(clock, "orange", 39.0, 1.008)
    clock.advance(DROPOUT_TIMEOUT_S + 1)
    reading = await TiltBeerTempSource(scanner).read()
    assert reading.health == Health.UNREACHABLE


async def test_with_no_device_id_multiple_detected_colors_falls_back_to_first_sorted():
    # Graceful default for a caller with no mapping context at all (see
    # live.py's own docstring) -- every real production call site
    # (daemon/drivers.py) always has a real device_id once a role is
    # actually mapped, so this only matters for a bare, contextless
    # construction like this test's own.
    clock = SimulatorClock()
    scanner = TiltScanner(clock, hci_device=0)
    from krauken.platforms.tilt.scanner import TiltReading

    scanner._readings["purple"] = TiltReading(70.0, 1.020, -60, clock.monotonic())
    scanner._readings["black"] = TiltReading(65.0, 1.010, -55, clock.monotonic())
    reading = await TiltBeerTempSource(scanner).read()
    assert reading.temp_f == 65.0  # "black" sorts before "purple"


async def test_device_id_picks_the_mapped_color_even_when_another_sorts_first():
    # The actual fix: with two Tilts in range, a device_id of "tilt:purple"
    # must read purple's data, not black's -- even though black would win
    # the no-device_id fallback above.
    clock = SimulatorClock()
    scanner = TiltScanner(clock, hci_device=0)
    from krauken.platforms.tilt.scanner import TiltReading

    scanner._readings["purple"] = TiltReading(70.0, 1.020, -60, clock.monotonic())
    scanner._readings["black"] = TiltReading(65.0, 1.010, -55, clock.monotonic())

    purple_beer = await TiltBeerTempSource(scanner, device_id="tilt:purple").read()
    black_beer = await TiltBeerTempSource(scanner, device_id="tilt:black").read()
    assert purple_beer.temp_f == 70.0
    assert black_beer.temp_f == 65.0

    purple_gravity = await TiltGravitySource(scanner, device_id="tilt:purple").read()
    assert purple_gravity.gravity_sg == 1.020


async def test_device_id_for_a_color_not_currently_detected_is_unreachable():
    # Mapped to "tilt:orange", but that Tilt dropped out of range -- must
    # read unreachable, not silently fall back to some OTHER detected
    # color the user never actually assigned to this role.
    clock = SimulatorClock()
    scanner = TiltScanner(clock, hci_device=0)
    from krauken.platforms.tilt.scanner import TiltReading

    scanner._readings["black"] = TiltReading(65.0, 1.010, -55, clock.monotonic())
    reading = await TiltBeerTempSource(scanner, device_id="tilt:orange").read()
    assert reading.health == Health.UNREACHABLE
    assert reading.temp_f is None
