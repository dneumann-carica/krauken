from __future__ import annotations

from krauken.contracts.clock import SimulatorClock
from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.brewpi.connection import BrewPiConnection
from krauken.platforms.brewpi.platform import DEVICE_ID, BrewPiPlatform
from tests.unit.test_brewpi_connection import REAL_N_RESPONSE, REAL_T_RESPONSE, FakeSerial


def _already_identified_connection() -> BrewPiConnection:
    """Stands in for "identify_and_connect() already succeeded" -- exactly
    what a real daemon has after the background connect task at startup
    (or a previous scan) worked, which is the state discover() actually
    runs against on every subsequent call (it re-tries the CURRENT port
    first)."""
    conn = BrewPiConnection(clock=SimulatorClock())
    conn._serial = FakeSerial({"n": REAL_N_RESPONSE, "t": REAL_T_RESPONSE})
    conn.port = "/dev/ttyACM0"
    conn.version_info = {"v": "0.2.13", "n": "da7e14a9", "s": 2, "y": 0, "b": "s", "l": "3"}
    return conn


async def test_discover_reports_the_chamber_bundle_candidate():
    platform = BrewPiPlatform(_already_identified_connection())
    candidates = await platform.discover({})
    assert len(candidates) == 1
    c = candidates[0]
    assert c.device_id == DEVICE_ID
    assert c.platform == "brewpi"
    assert c.bundled_roles == CHAMBER_BUNDLE
    assert CHAMBER_BUNDLE <= c.capabilities
    assert c.readings["chamber_temp_f"] == 76.55


async def test_discover_does_not_add_beer_temp_capability_on_a_chamber_only_rig():
    # REAL_T_RESPONSE has BeerTemp: null -- no second probe wired.
    platform = BrewPiPlatform(_already_identified_connection())
    candidates = await platform.discover({})
    assert Role.BEER_TEMP not in candidates[0].capabilities


async def test_discover_adds_beer_temp_capability_when_a_second_probe_is_wired():
    conn = BrewPiConnection(clock=SimulatorClock())
    body = b'T:{"BeerTemp":65.2,"BeerSet":65.0,"BeerAnn":null,"FridgeTemp":63.0,"FridgeSet":63.0,"FridgeAnn":null,"State":0}\r\n'
    conn._serial = FakeSerial({"n": REAL_N_RESPONSE, "t": body})
    conn.port = "/dev/ttyACM0"
    conn.version_info = {"v": "0.2.13"}
    candidates = await BrewPiPlatform(conn).discover({})
    assert Role.BEER_TEMP in candidates[0].capabilities


async def test_discover_offers_the_device_config_wizard_actions_with_just_the_fridge_probe_on_a_chamber_only_rig():
    # identify_probes/confirm_heater are superseded as SETUP mechanisms by
    # the device-configuration wizard (platforms/brewpi/device_config.py) --
    # both assumed a probe/pin role mapping already existed via BrewPi's
    # own classic web UI, which this session confirmed is often untrue.
    from krauken.platforms.brewpi.live import FRIDGE_PROBE_ADDRESS

    platform = BrewPiPlatform(_already_identified_connection())
    candidates = await platform.discover({})
    c = candidates[0]
    assert "identify_probes" not in c.available_tests
    assert "confirm_heater" not in c.available_tests
    assert "fire_outlet" not in c.available_tests
    for action in (
        "begin_device_config",
        "brewpi_devices",
        "identify_onewire_probes",
        "install_probe",
        "identify_relay_pin",
        "finalize_device_config",
        "reset_brewpi",
    ):
        assert action in c.available_tests
    assert c.identity["probe_addresses"] == [FRIDGE_PROBE_ADDRESS]


async def test_discover_offers_both_probe_addresses_once_a_beer_probe_is_wired():
    from krauken.platforms.brewpi.live import BEER_PROBE_ADDRESS, FRIDGE_PROBE_ADDRESS

    conn = BrewPiConnection(clock=SimulatorClock())
    body = b'T:{"BeerTemp":65.2,"BeerSet":65.0,"BeerAnn":null,"FridgeTemp":63.0,"FridgeSet":63.0,"FridgeAnn":null,"State":0}\r\n'
    conn._serial = FakeSerial({"n": REAL_N_RESPONSE, "t": body})
    conn.port = "/dev/ttyACM0"
    conn.version_info = {"v": "0.2.13"}
    candidates = await BrewPiPlatform(conn).discover({})
    assert candidates[0].identity["probe_addresses"] == [FRIDGE_PROBE_ADDRESS, BEER_PROBE_ADDRESS]


async def test_discover_returns_nothing_when_no_brewpi_is_found(monkeypatch):
    # Forced empty rather than relying on this test machine happening to
    # have no real /dev/ttyACM*/ttyUSB* devices -- that's true today on a
    # dev Mac but isn't something this test should assume about whatever
    # machine eventually runs it (a real Pi test runner might have one for
    # unrelated reasons).
    monkeypatch.setattr("krauken.platforms.brewpi.connection._candidate_ports", lambda: [])
    platform = BrewPiPlatform(BrewPiConnection(clock=SimulatorClock()))
    candidates = await platform.discover({})
    assert candidates == []
