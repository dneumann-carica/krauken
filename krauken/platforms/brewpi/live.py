"""The live BrewPi driver: ChamberDriver (always) and BeerTempSource
(eligible whenever a second probe happens to be wired -- see
BrewPiReading.beer_temp_f, which is simply None when it isn't).

No GravitySource here -- BrewPi has no hydrometer of its own; gravity
comes from Tilt (platforms/tilt/) if anything. No relay-timing/compressor-
protection logic here either, deliberately: the design doc's "Integrated
(BrewPi)" family means the Arduino owns its own thermostat and compressor
protection already (fridge-constant mode's min-on/min-off/anti-short-cycle
timers all live in the firmware) -- duplicating any of that on this side
would be exactly the "protection stacked at two tiers that can disagree"
failure mode the design doc calls out BrewPi as the reason to avoid.
set_target() therefore does nothing but forward a plain setpoint (or
None) to the Arduino; read_chamber() does nothing but relay back
whatever the Arduino itself reports.

Corrected after an earlier wrong assumption in this same session: the
Arduino's "FridgeTemp"/"BeerTemp" JSON field NAMES tell you what role
EACH ALREADY-WIRED probe plays, but they don't tell the PERSON wiring up
a new rig which physical probe is which -- exactly the same "which wire
is actually which" problem OneWire probes have, just with two fixed slots
instead of an arbitrary bus. probe_temps() below and platform.py's
DeviceCandidate now both treat BrewPi as needing the same identify_probes
wiggle-test flow as everything else."""
from __future__ import annotations

from krauken.contracts.models import BeerReading, ChamberMode, ChamberReading, Health
from krauken.platforms.brewpi.connection import STATE_IDLE, STATES_COOLING, STATES_HEATING, BrewPiConnection

# Fixed, not derived from anything the Arduino sends -- there are always
# exactly these two conceptual slots (unlike a OneWire bus's arbitrary ROM
# addresses), so a fixed pair of labels is enough for probe_temps()'s
# identify_probes consumer to tell them apart. Defined here (not
# connection.py), mirroring where Simulator/Manual define their own
# PROBE_1_ADDRESS/PROBE_2_ADDRESS constants -- in the live-driver module,
# not the engine/connection module underneath it.
FRIDGE_PROBE_ADDRESS = "brewpi-fridge"
BEER_PROBE_ADDRESS = "brewpi-beer"


def _mode_for_state(state: int | None) -> ChamberMode:
    if state in STATES_HEATING:
        return ChamberMode.HEAT
    if state in STATES_COOLING:
        return ChamberMode.COOL
    # STATE_IDLE and every "waiting to ..."/door-open/etc. code not in the
    # two sets above all read as IDLE for display purposes -- the Arduino
    # itself is the one enforcing why it isn't actively driving a relay
    # right now (min-off timer, door open, ...), which is exactly the
    # "protection lives once, at the tier that owns it" split; this driver
    # only ever needs to know actively-heating/actively-cooling/neither.
    return ChamberMode.IDLE


class BrewPiChamberDriver:
    def __init__(self, connection: BrewPiConnection, device_id: str | None = None):
        self._connection = connection  # device_id unused -- BrewPi has exactly one chamber device

    async def read_chamber(self) -> ChamberReading:
        reading = await self._connection.read_temps()
        if reading is None:
            return ChamberReading(
                temp_f=None,
                mode=ChamberMode.IDLE,
                health=Health.UNREACHABLE,
                last_good_ts=None,
                commanded_target_f=self._connection.commanded_target_f,
            )
        return ChamberReading(
            temp_f=reading.fridge_temp_f,
            mode=_mode_for_state(reading.state),
            health=Health.OK,
            last_good_ts=self._connection.clock.now(),
            commanded_target_f=self._connection.commanded_target_f,
            detail=f"BrewPi {self._connection.version_info.get('v')} on {self._connection.port}"
            if self._connection.version_info
            else "",
        )

    async def set_target(self, temp_f: float | None) -> None:
        await self._connection.set_fridge_target(temp_f)

    async def commanded_target(self) -> float | None:
        return self._connection.commanded_target_f

    async def set_ambient_location(self, location: str | None) -> None:
        pass  # Real hardware has no simulated-ambient concept -- see the Protocol's own docstring.

    async def probe_temps(self) -> dict[str, float | None]:
        # The Arduino's field NAMES ("FridgeTemp"/"BeerTemp") tell you
        # what role each wired probe plays, but not which physical probe
        # that is -- the wizard's identify_probes wiggle-test is exactly
        # how a person confirms that, same as a OneWire bus's arbitrary
        # ROM addresses. Beer slot only appears once actually wired (a
        # chamber-only rig has nothing there to identify), matching
        # platform.py's DeviceCandidate.identity["probe_addresses"].
        reading = await self._connection.read_temps()
        if reading is None:
            return {}
        temps: dict[str, float | None] = {FRIDGE_PROBE_ADDRESS: reading.fridge_temp_f}
        if reading.beer_temp_f is not None:
            temps[BEER_PROBE_ADDRESS] = reading.beer_temp_f
        return temps


class BrewPiBeerTempSource:
    """Only meaningful when a second probe is actually wired (design doc:
    "not the case on the reference rig, which has chamber-only") -- reads
    the same BrewPiConnection every ChamberDriver call already updates, so
    there's no separate polling here, just a different field of the same
    reading. Reads as UNREACHABLE (not "0 degrees" or some other silent
    default) whenever BeerTemp comes back null, which is exactly what a
    chamber-only rig reports on every single poll -- this is the driver
    role_mapping.md's rules mean never gets assigned in that case, since
    an unqualified device (no BEER_TEMP capability advertised -- see
    platform.py's DeviceCandidate) can't be picked for the role at all."""

    def __init__(self, connection: BrewPiConnection, device_id: str | None = None):
        self._connection = connection  # device_id unused -- BrewPi has exactly one beer-temp device

    async def read(self) -> BeerReading:
        reading = await self._connection.read_temps()
        if reading is None or reading.beer_temp_f is None:
            return BeerReading(temp_f=None, health=Health.UNREACHABLE, last_good_ts=None)
        return BeerReading(temp_f=reading.beer_temp_f, health=Health.OK, last_good_ts=self._connection.clock.now())
