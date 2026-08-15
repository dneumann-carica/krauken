"""Owns the one persistent raw-HCI BLE listener for Tilt hydrometers --
shared by TiltPlatform.discover() and TiltBeerTempSource/TiltGravitySource,
same "one shared connection object, thin driver wrappers around it" shape
BrewPiConnection and SimPlantEngine/ManualPanel all follow.

Watches for all 8 known Tilt colors unconditionally -- project decision
(explicit user direction, superseding an earlier single-configured-color
draft): discover() should surface whichever color(s) are actually present
as candidates, the same "scan and see what's really there" philosophy
already used for BrewPi's auto-scan, rather than requiring the color be
configured up front. There's no per-color cost to watching all 8 -- it's
the same one raw HCI socket regardless, just an 8-entry dict instead of a
1-entry one.

Deliberately NOT bleak/BlueZ's D-Bus Discovery API -- verified 2026-08-11
against real hardware that it doesn't reliably surface Tilt's advertisement
on this exact Pi/BlueZ combination (two independent bleak/btmon-based scan
attempts, zero Tilt-shaped packets, despite the Tilt visibly broadcasting
and being read successfully at the same time by both BrewPi Remix's own
aioblescan-based Tilt manager and the user's phone). aioblescan's raw HCI
socket caught the same advertisement instantly and repeatedly, decoding to
exactly the UUID/temp/gravity BrewPi's own web UI was showing. This needs
CAP_NET_RAW (not full root) on the daemon process -- see
deploy/krauken-daemon.service.

Passive listen-only, matching the design doc's "no separate bridge
process... 'Tilt dropped out' is detected as no beacon seen within a
timeout" framing exactly: there's no request/response here at all, just a
continuously-running background task updating a per-color cache the driver
classes (live.py) read synchronously -- no I/O on the read path itself,
same idiom the mock platforms' shared-engine classes use.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from krauken.contracts.clock import Clock
from krauken.contracts.errors import PlatformUnavailable
from krauken.platforms.base import requires_optional

log = logging.getLogger("krauken.platforms.tilt")

# Tilt's 8 fixed per-color iBeacon UUIDs -- publicly documented by Tilt's
# manufacturer and used identically by every open-source Tilt integration.
# Verified 2026-08-11: a real Tilt Orange decoded to exactly
# a495bb50-c5b1-4b44-b512-1370f02d74de, matching this table.
TILT_COLOR_BY_UUID: dict[str, str] = {
    "a495bb10c5b14b44b5121370f02d74de": "red",
    "a495bb20c5b14b44b5121370f02d74de": "green",
    "a495bb30c5b14b44b5121370f02d74de": "black",
    "a495bb40c5b14b44b5121370f02d74de": "purple",
    "a495bb50c5b14b44b5121370f02d74de": "orange",
    "a495bb60c5b14b44b5121370f02d74de": "blue",
    "a495bb70c5b14b44b5121370f02d74de": "yellow",
    "a495bb80c5b14b44b5121370f02d74de": "pink",
}
ALL_TILT_COLORS: frozenset[str] = frozenset(TILT_COLOR_BY_UUID.values())

# No beacon within this long and the color reads as dropped out (latest()
# returns None) -- matches the design doc's "no beacon seen within a
# timeout" framing verbatim. Tilt advertises roughly once/second; the real
# gaps observed during testing (at -58 dBm) never exceeded a couple of
# seconds, so this is generous padding, not a tight tolerance tuned to
# barely work.
DROPOUT_TIMEOUT_S = 30.0


@dataclass
class TiltReading:
    temp_f: float
    gravity_sg: float
    rssi: int
    last_seen_monotonic: float


class TiltScanner:
    """One instance per daemon process -- a single raw HCI socket sees
    every BLE advertisement in range; which ones are Tilts (and which
    color) is figured out per-packet, not per-listener. Watches for all
    8 known colors unconditionally (see module docstring)."""

    def __init__(self, clock: Clock, *, hci_device: int = 0):
        self.clock = clock
        self.colors = ALL_TILT_COLORS
        self._hci_device = hci_device
        self._readings: dict[str, TiltReading] = {}
        self._conn: Any = None
        self._btctrl: Any = None
        self._decoder: Any = None
        self._aiobs: Any = None

    @property
    def running(self) -> bool:
        return self._btctrl is not None

    @requires_optional("aioblescan", extra="pi")
    async def start(self) -> None:
        if self._btctrl is not None:
            return
        import aioblescan as aiobs
        from aioblescan.plugins import Tilt as TiltDecoder

        try:
            socket = aiobs.create_bt_socket(self._hci_device)
            loop = asyncio.get_running_loop()
            # Private asyncio API (event_loop._create_connection_transport),
            # not the documented create_connection() -- matches BrewPi
            # Remix's own working aioblescan usage on this exact Python
            # version verbatim (their own comment: "This used to work but
            # now requires a STREAM socket... thanks to martensjacobs for
            # this fix"), rather than a fresh guess at the "right" modern
            # API. Accepted fragility: could break on a future
            # Python/asyncio version, but aioblescan's own example code
            # has the identical exposure, so this driver is no more
            # fragile than its upstream.
            conn, btctrl = await loop._create_connection_transport(socket, aiobs.BLEScanRequester, None, None)
            # process MUST be assigned before send_scan_request(), not
            # after -- matches aioblescan's own __main__.py example
            # ordering exactly. Getting this backwards was a real bug
            # caught testing against the actual Tilt: advertisements
            # arriving in the window between the scan starting and
            # .process being assigned were silently dropped by whatever
            # BLEScanRequester.process defaults to.
            self._aiobs = aiobs
            self._decoder = TiltDecoder()
            btctrl.process = self._on_packet
            await btctrl.send_scan_request()
        except PlatformUnavailable:
            raise
        except Exception as e:
            # Anything from here down is platform/environment trouble, not
            # "aioblescan is missing" (requires_optional already covers
            # that) -- caught broadly and deliberately, not just the
            # exception types seen so far. Real ones hit during
            # development: AttributeError (socket.AF_BLUETOOTH doesn't
            # exist on macOS -- aioblescan is Linux-only despite importing
            # fine anywhere) and, on real Linux hardware, PermissionError
            # without CAP_NET_RAW. This must never propagate as a raw
            # exception: Daemon.start() only catches PlatformUnavailable
            # around this call (matching discovery.py's own per-platform
            # isolation), so anything else here would crash daemon
            # startup entirely over a platform that simply isn't usable
            # right now -- confirmed the hard way in testing, where an
            # uncaught AttributeError here looked indistinguishable from
            # the whole daemon hanging (it wasn't hung: it had crashed
            # inside a fixture, and the test harness's cleanup of an
            # already-broken async fixture was what actually looked stuck).
            raise PlatformUnavailable(f"Tilt BLE scanner unavailable: {e}") from e

        self._conn = conn
        self._btctrl = btctrl
        log.info("Tilt scanner started on hci%d", self._hci_device)

    async def stop(self) -> None:
        if self._btctrl is not None:
            try:
                await self._btctrl.stop_scan_request()
            except Exception:  # noqa: BLE001 -- best-effort shutdown
                pass
        if self._conn is not None:
            self._conn.close()
        self._btctrl = None
        self._conn = None

    def _on_packet(self, data: bytes) -> None:
        try:
            ev = self._aiobs.HCI_Event()
            ev.decode(data)
            raw = self._decoder.decode(ev)
        except Exception:  # noqa: BLE001 -- one malformed packet must never kill the scan loop
            return
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return
        color = TILT_COLOR_BY_UUID.get(parsed.get("uuid", ""))
        if color is None:
            return
        self._readings[color] = TiltReading(
            temp_f=float(parsed["major"]),
            gravity_sg=parsed["minor"] / 1000.0,
            rssi=int(parsed.get("rssi", 0)),
            last_seen_monotonic=self.clock.monotonic(),
        )

    def latest(self, color: str) -> TiltReading | None:
        reading = self._readings.get(color)
        if reading is None:
            return None
        if self.clock.monotonic() - reading.last_seen_monotonic > DROPOUT_TIMEOUT_S:
            return None
        return reading

    def detected_colors(self) -> list[str]:
        """Every color with a live (non-dropped-out) reading right now,
        sorted -- what discover() enumerates into candidates."""
        return sorted(c for c in self.colors if self.latest(c) is not None)
