"""identify_and_connect()'s port-scanning logic specifically -- separated
from test_brewpi_connection.py because this needs real `serial` importable
(requires_optional's check runs before anything else), even though the
actual port open is monkeypatched rather than touching real hardware.
Skips cleanly on a machine without the `pi` extra installed instead of
failing -- matching the whole point of platforms/base.py's
@requires_optional existing at all.
"""
from __future__ import annotations

import pytest

pytest.importorskip("serial")

from krauken.contracts.clock import SimulatorClock  # noqa: E402
from krauken.platforms.brewpi.connection import BrewPiConnection  # noqa: E402
from tests.unit.test_brewpi_connection import REAL_N_RESPONSE, FakeSerial  # noqa: E402


class _FakeSerialPort(FakeSerial):
    """Adds the constructor shape pyserial.Serial(port, baud, timeout=)
    has, so it can stand in for the real class via monkeypatch."""

    def __init__(self, port, baudrate, timeout=1.0, responses=None):
        super().__init__(responses or {})
        self.port_path = port


def test_identifies_a_real_brewpi_on_the_second_candidate_port(monkeypatch):
    import serial

    opened_ports = []

    def fake_serial_factory(port, baudrate, timeout=1.0):
        opened_ports.append(port)
        if port == "/dev/ttyUSB0":
            raise serial.SerialException("nothing there")
        return _FakeSerialPort(port, baudrate, timeout, responses={"n": REAL_N_RESPONSE})

    monkeypatch.setattr(serial, "Serial", fake_serial_factory)
    monkeypatch.setattr("krauken.platforms.brewpi.connection.BOOT_DELAY_S", 0.0)

    conn = BrewPiConnection(clock=SimulatorClock())

    import asyncio

    found = asyncio.run(
        conn.identify_and_connect(candidate_ports=["/dev/ttyUSB0", "/dev/ttyACM0"])
    )

    assert found is True
    assert conn.port == "/dev/ttyACM0"
    assert conn.version_info == {"v": "0.2.13", "n": "da7e14a9", "s": 2, "y": 0, "b": "s", "l": "3"}
    assert opened_ports == ["/dev/ttyUSB0", "/dev/ttyACM0"]


def test_returns_false_when_nothing_on_any_candidate_port_answers(monkeypatch):
    import serial

    def fake_serial_factory(port, baudrate, timeout=1.0):
        return _FakeSerialPort(port, baudrate, timeout, responses={})  # never answers 'n'

    monkeypatch.setattr(serial, "Serial", fake_serial_factory)
    monkeypatch.setattr("krauken.platforms.brewpi.connection.BOOT_DELAY_S", 0.0)

    conn = BrewPiConnection(clock=SimulatorClock())

    import asyncio

    found = asyncio.run(conn.identify_and_connect(candidate_ports=["/dev/ttyACM0"]))

    assert found is False
    assert conn.port is None
