"""Resolves a mapped role to a live driver instance. Krauken/BrewPi aren't
implemented yet (later milestones) -- a role mapped to either currently
resolves to None, same as an unmapped role, since there's no live driver to
hand back. The control loop treats "no driver" and "driver present but
reading unhealthy" as distinct cases (see contracts/failsafe.py); this
module only ever returns None for the former.
"""
from __future__ import annotations

from typing import Any

from krauken.contracts.interfaces import BeerTempSource, ChamberDriver, GravitySource
from krauken.platforms.manual.live import ManualBeerTempSource, ManualChamberDriver, ManualGravitySource
from krauken.platforms.simulator.live import SimBeerTempSource, SimChamberDriver, SimGravitySource


def chamber_driver(ctx: Any, platform: str | None) -> ChamberDriver | None:
    if platform == "simulator":
        return SimChamberDriver(ctx.sim_engine)
    if platform == "manual":
        return ManualChamberDriver(ctx.manual_panel)
    return None


def beer_temp_source(ctx: Any, platform: str | None) -> BeerTempSource | None:
    if platform == "simulator":
        return SimBeerTempSource(ctx.sim_engine)
    if platform == "manual":
        return ManualBeerTempSource(ctx.manual_panel)
    return None


def gravity_source(ctx: Any, platform: str | None) -> GravitySource | None:
    if platform == "simulator":
        return SimGravitySource(ctx.sim_engine)
    if platform == "manual":
        return ManualGravitySource(ctx.manual_panel)
    return None
