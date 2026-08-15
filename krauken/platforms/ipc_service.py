"""Generic IPC service scaffolding -- the server-side half of the
out-of-process pattern, matching platforms/ipc_driver.py's client-side
classes op-for-op. Registers @op handlers keyed by the ChamberDriver/
BeerTempSource/GravitySource/PlatformDriver Protocol methods themselves, so
any process that constructs a ServiceContext around real driver instances
and runs an IPCServer with these ops registered can be reached by
ipc_driver.py's Ipc* classes -- Simulator and Manual today, a real Hardware
Supervisor later, with no changes needed here for either.

Each process using this module still registers its OWN dev-panel ops
separately (see platforms/simulator/service.py, platforms/manual/service.py)
-- those are per-platform and deliberately NOT part of this shared,
Protocol-shaped vocabulary (see contracts/interfaces.py: the driver
Protocols stay pure control/discovery, nothing dev-tooling-specific)."""
from __future__ import annotations

from typing import Any, Mapping

from krauken.contracts.clock import RemoteClock
from krauken.contracts.interfaces import BeerTempSource, ChamberDriver, GravitySource, PlatformDriver
from krauken.contracts.models import DeviceCandidate
from krauken.ipc.server import op


class ServiceContext:
    """Passed as `ctx` to every op below -- one instance per process,
    holding whichever real driver objects that process actually backs.
    `chamber`/`beer_temp`/`gravity` may each be None if this platform
    genuinely can't fill that role (none of Simulator/Manual leave any
    unfilled today, but the shape allows for one that does).

    `clock` is the same RemoteClock instance the process's own driver
    objects were constructed with (e.g. SimPlantEngine's clock) -- the
    clock.sync op below updates it in place, which is what lets a shared
    engine's own time-dependent physics stay on the daemon's timeline
    without this module needing to know anything about SimPlantEngine
    specifically."""

    def __init__(
        self,
        *,
        platform: PlatformDriver,
        chamber: ChamberDriver | None,
        beer_temp: BeerTempSource | None,
        gravity: GravitySource | None,
        clock: RemoteClock,
    ):
        self.platform = platform
        self.chamber = chamber
        self.beer_temp = beer_temp
        self.gravity = gravity
        self.clock = clock


def _candidate_to_wire(c: DeviceCandidate) -> dict[str, Any]:
    return {
        "device_id": c.device_id,
        "platform": c.platform,
        "display_name": c.display_name,
        "kind_label": c.kind_label,
        "capabilities": list(c.capabilities),
        "bundled_roles": list(c.bundled_roles),
        "health": c.health.value,
        "health_note": c.health_note,
        "detail_line": c.detail_line,
        "reading_summary": c.reading_summary,
        "readings": dict(c.readings),
        "identity": dict(c.identity),
        "last_seen_ts": c.last_seen_ts,
        "requires_setup": c.requires_setup,
        "available_tests": list(c.available_tests),
        "simulated": c.simulated,
        "platform_config": dict(c.platform_config),
    }


@op("clock.sync", mutating=True)
async def _clock_sync(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    ctx.clock.set(now=args["now"], monotonic=args["monotonic"])
    return {}


@op("platform.discover")
async def _discover(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    candidates = await ctx.platform.discover({})
    return {"candidates": [_candidate_to_wire(c) for c in candidates]}


@op("chamber.read_chamber")
async def _chamber_read(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    r = await ctx.chamber.read_chamber()
    return {
        "temp_f": r.temp_f,
        "mode": r.mode.value,
        "health": r.health.value,
        "last_good_ts": r.last_good_ts,
        "commanded_target_f": r.commanded_target_f,
        "detail": r.detail,
    }


@op("chamber.set_target", mutating=True)
async def _chamber_set_target(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    await ctx.chamber.set_target(args.get("temp_f"))
    return {}


@op("chamber.commanded_target")
async def _chamber_commanded_target(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"temp_f": await ctx.chamber.commanded_target()}


@op("chamber.set_ambient_location", mutating=True)
async def _chamber_set_ambient_location(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    await ctx.chamber.set_ambient_location(args.get("location"))
    return {}


@op("chamber.probe_temps")
async def _chamber_probe_temps(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"temps": await ctx.chamber.probe_temps()}


@op("beer_temp.read")
async def _beer_temp_read(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    r = await ctx.beer_temp.read()
    return {"temp_f": r.temp_f, "health": r.health.value, "last_good_ts": r.last_good_ts}


@op("gravity.read")
async def _gravity_read(ctx: ServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    r = await ctx.gravity.read()
    return {"gravity_sg": r.gravity_sg, "health": r.health.value, "last_good_ts": r.last_good_ts}
