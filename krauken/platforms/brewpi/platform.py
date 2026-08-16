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
                # No fire_outlet here -- BrewPi's firmware decides which
                # relay does what internally, so there's no independent
                # relay to fire the way Manual/Simulator's outlets can be.
                # identify_probes/confirm_heater are superseded as SETUP
                # mechanisms by the device-configuration wizard below (both
                # assumed probe/pin roles were already assigned via
                # BrewPi's own classic web UI, which is often untrue --
                # confirmed this session against the actual reference rig,
                # which has a cooling actuator installed and NO heat
                # device installed anywhere). confirm_heater itself is kept
                # as a standalone post-setup diagnostic (HardwareSetupView's
                # "Test heater" button), not exposed through the guided
                # wizard's available_tests here.
                #
                # The actions below (platforms/brewpi/device_config.py,
                # dispatched via PLATFORM_BINDINGS["brewpi"].test_runners --
                # see daemon/tests_runtime.py's start_test()) are what let
                # Krauken discover and install probe/relay mappings itself,
                # so a user never needs BrewPi's own Device Configuration
                # page: begin_device_config (self-heals/snapshots/wipes at
                # wizard start), brewpi_devices (full device list with live
                # values), identify_onewire_probes (chamber/beer probe ID
                # from raw OneWire addresses), install_probe (installs an
                # identified probe immediately, not deferred to finalize),
                # identify_relay_pin (unified relay pin-identification
                # sweep -- always forces a heat demand and lets the human
                # observer say what physically turned on; replaced the
                # old two-pass cool/heat sweep_relay, which left multiple
                # stray same-function actuators installed and driven
                # together -- confirmed live this session BrewPi does not
                # enforce one-actuator-per-function), finalize_device_config
                # (push final config + reset).
                available_tests=(
                    "live_read",
                    "begin_device_config",
                    "brewpi_devices",
                    "identify_onewire_probes",
                    "install_probe",
                    "identify_relay_pin",
                    "finalize_device_config",
                    "reset_brewpi",
                ),
            ),
        ]
