"""The BrewPi PlatformDriver: discover() re-runs the auto-scan every time
(not just once, ever) so unplugging/replugging the Arduino, or it
enumerating under a different /dev/ttyACM* path after a reboot, is picked
up by the next Hardware Setup scan rather than requiring a daemon
restart. Reuses the SAME BrewPiConnection the live ChamberDriver reads
from (see connection.py's own docstring) -- a successful scan leaves the
connection open and ready, so the very next control tick's read_chamber()
doesn't pay the ~4s boot-delay cost again.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from krauken.contracts.models import DeviceCandidate, Health
from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.brewpi.connection import BrewPiConnection
from krauken.platforms.brewpi.live import BEER_PROBE_ADDRESS, FRIDGE_PROBE_ADDRESS

PLATFORM_ID = "brewpi"
DISPLAY_NAME = "BrewPi"

# Fixed, not derived from the serial port path -- a port can enumerate
# under a different /dev/ttyACM* name after a reboot (project decision:
# accepted, since auto-scan re-resolves it every scan regardless), and
# there's only ever one BrewPi per install (single-chamber scope, per the
# design doc's own non-goals), so there's nothing a port-derived id would
# need to disambiguate. The actual port lives in `identity` below, for
# display/debugging only -- never persisted as identity.
DEVICE_ID = "brewpi:controller"


class BrewPiPlatform:
    platform_id = PLATFORM_ID
    display_name = DISPLAY_NAME

    def __init__(self, connection: BrewPiConnection):
        self._connection = connection

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]:
        found = await self._connection.identify_and_connect()
        if not found:
            # Not PlatformUnavailable -- pyserial IS installed and usable
            # (identify_and_connect's own @requires_optional already
            # covers the "not installed" case with that exception); this
            # is just "scanned, found nothing", the same as any mock
            # platform's discover() returning an empty list when nothing
            # matches.
            return []

        reading = await self._connection.read_temps()
        chamber_f = reading.fridge_temp_f if reading else None
        beer_f = reading.beer_temp_f if reading else None
        capabilities = set(CHAMBER_BUNDLE)
        if beer_f is not None:
            capabilities.add(Role.BEER_TEMP)

        version = self._connection.version_info or {}
        # Beer slot only advertised once actually wired -- a chamber-only
        # rig has nothing there for identify_probes to identify, matching
        # live.py's probe_temps() (which likewise omits BEER_PROBE_ADDRESS
        # entirely rather than reporting it as a probe reading None).
        probe_addresses = [FRIDGE_PROBE_ADDRESS] + ([BEER_PROBE_ADDRESS] if beer_f is not None else [])
        return [
            DeviceCandidate(
                device_id=DEVICE_ID,
                platform=PLATFORM_ID,
                display_name="BrewPi controller",
                kind_label="Chamber controller - BrewPi (Arduino)",
                capabilities=frozenset(capabilities),
                bundled_roles=CHAMBER_BUNDLE,
                health=Health.OK,
                detail_line=f"Firmware {version.get('v', '?')} on {self._connection.port}",
                reading_summary=f"{chamber_f:.1f}°F chamber" if chamber_f is not None else None,
                readings={"chamber_temp_f": chamber_f, "beer_temp_f": beer_f},
                identity={"serial_port": self._connection.port, "firmware": version, "probe_addresses": probe_addresses},
                simulated=False,
                available_tests=("live_read", "identify_probes"),
            ),
        ]
