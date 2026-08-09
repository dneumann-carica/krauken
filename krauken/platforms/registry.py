"""Which platforms are active in this process. Ordinary registry entries --
nothing in discovery/aggregation/the UI can tell a mock platform from a real
one. Real platforms (Krauken, BrewPi, Tilt) land in later milestones; only
Manual/Simulator exist today, which is exactly why M1's Hardware Setup flow
can be built and demoed with zero real hardware.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from krauken.contracts.interfaces import PlatformDriver
from krauken.platforms.manual.platform import ManualPlatform
from krauken.platforms.simulator.platform import SimulatorPlatform

if TYPE_CHECKING:
    from krauken.platforms.manual.live import ManualPanel
    from krauken.platforms.simulator.live import SimPlantEngine

# Real platforms aren't implemented yet, so there's no meaningful
# "production default" to gate behind KRAUKEN_ENABLE_MOCK_PLATFORMS yet --
# Manual/Simulator are simply always available until something real exists
# to distinguish them from. Revisit this default once krauken/platforms/krauken
# (or any real platform) lands.
DEFAULT_PLATFORMS = ("manual", "simulator")


def enabled_platform_ids() -> tuple[str, ...]:
    raw = os.environ.get("KRAUKEN_PLATFORMS")
    if raw:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return DEFAULT_PLATFORMS


def build_registry(
    *, manual_panel: "ManualPanel | None" = None, sim_engine: "SimPlantEngine | None" = None
) -> list[PlatformDriver]:
    """Manual/Simulator each read live off a shared instance the daemon
    also ticks/mutates elsewhere (ctx.manual_panel / ctx.sim_engine) --
    injected here rather than each platform reaching into a global, so
    discover() genuinely reflects current dev-panel/physics state instead
    of a fixed snapshot. Both default to None so tests that only care about
    the static parts of a candidate (capabilities, bundling, tests) can
    call build_registry() with no daemon behind it at all."""
    drivers: list[PlatformDriver] = []
    for platform_id in enabled_platform_ids():
        if platform_id == "manual":
            drivers.append(ManualPlatform(manual_panel))
        elif platform_id == "simulator":
            drivers.append(SimulatorPlatform(sim_engine))
        # else: unknown/not-yet-implemented platform id -- ignore rather
        # than crash the daemon (krauken/brewpi/tilt land in later milestones)
    return drivers
