from __future__ import annotations

import pytest

pytest.importorskip("aioblescan")

from krauken.contracts.clock import SimulatorClock  # noqa: E402
from krauken.contracts.roles import Role  # noqa: E402
from krauken.platforms.tilt.platform import TiltPlatform  # noqa: E402
from krauken.platforms.tilt.scanner import TiltReading, TiltScanner  # noqa: E402


def _running_scanner_with(clock: SimulatorClock, **colors_to_readings) -> TiltScanner:
    s = TiltScanner(clock, hci_device=0)
    s._btctrl = object()  # anything not-None -- `running` should read True without a real socket
    for color, (temp_f, gravity_sg) in colors_to_readings.items():
        s._readings[color] = TiltReading(temp_f, gravity_sg, rssi=-58, last_seen_monotonic=clock.monotonic())
    return s


async def test_discover_surfaces_a_candidate_for_a_detected_color():
    clock = SimulatorClock()
    scanner = _running_scanner_with(clock, orange=(39.0, 1.008))
    candidates = await TiltPlatform(scanner).discover({})
    assert len(candidates) == 1
    c = candidates[0]
    assert c.device_id == "tilt:orange"
    assert c.platform == "tilt"
    assert c.capabilities == frozenset({Role.BEER_TEMP, Role.BEER_GRAVITY})
    assert c.bundled_roles == frozenset()  # Tilt is never bundled
    assert c.readings == {"beer_temp_f": 39.0, "gravity_sg": 1.008}


async def test_discover_surfaces_every_detected_color_as_its_own_candidate():
    clock = SimulatorClock()
    scanner = _running_scanner_with(clock, orange=(39.0, 1.008), purple=(65.0, 1.020))
    candidates = await TiltPlatform(scanner).discover({})
    device_ids = {c.device_id for c in candidates}
    assert device_ids == {"tilt:orange", "tilt:purple"}


async def test_discover_reports_nothing_when_no_tilt_is_in_range():
    scanner = _running_scanner_with(SimulatorClock())
    candidates = await TiltPlatform(scanner).discover({})
    assert candidates == []
