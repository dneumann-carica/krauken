"""The Simulator driver. Emits TWO candidates from one conceptual plant,
deliberately -- not one combined candidate. A single combined candidate
would let a developer complete the whole Hardware Setup flow without ever
exercising the bundle rule or the independent-beer-temp-source path, since
everything would just be "the simulator." Two candidates make a fully
mocked dev environment a genuine exercise of the real config state machine:
a chamber controller (bundle) and a separate hydrometer-like device
(independent beer temp + gravity), mirroring how Krauken+Tilt or BrewPi+Tilt
would really be assigned.

The full thermal/gravity plant model (used to generate the demo batch's
historical data) lives in platforms/simulator/plant.py. discover() here
reads the SAME live engine (injected at construction) the control loop
ticks, so a scan reflects genuinely current readings -- not a fixed,
never-changing snapshot.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from krauken.contracts.models import DeviceCandidate, Health
from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.simulator.live import SimPlantEngine

PLATFORM_ID = "simulator"
DISPLAY_NAME = "Simulator"


class SimulatorPlatform:
    platform_id = PLATFORM_ID
    display_name = DISPLAY_NAME

    def __init__(self, engine: SimPlantEngine | None = None):
        self._engine = engine

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]:
        chamber_f = self._engine.read_chamber().temp_f if self._engine is not None else 66.8
        beer_f = self._engine.read_beer().temp_f if self._engine is not None else 67.4
        gravity_sg = self._engine.read_gravity().gravity_sg if self._engine is not None else 1.042
        probe2 = bool(self._engine is not None and self._engine.probe2_enabled)

        return [
            DeviceCandidate(
                device_id="simulator:chamber",
                platform=PLATFORM_ID,
                display_name="Simulated chamber controller",
                kind_label="Chamber controller - simulated",
                capabilities=CHAMBER_BUNDLE | ({Role.BEER_TEMP} if probe2 else frozenset()),
                bundled_roles=CHAMBER_BUNDLE,
                health=Health.OK,
                detail_line="Outlet 1 cool / Outlet 2 heat - simulated plant",
                reading_summary=f"{chamber_f:.1f}°F chamber" if chamber_f is not None else None,
                readings={"chamber_temp_f": chamber_f},
                identity={"probe_addresses": ["sim-probe-1", "sim-probe-2"] if probe2 else ["sim-probe-1"]},
                simulated=True,
                available_tests=("fire_outlet", "identify_probes"),
            ),
            DeviceCandidate(
                device_id="simulator:tilt",
                platform=PLATFORM_ID,
                display_name="Simulated Tilt - Purple",
                kind_label="Bluetooth hydrometer - simulated",
                capabilities=frozenset({Role.BEER_TEMP, Role.BEER_GRAVITY}),
                bundled_roles=frozenset(),
                health=Health.OK,
                detail_line="RSSI -60 dBm - simulated",
                reading_summary=(
                    f"{beer_f:.1f}°F - {gravity_sg:.3f}" if beer_f is not None and gravity_sg is not None else None
                ),
                readings={"beer_temp_f": beer_f, "gravity_sg": gravity_sg},
                identity={"rssi_dbm": -60, "battery_weeks": 3},
                simulated=True,
                available_tests=("live_read",),
            ),
        ]
