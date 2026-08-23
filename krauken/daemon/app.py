"""Daemon composition root. One factory, two parameterizations: production
(__main__.py, ProductionClock/SimulatorClock auto-selected by _select_clock
below, real db path) and tests/scenarios (krauken.daemon.testing,
SimulatorClock, a tmp db) -- no `if TESTING:` branches anywhere in the
daemon itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from pathlib import Path
from typing import Any

from krauken.contracts.clock import Clock, ProductionClock, SimulatorClock
from krauken.daemon.control_loop import DEFAULT_CONTROL_TICK_INTERVAL_S, control_tick
from krauken.daemon.control_state import ControlState
from krauken.db import queries, writes
from krauken.db.connection import open_ro, open_rw
from krauken.db.migrate import migrate
from krauken.ipc.server import IPCServer
from krauken.platforms.registry import PlatformRegistry

# Imported for their @op-decorated side effects (registers hardware.*/
# settings.*/fermentation.*/manual.* ops into krauken.ipc.server.OPS) -- not
# otherwise referenced in this module.
from krauken.daemon.ops import hardware as _hardware_ops  # noqa: F401
from krauken.daemon.ops import settings as _settings_ops  # noqa: F401
from krauken.daemon.ops import fermentation as _fermentation_ops  # noqa: F401
from krauken.daemon.ops import dev_panel as _dev_panel_ops  # noqa: F401

log = logging.getLogger("krauken.daemon")


class DaemonContext:
    """Passed as `ctx` to every IPC op handler.

    Deliberately holds no reference to any concrete platform class
    (BrewPiConnection, TiltScanner, ManualIpcConnection,
    SimulatorIpcConnection) or its construction parameters (a socket
    path, an hci device number) -- that's all owned by
    platforms/registry.py's PlatformRegistry now. This context only ever
    holds `registry` itself: an iterable of PlatformDriver (for
    discover()) plus state_for(platform_id) (for daemon/drivers.py's
    role-dispatch) -- generic handles, never the concrete hardware
    behind them."""

    def __init__(self, *, db_path: Path, clock: Clock):
        self.db_path = db_path
        self.clock = clock
        self.conn = open_rw(db_path)
        self.registry = PlatformRegistry(clock=clock)
        # Serializes actual SQLite writes from background tasks (a scan's
        # device upserts) against each other and against control-tick
        # writes -- separate from IPCServer.state_lock, which only covers
        # the duration of a single op *call*, not a whole background job.
        self.db_lock = asyncio.Lock()
        self.jobs: dict[str, Any] = {}
        self.control_state = ControlState()


class Daemon:
    def __init__(
        self,
        *,
        ctx: DaemonContext,
        socket_path: Path,
        heartbeat_interval_s: float,
        control_tick_interval_s: float = DEFAULT_CONTROL_TICK_INTERVAL_S,
    ):
        self.ctx = ctx
        self.heartbeat_interval_s = heartbeat_interval_s
        self.control_tick_interval_s = control_tick_interval_s
        self.server = IPCServer(socket_path, ctx)
        self._heartbeat_task: asyncio.Task | None = None
        self._control_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        # Generic -- the daemon doesn't know or care which concrete
        # platforms exist, how many there are, or which ones are IPC-backed
        # vs. in-process. Each platform's own start() knows whether it
        # needs to background itself (BrewPiConnection.start()) or can
        # return quickly (everything else); PlatformRegistry.start_all()
        # itself tolerates any one of them being unreachable.
        await self.ctx.registry.start_all()
        await self.server.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._control_task = asyncio.create_task(self._control_loop())
        log.info("daemon started")

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._heartbeat_task, self._control_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.server.stop()
        # Any still-running background job -- a hardware scan
        # (discovery.py's ScanJob) or a hardware test (tests_runtime.py's
        # TestJob), the only two job kinds that track their own `_task` --
        # must be cancelled and awaited BEFORE the db connection closes
        # below, or it can crash into "Cannot operate on a closed
        # database" the next time it reaches a write. Confirmed live: a
        # scan that outlives whatever budget its caller gave up on
        # (db/seed.py's _scan_and_wait, on real out-of-process IPC) used to
        # keep running as an orphaned task straight through this shutdown
        # and hit exactly that. Duck-typed via getattr, not an isinstance
        # check against ScanJob/TestJob -- this context deliberately holds
        # no concrete-class knowledge of what a "job" is (see
        # DaemonContext's own docstring above).
        for job in list(self.ctx.jobs.values()):
            task: asyncio.Task | None = getattr(job, "_task", None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.ctx.registry.stop_all()
        self.ctx.conn.close()
        log.info("daemon stopped")

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.stop()

    async def _heartbeat_loop(self) -> None:
        # Only writes the generic "alive" heartbeat when nothing else is --
        # once a fermentation is active, the control loop keeps live_state
        # fresh with real telemetry every tick, and a slower heartbeat
        # blindly overwriting that moments later with a bare {"status":
        # "alive"} would make the UI's live view flicker back to nothing.
        #
        # Paces itself with a REAL asyncio.sleep(), not ctx.clock.sleep() --
        # this loop's own cadence is a process-liveness concern (like the
        # IPC layer's timeouts, contracts/clock.py's docstring), not
        # simulated fermentation time. Under SimulatorClock (which never
        # really waits), racing the SAME shared clock forward independently
        # of the control loop's own pacing was a real, confirmed bug: since
        # this branch is nearly a no-op once a fermentation is active, it
        # could complete vastly more iterations per unit of real scheduling
        # time than the control loop's much heavier tick could, dragging
        # the shared clock ahead unpredictably between one control tick and
        # the next -- producing huge, inconsistent dt_s values and a
        # runaway chamber-temp oscillation that had nothing to do with the
        # physics coefficients themselves. The timestamp written still uses
        # ctx.clock.now(), so the recorded event stays consistent with the
        # fermentation's own simulated timeline -- only this loop's WAKE
        # cadence is real.
        while True:
            async with self.ctx.db_lock:
                if queries.active_fermentation(self.ctx.conn) is None:
                    as_of = datetime.datetime.fromtimestamp(
                        self.ctx.clock.now(), tz=datetime.timezone.utc
                    ).isoformat()
                    writes.write_live_state(self.ctx.conn, as_of, {"status": "alive"})
            await asyncio.sleep(self.heartbeat_interval_s)

    async def _control_loop(self) -> None:
        # Runs under server.state_lock so a tick never interleaves with an
        # in-progress mutating IPC op (see ipc/server.py's module
        # docstring) -- e.g. a fermentation.terminate call and a tick can't
        # both be mid-write against the same fermentation at once.
        while True:
            try:
                async with self.server.state_lock:
                    await control_tick(self.ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick must never permanently kill control. Left
                # uncaught, an exception here propagates straight out of
                # this loop's `while True` and silently ends _control_task
                # for the rest of the process's life -- silently, because
                # `self` keeps its own reference to the task (see start()
                # below), so it's never garbage collected either, and
                # asyncio's own "Task exception was never retrieved"
                # logging only fires from Task.__del__. The daemon's own
                # /health still reports "ok" (that only checks the IPC
                # socket, not this loop), so a real fermentation could sit
                # completely uncontrolled -- no heating, no cooling, no new
                # samples -- with nothing in the log to say why. Log it and
                # keep ticking instead; whatever caused this one tick to
                # fail gets another chance next tick.
                log.exception("control tick failed -- will retry next tick")
            await self.ctx.clock.sleep(self.control_tick_interval_s)


def _select_clock(db_path: Path) -> Clock:
    """SimulatorClock iff every currently-mapped hardware role uses the
    simulator platform (an unmapped optional role, e.g. gravity, doesn't
    count against this) -- otherwise ProductionClock, since Manual (and any
    future real hardware platform) depends on genuine real-time pacing that
    a never-waits clock would break. Evaluated once, here, at daemon
    startup -- not hot-swapped mid-session. Changing your hardware mapping
    across this line requires a daemon restart to take effect, consistent
    with how KRAUKEN_CONTROL_TICK_INTERVAL_S and other daemon-level config
    already require one."""
    conn = open_ro(db_path)
    try:
        mapped_platforms = {row["platform"] for row in queries.hardware_config(conn) if row["platform"] is not None}
    finally:
        conn.close()
    if mapped_platforms and mapped_platforms == {"simulator"}:
        # Anchored to real wall-clock "now" at construction time, not
        # SimulatorClock's own start=0.0 default (1970-01-01) -- it still
        # never really waits (ticks race forward exactly as before), this
        # only changes what "now" MEANS at the moment the clock is built.
        # Without this, every daemon restart resets the simulated clock
        # back to epoch 0, so a batch started in a freshly-restarted
        # process gets a started_at in 1970/71/etc. -- confusingly BEFORE
        # older real batches from a process that had been running longer,
        # and jarringly before the demo batches, whose dates come from a
        # separate pipeline (db/seed.py) that already anchors to real
        # "now" at seed time for exactly this readability reason (see that
        # module's docstring). daemon/testing.py's build_scenario_daemon()
        # made the identical choice for its own DEFAULT_SCENARIO_START_TS,
        # for the identical reason -- this just extends it to the real
        # daemon's own startup path, which never got the same treatment.
        return SimulatorClock(start=datetime.datetime.now(datetime.timezone.utc).timestamp())
    return ProductionClock()


def build_daemon(
    *,
    db_path: Path,
    clock: Clock | None = None,
    socket_path: Path,
    heartbeat_interval_s: float = 60.0,
    control_tick_interval_s: float = DEFAULT_CONTROL_TICK_INTERVAL_S,
) -> Daemon:
    migrate(db_path)
    ctx = DaemonContext(db_path=db_path, clock=clock or _select_clock(db_path))
    return Daemon(
        ctx=ctx, socket_path=socket_path, heartbeat_interval_s=heartbeat_interval_s,
        control_tick_interval_s=control_tick_interval_s,
    )
