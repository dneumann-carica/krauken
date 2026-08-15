"""Tests TiltScanner's packet-decode logic against the EXACT raw HCI
advertisement bytes captured from a real Tilt Orange on 2026-08-11 (via
`sudo python3 -m aioblescan --tilt --raw` against the real BrewPi Pi's
Bluetooth adapter -- see scanner.py's module docstring for the full
provenance and why aioblescan/raw-HCI was needed over bleak). This is a
golden-fixture test: if aioblescan's own wire format or this decode logic
ever drifts, this is the test that would catch it, because the bytes
aren't synthesized -- they're a verbatim real capture that decoded to
{"uuid": "a495bb50c5b14b44b5121370f02d74de", "major": 39, "minor": 1008}
in that same session, matching BrewPi's own web UI (Tilt SG: 1.008, Tilt
Temp: 39F) exactly.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aioblescan")

from krauken.contracts.clock import SimulatorClock  # noqa: E402
from krauken.platforms.tilt.scanner import ALL_TILT_COLORS, DROPOUT_TIMEOUT_S, TiltScanner  # noqa: E402

# Verbatim raw HCI event bytes captured 2026-08-11 -- a real Tilt Orange
# advertisement, decoded live to major=39 (temp F), minor=1008 (SG x1000).
REAL_TILT_ORANGE_PACKET = (
    b"\x04>*\x02\x01\x03\x01\x82\xfc\xa4\xd2\xf0\xc2\x1e\x02\x01\x04\x1a\xffL\x00\x02\x15"
    b"\xa4\x95\xbbP\xc5\xb1KD\xb5\x12\x13p\xf0-t\xde\x00'\x03\xf0\xc5\xc6"
)

# A structurally-identical packet but NOT a Tilt at all -- one of the
# other real advertisements captured in the same session (an Apple
# continuity/proximity-pairing message, company ID 0x004C like every
# iBeacon but a totally different subtype), used to confirm the decoder
# doesn't false-positive on "any Apple manufacturer data".
REAL_NON_TILT_PACKET = (
    b"\x04>\x1e\x02\x01\x00\x01\r\x03\x9a\x1a)S\x12\x02\x01\x1a\x02\n\x0c\x0b\xffL\x00\x10\x06K\x1dz}\xbeh\xb0"
)


def _scanner(clock=None) -> TiltScanner:
    s = TiltScanner(clock or SimulatorClock(), hci_device=0)
    # _on_packet needs the aioblescan module + a live decoder instance,
    # normally set up inside start() -- assembled directly here so this
    # test never has to open a real (or fake) raw HCI socket at all.
    import aioblescan as aiobs
    from aioblescan.plugins import Tilt as TiltDecoder

    s._aiobs = aiobs
    s._decoder = TiltDecoder()
    return s


def test_watches_for_all_eight_known_colors_by_default():
    s = _scanner()
    assert s.colors == ALL_TILT_COLORS
    assert len(s.colors) == 8


def test_decodes_a_real_captured_tilt_orange_advertisement():
    s = _scanner()
    s._on_packet(REAL_TILT_ORANGE_PACKET)
    reading = s.latest("orange")
    assert reading is not None
    assert reading.temp_f == 39.0
    assert reading.gravity_sg == 1.008


def test_a_color_never_seen_reads_as_not_present():
    s = _scanner()
    assert s.latest("orange") is None
    assert s.detected_colors() == []


def test_ignores_a_real_non_tilt_apple_advertisement():
    s = _scanner()
    s._on_packet(REAL_NON_TILT_PACKET)
    assert s.detected_colors() == []


def test_malformed_packet_does_not_raise():
    s = _scanner()
    s._on_packet(b"\x00\x01garbage")  # must not raise -- one bad packet can't kill the scan loop
    assert s.detected_colors() == []


def test_detected_colors_reflects_whatever_was_actually_seen():
    s = _scanner()
    s._on_packet(REAL_TILT_ORANGE_PACKET)
    assert s.detected_colors() == ["orange"]


def test_a_reading_drops_out_after_the_timeout_with_no_new_beacon():
    clock = SimulatorClock()
    s = _scanner(clock)
    s._on_packet(REAL_TILT_ORANGE_PACKET)
    assert s.latest("orange") is not None

    clock.advance(DROPOUT_TIMEOUT_S + 1)
    assert s.latest("orange") is None
    assert s.detected_colors() == []


def test_a_fresh_beacon_before_the_timeout_keeps_it_alive():
    clock = SimulatorClock()
    s = _scanner(clock)
    s._on_packet(REAL_TILT_ORANGE_PACKET)

    clock.advance(DROPOUT_TIMEOUT_S - 1)
    assert s.latest("orange") is not None  # still within the window

    s._on_packet(REAL_TILT_ORANGE_PACKET)  # a fresh beacon resets the clock
    clock.advance(DROPOUT_TIMEOUT_S - 1)
    assert s.latest("orange") is not None  # would have expired from the FIRST beacon alone
