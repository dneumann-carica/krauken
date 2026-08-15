"""The Tilt PlatformDriver: discover() emits one DeviceCandidate per Tilt
color that currently has a fresh (non-dropped-out) reading -- all 8 known
colors are watched unconditionally (scanner.py), so this surfaces
whichever are actually detected, not a preconfigured set. Mirrors
Simulator's "one candidate per physical device" pattern, not one combined
candidate for the whole scanner.

The scanner itself is meant to run continuously from daemon startup (see
daemon/app.py) so it never misses Tilt's ~1/sec advertisements between
scans -- discover() here just reports whatever it's already caching, and
opportunistically retries starting it if a previous start() attempt failed
(e.g. aioblescan wasn't installed at daemon-startup time but is now), so a
dependency fixed without a daemon restart still gets picked up by the next
Hardware Setup scan.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from krauken.contracts.models import DeviceCandidate, Health
from krauken.contracts.roles import Role
from krauken.platforms.tilt.scanner import TiltScanner

PLATFORM_ID = "tilt"
DISPLAY_NAME = "Tilt"


class TiltPlatform:
    platform_id = PLATFORM_ID
    display_name = DISPLAY_NAME

    def __init__(self, scanner: TiltScanner):
        self._scanner = scanner

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]:
        if not self._scanner.running:
            await self._scanner.start()  # raises PlatformUnavailable if it still can't -- discovery.py handles that per-platform

        candidates = []
        for color in self._scanner.detected_colors():
            reading = self._scanner.latest(color)
            candidates.append(
                DeviceCandidate(
                    device_id=f"tilt:{color}",
                    platform=PLATFORM_ID,
                    display_name=f"Tilt {color.capitalize()}",
                    kind_label="Bluetooth hydrometer",
                    capabilities=frozenset({Role.BEER_TEMP, Role.BEER_GRAVITY}),
                    bundled_roles=frozenset(),
                    health=Health.OK,
                    detail_line=f"RSSI {reading.rssi} dBm",
                    reading_summary=f"{reading.temp_f:.1f}°F - {reading.gravity_sg:.3f}",
                    readings={"beer_temp_f": reading.temp_f, "gravity_sg": reading.gravity_sg},
                    identity={"color": color},
                    simulated=False,
                    available_tests=("live_read",),
                )
            )
        return candidates
