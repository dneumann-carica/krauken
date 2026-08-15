"""The live Manual driver: implements ChamberDriver/BeerTempSource/
GravitySource with purely operator-settable state -- no physics, no
coupling between chamber and beer temp. Where the Simulator driver
(platforms/simulator/live.py) exists to give the control loop a *plausible*
autonomous batch to run against, this one exists to let a developer hand-
pick an exact reading (e.g. "beer temp reads 75F") and watch the control
loop react to that specific value -- the dev-panel API mentioned in
platforms/manual/platform.py's docstring ("Milestone 2+").

set_target() only records what was commanded, for display -- it never
moves temp_f/mode itself, since nothing here is simulating a fridge.

A hand-set `health != OK` is the one deliberate exception to "purely
operator-settable, no behavior": it makes last_good_ts report None (no
known-good reading at all) instead of "just now". That's what makes the
dev-panel's health field actually useful for exercising the control loop's
beer-temp-lost/gravity-lost failsafe paths (contracts/failsafe.py) without
needing real hardware to genuinely go unresponsive.

Two more operator-settable concepts, both purely for dev-panel/wizard
testing rather than the control loop itself: a chamber controller's outlet
on/off state and optional second probe (mirroring the real hardware's
1-cooling + optional-heating + chamber-probe + optional-beer-probe shape),
and the Tilt's own availability toggle (simulating it being out of BLE
range or powered off -- see platform.py's discover(), which omits it
entirely from the scan while unavailable).
"""
from __future__ import annotations

from dataclasses import dataclass

from krauken.contracts.clock import Clock
from krauken.contracts.models import BeerReading, ChamberMode, ChamberReading, GravityReading, Health

_MODE_MAP = {"idle": ChamberMode.IDLE, "cool": ChamberMode.COOL, "heat": ChamberMode.HEAT}

# The probe-address strings this platform's discover() advertises in
# DeviceCandidate.identity["probe_addresses"] (platform.py) -- defined here,
# not there, so ManualChamberDriver.probe_temps() can key its readings off
# the same constants discover() advertised instead of two files each
# hand-typing the same literal strings.
PROBE_1_ADDRESS = "manual-probe-1"
PROBE_2_ADDRESS = "manual-probe-2"


@dataclass
class ManualChamberState:
    temp_f: float | None = 68.0
    mode: str = "idle"  # idle | cool | heat -- operator-settable directly
    health: Health = Health.OK
    commanded_target_f: float | None = None
    # Outlet state -- purely for dev-panel display/testing, since nothing
    # here simulates a real relay the way SimPlantEngine's relay does.
    cooling_on: bool = False
    heating_on: bool = False
    # Models a cooling-only rig with no heater wired at all -- when False,
    # the dev panel forces heating_on back off and disables the control.
    heating_enabled: bool = True
    # Models a 1-probe vs. 2-probe rig -- when enabled, this device also
    # becomes eligible for the beer-temp role (see platform.py's
    # capabilities) and gets a second, independently-settable reading for
    # the guided wizard's two-probe identify test.
    probe2_enabled: bool = False
    probe2_temp_f: float | None = 68.0


@dataclass
class ManualTiltState:
    temp_f: float | None = 68.0
    gravity_sg: float | None = 1.050
    health: Health = Health.OK
    # False = "not discoverable" -- omitted from scans entirely (platform.py)
    # and reads as unreachable if it's already mapped to a role.
    available: bool = True


class ManualPanel:
    """One process-wide store per daemon ctx -- the dev-panel API mutates
    this directly; the driver classes below just read/wrap it."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.chamber = ManualChamberState()
        self.tilt = ManualTiltState()


def _last_good_ts(clock: Clock, health: Health) -> float | None:
    return clock.now() if health == Health.OK else None


class ManualChamberDriver:
    def __init__(self, panel: ManualPanel, device_id: str | None = None):
        self._panel = panel  # device_id unused -- Manual has exactly one chamber device

    async def read_chamber(self) -> ChamberReading:
        s = self._panel.chamber
        return ChamberReading(
            temp_f=s.temp_f,
            mode=_MODE_MAP[s.mode],
            health=s.health,
            last_good_ts=_last_good_ts(self._panel.clock, s.health),
            commanded_target_f=s.commanded_target_f,
        )

    async def set_target(self, temp_f: float | None) -> None:
        self._panel.chamber.commanded_target_f = temp_f

    async def commanded_target(self) -> float | None:
        return self._panel.chamber.commanded_target_f

    async def set_ambient_location(self, location: str | None) -> None:
        pass  # Manual has no ambient/physics concept at all -- see the Protocol's own docstring.

    async def probe_temps(self) -> dict[str, float | None]:
        s = self._panel.chamber
        temps: dict[str, float | None] = {PROBE_1_ADDRESS: s.temp_f}
        if s.probe2_enabled:
            temps[PROBE_2_ADDRESS] = s.probe2_temp_f
        return temps


class ManualBeerTempSource:
    """Also the driver returned when beer_temp is mapped onto the chamber
    controller's optional second probe (probe2_enabled) -- both read the
    same shared panel.tilt state today. device_id is accepted (see
    daemon/drivers.py) but still unused here even though it's now
    available: both "which manual device is actually asking" paths read
    the identical shared state regardless, so there's nothing to
    disambiguate -- unlike Tilt's live.py, which genuinely needs it to
    pick a color."""

    def __init__(self, panel: ManualPanel, device_id: str | None = None):
        self._panel = panel

    async def read(self) -> BeerReading:
        s = self._panel.tilt
        if not s.available:
            return BeerReading(temp_f=None, health=Health.UNREACHABLE, last_good_ts=None)
        return BeerReading(temp_f=s.temp_f, health=s.health, last_good_ts=_last_good_ts(self._panel.clock, s.health))


class ManualGravitySource:
    def __init__(self, panel: ManualPanel, device_id: str | None = None):
        self._panel = panel

    async def read(self) -> GravityReading:
        s = self._panel.tilt
        if not s.available:
            return GravityReading(gravity_sg=None, health=Health.UNREACHABLE, last_good_ts=None)
        return GravityReading(
            gravity_sg=s.gravity_sg, health=s.health, last_good_ts=_last_good_ts(self._panel.clock, s.health)
        )
