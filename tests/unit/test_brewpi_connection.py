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

# The QUERY_TIMEOUT_S-shrinking autouse fixture lives in tests/unit/conftest.py
# now, not here -- it needs to apply to every test file that can exercise a
# "never answers" BrewPiConnection path, not just this one (confirmed live:
# test_brewpi_identify.py has its own such case).


class FakeSerial:
    """Maps a written command's leading character to a canned response,
    queued for the NEXT readline() call(s) -- mirrors real BrewPi
    firmware's behavior closely enough for this driver's own query loop
    (write once, then poll readline() until a matching line shows up)
    without needing to simulate real serial timing at all: every canned
    response is available immediately, so tests never actually wait out
    QUERY_TIMEOUT_S (further sped up by this file's `_fast_query_timeout`
    autouse fixture, which shrinks it well below its real 15s value).
    `responses={}` (or a command with no entry) means "never answers" --
    readline() just returns b"" forever, letting a test exercise the
    real wait-then-give-up path. A response value may
    be a single `bytes` line (existing convention) or a `list[bytes]` of
    several lines queued in order -- the latter is what lets a test
    reproduce the real firmware's confirmed behavior of splitting a 'd'/'h'
    device-list response across multiple physical lines when a log
    message gets interleaved mid-array."""

    def __init__(self, responses: dict[str, bytes | list[bytes]]):
        self.responses = responses
        self.written: list[bytes] = []
        self._pending: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)
        command = data.decode("ascii").strip()[:1]
        if command in self.responses:
            value = self.responses[command]
            if isinstance(value, list):
                self._pending.extend(value)
            else:
                self._pending.append(value)

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


# --- Device-configuration commands (d/h/U/R) -- confirmed 2026-08-15 both
# via reading brewpi-remix/brewpi-firmware-rmx's real firmware source and
# against the real rig; see plans/jiggly-bubbling-popcorn.md for the full
# session. ---

REAL_D_RESPONSE = (
    b'd:[{"i":1,"t":1,"c":1,"b":0,"f":5,"h":2,"d":0,"p":18,"v": 76.549,'
    b'"a":"28FFCDAB94160574","j": 0.000},{"i":3,"t":3,"c":1,"b":0,"f":3,'
    b'"h":1,"d":0,"p":5,"v":0,"x":1}]\r\n'
)
REAL_H_RESPONSE = (
    b'h:[{"i":-1,"t":0,"c":1,"b":0,"f":0,"h":1,"d":0,"p":2,"x":1},'
    b'{"i":-1,"t":0,"c":1,"b":0,"f":0,"h":1,"d":0,"p":6,"x":1},'
    b'{"i":-1,"t":0,"c":1,"b":0,"f":0,"h":1,"d":0,"p":19,"x":1}]\r\n'
)
# The confirmed-real interleaved-log-message shape: a firmware warning
# splits one logical JSON array across two physical lines.
REAL_H_RESPONSE_WITH_INTERLEAVED_LOG = [
    b'h:[D:{"logType":"W","logID":2,"V":[18,"28FFBA8594160473"]}\r\n',
    b'{"i":-1,"t":0,"c":1,"b":0,"f":0,"h":2,"d":0,"p":18,"v":null,'
    b'"a":"28FFBA8594160473","j": 0.000},{"i":-1,"t":0,"c":1,"b":0,"f":0,'
    b'"h":1,"d":0,"p":2,"x":1}]\r\n',
]


async def test_list_installed_devices_parses_the_real_captured_response():
    conn = _connected({"d": REAL_D_RESPONSE})
    devices = await conn.list_installed_devices()
    assert [d.slot for d in devices] == [1, 3]
    assert devices[0].address == "28FFCDAB94160574"
    assert devices[0].function == 5  # DEVICE_FUNCTION_CHAMBER_TEMP
    assert devices[0].value == 76.549
    assert devices[1].pin == 5
    assert devices[1].invert == 1


async def test_list_available_devices_parses_the_real_captured_response():
    conn = _connected({"h": REAL_H_RESPONSE})
    devices = await conn.list_available_devices()
    assert [d.slot for d in devices] == [-1, -1, -1]
    assert [d.pin for d in devices] == [2, 6, 19]


async def test_list_available_devices_survives_an_interleaved_log_message():
    # This is the real, confirmed firmware behavior this session found --
    # not a hypothetical: a log message split a device-list response
    # across two physical lines. Without _query_device_list_locked's
    # log-stripping/accumulation, this would fail to parse entirely.
    conn = _connected({"h": REAL_H_RESPONSE_WITH_INTERLEAVED_LOG})
    devices = await conn.list_available_devices()
    assert len(devices) == 2
    assert devices[0].address == "28FFBA8594160473"
    assert devices[0].value is None  # the confirmed-flaky probe reads null
    assert devices[1].pin == 2


async def test_list_all_devices_merges_installed_and_available():
    conn = _connected({"d": REAL_D_RESPONSE, "h": REAL_H_RESPONSE})
    devices = await conn.list_all_devices()
    assert len(devices) == 5
    assert [d.slot for d in devices] == [1, 3, -1, -1, -1]


async def test_list_installed_devices_returns_empty_when_disconnected():
    conn = BrewPiConnection(clock=SimulatorClock())
    assert await conn.list_installed_devices() == []


async def test_install_device_sends_the_confirmed_write_shape():
    from krauken.platforms.brewpi.device_config import BrewPiDevice

    conn = _connected({})
    device = BrewPiDevice(slot=0, chamber=1, beer=0, function=1, hardware=1, pin=2, invert=0)
    await conn.install_device(device)
    written = conn._serial.written[-1].decode("ascii")
    assert written.startswith("U")
    import json

    assert json.loads(written[1:].strip()) == {"i": 0, "c": 1, "b": 0, "f": 1, "h": 1, "d": 0, "p": 2, "x": 0}


async def test_install_device_omits_t_and_v_fields():
    # Confirmed via DeviceManager.cpp's DeviceDefinition struct: "t" isn't
    # part of it at all (read-side only), and "v" is live telemetry, never
    # echoed back on a write.
    from krauken.platforms.brewpi.device_config import BrewPiDevice

    conn = _connected({})
    device = BrewPiDevice(slot=0, category=3, function=1, hardware=1, pin=2, value=42.0)
    await conn.install_device(device)
    import json

    written = conn._serial.written[-1].decode("ascii")
    body = json.loads(written[1:].strip())
    assert "t" not in body
    assert "v" not in body


async def test_reset_and_reconnect_sends_r_then_reidentifies():
    conn = _connected({"n": REAL_N_RESPONSE})
    result = await conn.reset_and_reconnect()
    assert conn._serial.written[0] == b"R\n"
    assert result is True
    assert conn.version_info == {"v": "0.2.13", "n": "da7e14a9", "s": 2, "y": 0, "b": "s", "l": "3"}


async def test_reset_and_reconnect_is_a_safe_no_op_when_disconnected():
    conn = BrewPiConnection(clock=SimulatorClock())
    result = await conn.reset_and_reconnect()
    assert result is False


# --- Gap 5: no resend on retry -- confirmed live 2026-08-18 that the old
# shape (resend the command on every retry iteration) let two outstanding
# requests for the same thing race, producing interleaved/stale responses.
# The fix: write exactly once, then just keep reading until the deadline. ---


async def test_query_locked_never_resends_while_waiting_for_a_late_response():
    # A non-matching line (an async log fragment) arrives first, then the
    # real match -- the fix must keep reading rather than giving up and
    # resending after some fixed sub-interval.
    conn = _connected(
        {"t": [b'D:{"logType":"I","logID":12,"V":["mode","f"]}\r\n', REAL_T_RESPONSE]},
    )
    async with conn._lock:
        data = await conn._query_locked("t", "T")
    assert data is not None
    assert data["FridgeTemp"] == 76.55
    assert len(conn._serial.written) == 1  # exactly one write -- no resend


async def test_query_locked_writes_exactly_once_when_the_arduino_never_answers():
    conn = _connected({})  # no 't' entry -- readline() always returns b""
    async with conn._lock:
        data = await conn._query_locked("t", "T")
    assert data is None
    assert len(conn._serial.written) == 1  # gave up after ONE write, not several


async def test_query_device_list_locked_never_resends_while_waiting_for_a_late_response():
    conn = _connected({"h": REAL_H_RESPONSE_WITH_INTERLEAVED_LOG})
    devices = await conn.list_available_devices()
    assert len(devices) == 2
    assert len(conn._serial.written) == 1  # exactly one write -- no resend


async def test_query_device_list_locked_writes_exactly_once_when_the_arduino_never_answers():
    conn = _connected({})  # no 'h' entry -- readline() always returns b""
    devices = await conn.list_available_devices()
    assert devices == []
    assert len(conn._serial.written) == 1  # gave up after ONE write, not several
