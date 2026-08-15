"""The Simulator's own out-of-process entry point: a SimPlantEngine behind
the generic IPC service scaffolding (platforms/ipc_service.py) plus this
platform's own dev-panel ops (simulator.get_readings/set_probe2), which the
API talks to DIRECTLY (see api/routers/dev_panel.py) -- not through the
daemon, and deliberately not part of the ChamberDriver/BeerTempSource/
GravitySource/PlatformDriver Protocols the generic ops expose. See
ipc_service.py's own module docstring for why those two stay separate: the
driver Protocols stay pure control/discovery, nothing dev-tooling-specific
ever leaks into them.

Meant to run as its own OS process (krauken-simulator, deploy/
krauken-simulator.service) -- see __main__.py for the real entry point.
daemon/testing.py's scenario-test harness builds one the exact same way,
just in the same process as the test itself, wired to a real (if
short-lived) unix socket -- there is only ever one build path, no separate
test-only stand-in.

The engine's clock is always a RemoteClock (contracts/clock.py) -- this
process never decides real-time vs. compressed for itself; the daemon owns
that choice and pushes it here every control tick (daemon/drivers.py's
sync_remote_clocks(), platforms/ipc_service.py's clock.sync op)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from krauken.contracts.clock import RemoteClock
from krauken.ipc.server import IPCServer, op
from krauken.platforms.ipc_service import ServiceContext
from krauken.platforms.simulator.live import SimBeerTempSource, SimChamberDriver, SimGravitySource, SimPlantEngine
from krauken.platforms.simulator.platform import SimulatorPlatform

log = logging.getLogger("krauken.platforms.simulator.service")


class SimulatorServiceContext(ServiceContext):
    """Adds the raw SimPlantEngine -- needed by this platform's own
    dev-panel ops below (probe2 controls), which reach past the generic
    ChamberDriver Protocol on purpose. Nothing in ipc_service.py's shared
    ops ever touches this attribute."""

    def __init__(self, engine: SimPlantEngine):
        super().__init__(
            platform=SimulatorPlatform(engine),
            chamber=SimChamberDriver(engine),
            beer_temp=SimBeerTempSource(engine),
            gravity=SimGravitySource(engine),
            clock=engine.clock,
        )
        self.engine = engine


@op("simulator.get_readings")
async def _get_readings(ctx: SimulatorServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    chamber = ctx.engine.read_chamber()
    return {
        "chamber_temp_f": chamber.temp_f,
        "mode": chamber.mode.value,
        "probe2_enabled": ctx.engine.probe2_enabled,
        "probe2_temp_f": ctx.engine.probe2_temp_f,
    }


@op("simulator.set_probe2", mutating=True)
async def _set_probe2(ctx: SimulatorServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    if "enabled" in args:
        ctx.engine.set_probe2_enabled(bool(args["enabled"]))
    if "temp_f" in args:
        value = args["temp_f"]
        ctx.engine.set_probe2_temp(None if value is None else float(value))
    return {"probe2_enabled": ctx.engine.probe2_enabled, "probe2_temp_f": ctx.engine.probe2_temp_f}


@op("simulator.reset_for_new_batch", mutating=True)
async def _reset_for_new_batch(ctx: SimulatorServiceContext, args: Mapping[str, Any]) -> dict[str, Any]:
    """Called by daemon/fermentation.py's start_fermentation() -- see its
    own call site for why this matters (without it, gravity/exotherm
    silently carry forward from whenever this process itself started, not
    from this fermentation's actual start). Platform-specific by nature
    (SimPlantEngine.reset_for_new_batch()'s own docstring), same as
    simulator.set_probe2 above -- not part of the generic ChamberDriver
    Protocol ipc_service.py exposes."""
    ctx.engine.reset_for_new_batch()
    return {}


class SimulatorService:
    def __init__(self, *, socket_path: Path):
        clock = RemoteClock()
        self.engine = SimPlantEngine(clock)
        # The engine's own _start_mono/_chamber_last_mono/_beer_last_mono
        # are anchored via this SAME clock at construction time, above --
        # before the daemon has ever synced it (this process starts
        # independently, per the whole point of the out-of-process split).
        # Re-anchor once real values arrive instead of computing an
        # elapsed time between the construction-time wall-clock guess and
        # a wildly different first synced value (RemoteClock's own
        # docstring has the full story, including the real bug this
        # fixes).
        clock.on_first_sync = self.engine.reset_for_new_batch
        self.ctx = SimulatorServiceContext(self.engine)
        self.server = IPCServer(socket_path, self.ctx)

    async def start(self) -> None:
        await self.server.start()
        log.info("simulator service started")

    async def stop(self) -> None:
        await self.server.stop()
        log.info("simulator service stopped")


def build_service(*, socket_path: Path) -> SimulatorService:
    return SimulatorService(socket_path=socket_path)
