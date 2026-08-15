"""The live Tilt driver: GravitySource and, independently, an eligible
BeerTempSource (design doc: Tilt fills either or both roles, never
bundled with anything). Both classes just read TiltScanner's cache --
see that module's own docstring for why there's no I/O here at all.

Now genuinely multi-Tilt-aware: device_id (e.g. "tilt:orange") is threaded
through from daemon/drivers.py, which gets it straight from
hardware_config's own device_id column -- the same thing discover()
advertised as this candidate's identity when it was mapped. discover()
(platform.py) already surfaced every detected color as its own candidate
for the Hardware Setup UI; this closes the other half -- the live read
path now honors whichever one was actually picked, instead of guessing.

device_id=None is still accepted and falls back to "whichever detected
color sorts first" -- not a real operating mode (every production call
site always has a real device_id once a role is actually mapped), just a
graceful default for any caller that doesn't have one (bare
`TiltBeerTempSource(scanner)` construction, as several tests still do)."""
from __future__ import annotations

from krauken.contracts.models import BeerReading, GravityReading, Health
from krauken.platforms.tilt.scanner import TiltScanner


def _color_for(scanner: TiltScanner, device_id: str | None) -> str | None:
    if device_id is not None and device_id.startswith("tilt:"):
        return device_id.split(":", 1)[1]
    detected = scanner.detected_colors()
    return detected[0] if detected else None


class TiltBeerTempSource:
    def __init__(self, scanner: TiltScanner, device_id: str | None = None):
        self._scanner = scanner
        self._device_id = device_id

    async def read(self) -> BeerReading:
        color = _color_for(self._scanner, self._device_id)
        reading = self._scanner.latest(color) if color else None
        if reading is None:
            return BeerReading(temp_f=None, health=Health.UNREACHABLE, last_good_ts=None)
        return BeerReading(temp_f=reading.temp_f, health=Health.OK, last_good_ts=self._scanner.clock.now())


class TiltGravitySource:
    def __init__(self, scanner: TiltScanner, device_id: str | None = None):
        self._scanner = scanner
        self._device_id = device_id

    async def read(self) -> GravityReading:
        color = _color_for(self._scanner, self._device_id)
        reading = self._scanner.latest(color) if color else None
        if reading is None:
            return GravityReading(gravity_sg=None, health=Health.UNREACHABLE, last_good_ts=None)
        return GravityReading(
            gravity_sg=reading.gravity_sg, health=Health.OK, last_good_ts=self._scanner.clock.now()
        )
