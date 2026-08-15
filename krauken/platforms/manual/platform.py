"""The Manual driver: an operator-settable dev-panel platform. discover()
mirrors the real hardware shape that BrewPi/Krauken/Simulator all share --
one bundle-capable chamber controller (1 cooling outlet, optional heating
outlet, chamber probe, optional second/beer probe) plus a separate
Tilt-like hydrometer -- so a Manual-mapped rig exercises the exact same
Hardware Setup flow a real one would, not a simplified stand-in. Registers
through the exact same discover()/DeviceCandidate contract as every real
platform, per the project's decision that mock platforms must be
indistinguishable from real ones to the discovery/aggregation/UI code --
see platforms/registry.py.

Reads live off the shared ManualPanel (platforms/manual/live.py) injected
at construction, so a scan reflects whatever the dev panel currently has
set -- not a fixed, never-changing snapshot.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from krauken.contracts.models import DeviceCandidate, Health
from krauken.contracts.roles import CHAMBER_BUNDLE, Role
from krauken.platforms.manual.live import (
    PROBE_1_ADDRESS,
    PROBE_2_ADDRESS,
    ManualChamberState,
    ManualPanel,
    ManualTiltState,
)

PLATFORM_ID = "manual"
DISPLAY_NAME = "Manual (dev panel)"


def _fmt_temp(temp_f: float | None) -> str | None:
    return f"{temp_f:.1f}°F" if temp_f is not None else None


class ManualPlatform:
    platform_id = PLATFORM_ID
    display_name = DISPLAY_NAME

    def __init__(self, panel: ManualPanel | None = None):
        self._panel = panel

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]:
        chamber = self._panel.chamber if self._panel is not None else ManualChamberState()
        tilt = self._panel.tilt if self._panel is not None else ManualTiltState()

        candidates: list[DeviceCandidate] = [
            DeviceCandidate(
                device_id="manual:chamber",
                platform=PLATFORM_ID,
                display_name="Manual chamber controller",
                kind_label="Chamber controller - dev panel",
                capabilities=CHAMBER_BUNDLE | ({Role.BEER_TEMP} if chamber.probe2_enabled else frozenset()),
                bundled_roles=CHAMBER_BUNDLE,
                health=chamber.health,
                detail_line="Operator-settable, no real relays/probes",
                reading_summary=_fmt_temp(chamber.temp_f),
                readings={
                    "chamber_temp_f": chamber.temp_f,
                    **({"beer_temp_f": chamber.probe2_temp_f} if chamber.probe2_enabled else {}),
                },
                identity={
                    "probe_addresses": [PROBE_1_ADDRESS, PROBE_2_ADDRESS]
                    if chamber.probe2_enabled
                    else [PROBE_1_ADDRESS]
                },
                simulated=True,
                available_tests=("fire_outlet", "identify_probes"),
            ),
        ]

        if tilt.available:
            candidates.append(
                DeviceCandidate(
                    device_id="manual:tilt",
                    platform=PLATFORM_ID,
                    display_name="Manual Tilt",
                    kind_label="Bluetooth hydrometer - dev panel",
                    capabilities=frozenset({Role.BEER_TEMP, Role.BEER_GRAVITY}),
                    bundled_roles=frozenset(),
                    health=tilt.health,
                    detail_line="Operator-settable, no real BLE",
                    reading_summary=(
                        f"{_fmt_temp(tilt.temp_f)} - {tilt.gravity_sg:.3f}"
                        if tilt.temp_f is not None and tilt.gravity_sg is not None
                        else None
                    ),
                    readings={"beer_temp_f": tilt.temp_f, "gravity_sg": tilt.gravity_sg},
                    simulated=True,
                    available_tests=("live_read",),
                )
            )
        return candidates
