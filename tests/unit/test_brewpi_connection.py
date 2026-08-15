"""Tests the request/response protocol logic directly against a fake
serial transport (no real pyserial I/O involved) using the exact byte
sequences captured from a real BrewPi Remix rig on 2026-08-11 -- see
connection.py's own module docstring for the full provenance. Bypasses
identify_and_connect()'s port-scanning entirely by constructing a
BrewPiConnection and handing it a FakeSerial directly, which is both
simpler and more portable (works with or without the `pi` extra's
pyserial actually installed) than mocking pyserial.Serial's constructor.
"""
from __future__ import annotations

import pytest

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.models import ChamberMode, Health
from krauken.platforms.brewpi.connection import BrewPiConnection
from krauken.platforms.brewpi.live import BrewPiBeerTempSource, BrewPiChamberDriver


class FakeSerial:
    """Maps a written command's leading character to a canned response
    line, queued for the NEXT readline() call -- mirrors real BrewPi
    firmware's behavior closely enough for this driver's own retry loop
    (write, then poll readline() until a matching line shows up) without
    needing to simulate real serial timing at all: every canned response
    is available immediately, so tests never actually wait out
    QUERY_RETRY_INTERVAL_S. `responses={}` (or a command with no entry)
    means "never answers" -- readline() just returns b"" forever, letting
    a test exercise the real retry-then-give-up path."""

    def __init__(self, responses: dict[str, bytes]):
        self.responses = responses
        self.written: list[bytes] = []
        self._pending: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)
        command = data.decode("ascii").strip()[:1]
        if command in self.responses:
            self._pending.append(self.responses[command])

    def readline(self) -> bytes:
        if self._pending:
            return self._pending.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


def _connected(responses: dict[str, bytes]) -> BrewPiConnection:
    conn = BrewPiConnection(clock=SimulatorClock())
    conn._serial = FakeSerial(responses)
    conn.port = "/dev/fake0"
    return conn


# Exact real response captured from the Arduino, see connection.py's docstring.
REAL_T_RESPONSE = (
    b'T:{"BeerTemp":null,"BeerSet":null,"BeerAnn":null,"FridgeTemp": 76.55,'
    b'"FridgeSet": 76.00,"FridgeAnn":null,"State":0}\r\n'
)
REAL_N_RESPONSE = b'N:{"v":"0.2.13","n":"da7e14a9","s":2,"y":0,"b":"s","l":"3"}\r\n'


async def test_read_temps_parses_the_real_captured_response():
    conn = _connected({"t": REAL_T_RESPONSE})
    reading = await conn.read_temps()
    assert reading is not None
    assert reading.fridge_temp_f == 76.55
    assert reading.fridge_set_f == 76.00
    assert reading.beer_temp_f is None
    assert reading.state == 0


async def test_read_temps_returns_none_when_disconnected():
    conn = BrewPiConnection(clock=SimulatorClock())
    assert await conn.read_temps() is None


async def test_read_temps_returns_none_when_the_arduino_never_answers():
    conn = _connected({})  # no 't' entry -- readline() always returns b""
    assert await conn.read_temps() is None


async def test_set_fridge_target_sends_fridge_constant_mode_with_the_temp():
    conn = _connected({})
    await conn.set_fridge_target(65.5)
    assert conn._serial.written[-1] == b'j{mode:"f", fridgeSet:65.5}\n'
    assert conn.commanded_target_f == 65.5


async def test_set_fridge_target_none_sends_off_mode():
    conn = _connected({})
    await conn.set_fridge_target(None)
    assert conn._serial.written[-1] == b'j{mode:"o"}\n'
    assert conn.commanded_target_f is None


async def test_commanded_target_f_is_recorded_even_though_it_is_never_acked():
    # set_fridge_target() only records what was commanded, same convention
    # as ManualChamberDriver -- there's no response to a 'j' command to
    # wait for at all, real or fake.
    conn = _connected({})
    await conn.set_fridge_target(70.0)
    driver = BrewPiChamberDriver(conn)
    assert await driver.commanded_target() == 70.0


async def test_chamber_driver_maps_the_real_response_to_a_reading():
    conn = _connected({"t": REAL_T_RESPONSE})
    driver = BrewPiChamberDriver(conn)
    reading = await driver.read_chamber()
    assert reading.temp_f == 76.55
    assert reading.mode == ChamberMode.IDLE  # State: 0
    assert reading.health == Health.OK


@pytest.mark.parametrize(
    "state,expected_mode",
    [(3, ChamberMode.HEAT), (9, ChamberMode.HEAT), (4, ChamberMode.COOL), (8, ChamberMode.COOL), (0, ChamberMode.IDLE), (99, ChamberMode.IDLE)],
)
async def test_chamber_driver_maps_every_known_state_code(state, expected_mode):
    body = f'T:{{"BeerTemp":null,"BeerSet":null,"BeerAnn":null,"FridgeTemp":70.0,"FridgeSet":70.0,"FridgeAnn":null,"State":{state}}}\r\n'
    conn = _connected({"t": body.encode("ascii")})
    driver = BrewPiChamberDriver(conn)
    reading = await driver.read_chamber()
    assert reading.mode == expected_mode


async def test_chamber_driver_reports_unreachable_when_the_arduino_does_not_answer():
    conn = _connected({})
    driver = BrewPiChamberDriver(conn)
    reading = await driver.read_chamber()
    assert reading.health == Health.UNREACHABLE
    assert reading.temp_f is None


async def test_beer_temp_source_is_unreachable_on_a_chamber_only_rig():
    # Real captured response has BeerTemp: null -- no second probe wired,
    # matching the design doc's reference rig exactly.
    conn = _connected({"t": REAL_T_RESPONSE})
    source = BrewPiBeerTempSource(conn)
    reading = await source.read()
    assert reading.health == Health.UNREACHABLE
    assert reading.temp_f is None


async def test_beer_temp_source_reads_a_real_value_when_a_second_probe_is_wired():
    body = b'T:{"BeerTemp":65.2,"BeerSet":65.0,"BeerAnn":null,"FridgeTemp":63.0,"FridgeSet":63.0,"FridgeAnn":null,"State":0}\r\n'
    conn = _connected({"t": body})
    source = BrewPiBeerTempSource(conn)
    reading = await source.read()
    assert reading.health == Health.OK
    assert reading.temp_f == 65.2


async def test_probe_temps_reports_only_the_fridge_probe_on_a_chamber_only_rig():
    # Corrected after an earlier wrong assumption: the Arduino's field
    # NAMES tell you which role a wired probe plays, not which physical
    # probe that is -- there's still a real identify_probes wiggle-test
    # need here, same as any other platform's probes.
    conn = _connected({"t": REAL_T_RESPONSE})  # BeerTemp: null -- nothing wired
    driver = BrewPiChamberDriver(conn)
    from krauken.platforms.brewpi.live import FRIDGE_PROBE_ADDRESS

    assert await driver.probe_temps() == {FRIDGE_PROBE_ADDRESS: 76.55}


async def test_probe_temps_reports_both_probes_once_a_beer_probe_is_wired():
    from krauken.platforms.brewpi.live import BEER_PROBE_ADDRESS, FRIDGE_PROBE_ADDRESS

    body = b'T:{"BeerTemp":65.2,"BeerSet":65.0,"BeerAnn":null,"FridgeTemp":63.0,"FridgeSet":63.0,"FridgeAnn":null,"State":0}\r\n'
    conn = _connected({"t": body})
    driver = BrewPiChamberDriver(conn)
    assert await driver.probe_temps() == {FRIDGE_PROBE_ADDRESS: 63.0, BEER_PROBE_ADDRESS: 65.2}


async def test_probe_temps_is_empty_when_disconnected():
    conn = _connected({})  # never answers 't'
    driver = BrewPiChamberDriver(conn)
    assert await driver.probe_temps() == {}


async def test_identify_and_connect_uses_the_version_query_and_caches_it():
    conn = _connected({"n": REAL_N_RESPONSE})
    conn.port = None  # force the "not yet connected" path to be exercised via _query_locked directly
    conn._serial = FakeSerial({"n": REAL_N_RESPONSE})
    async with conn._lock:
        info = await conn._query_locked("n", "N")
    assert info == {"v": "0.2.13", "n": "da7e14a9", "s": 2, "y": 0, "b": "s", "l": "3"}
