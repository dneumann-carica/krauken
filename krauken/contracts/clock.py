"""Injectable clock. Control-loop and protection-timer code must read time
through this, never via time.time()/time.monotonic() directly -- that's what
lets tests compress multi-week fermentations and lets restart-safety tests
inject a mutated clock across a simulated crash.

Three implementations: ProductionClock (real wall-clock passthrough -- used
whenever Manual or any future real hardware platform is mapped to any role,
since both depend on genuine real-time pacing), SimulatorClock (never
actually waits -- sleep() advances its own counters by the full requested
amount and returns immediately, so a whole simulated fermentation lifecycle
completes in real seconds rather than real weeks), and RemoteClock (driven
entirely by another process -- see its own docstring).

Deliberately ignorant of the simulator's own physics/relay-protection
state -- this module only ever hands back elapsed time, never calls into
platforms/simulator/live.py or any other driver. See daemon/app.py's
_select_clock for how a daemon picks which implementation to construct.

The daemon is the one place that decides real-time vs. compressed --
whichever platform's process actually ends up doing the time-dependent
work (SimPlantEngine's thermal/gravity model, today) never makes that
choice itself. See RemoteClock's own docstring and
platforms/ipc_service.py's clock.sync op for the mechanism that keeps an
out-of-process platform's clock on the exact same timeline the daemon
itself is using."""
from __future__ import annotations

import time
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Wall-clock unix seconds. For persistence/display only -- never for
        timer arithmetic (NTP can step this)."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, immune to wall-clock steps. All timer math
        (min-on/min-off/lockout, beer-temp-lost thresholds) uses this."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class ProductionClock:
    """Real wall-clock passthrough. Used whenever Manual (or any future real
    hardware platform) is mapped to any role -- both depend on genuine
    real-time pacing (a human reacting to a commanded target, or an actual
    physical compressor's own timing), so nothing here throttles or
    accelerates anything."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


class SimulatorClock:
    """Never actually waits -- sleep(seconds) advances now()/monotonic() by
    the full requested amount and returns immediately. This is the entire
    mechanism that lets a simulated fermentation lifecycle complete in real
    seconds: nothing throttles the daemon's own _control_loop/_heartbeat_loop
    (daemon/app.py) once this clock is active, since their only real-time
    wait is `await ctx.clock.sleep(interval_s)` between ticks.

    Selected automatically at daemon startup when every currently-mapped
    hardware role uses the simulator platform (daemon/app.py's
    _select_clock) -- not a dev-panel dial, no multiplier/preset concept.
    Also used directly by daemon/testing.py's build_scenario_daemon() for
    compressed-time scenario tests -- one implementation, not a separate
    test-only stand-in duplicating the same behavior.

    advance() is kept as an explicit, direct bump for unit tests that drive
    a driver's physics without going through a real control loop's own
    sleep()-based cadence (see tests/unit/test_sim_live.py) -- sleep() is
    just advance() plus a mandatory cooperative yield."""

    def __init__(self, start: float = 0.0):
        self._now = start
        self._mono = start

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        # Must actually yield to the event loop (await asyncio.sleep(0)), not
        # just return -- a bare advance() with no suspension point turns any
        # `while True: ...; await clock.sleep(...)` loop into a genuine
        # busy-spin that starves every other task/coroutine, including the
        # one waiting on this loop's own first side effect.
        import asyncio

        self.advance(seconds)
        await asyncio.sleep(0)


class RemoteClock:
    """A Clock instance whose value is set entirely from the outside, by
    another process -- used by every out-of-process platform (Simulator
    today, a real Hardware Supervisor later) so its own time-dependent
    behavior (SimPlantEngine's thermal/gravity model) stays on the exact
    same timeline the daemon itself is using, whether that's real time
    (ProductionClock) or compressed scenario time (SimulatorClock). The
    remote platform never decides this for itself -- see
    platforms/ipc_service.py's clock.sync op, called once per daemon
    control tick (daemon/drivers.py's sync_remote_clocks()), cheap and
    idempotent, same idiom as ChamberDriver.set_ambient_location's "called
    every tick regardless of who's actually listening."

    Starts from a real wall-clock snapshot -- sane before the daemon's
    first sync arrives (e.g. the gap between this process starting and the
    daemon's first tick) -- then only ever changes via set(). Granularity
    is therefore the daemon's own control_tick_interval_s (default 30s),
    not continuous: a read between two syncs sees whatever the last sync
    reported. That's exact for SimulatorClock (time genuinely doesn't move
    between explicit advances, so there's nothing to miss) and a
    deliberate, accepted approximation under ProductionClock (real
    per-tick freshness, not sub-tick precision).

    `on_first_sync`, if set, fires exactly once, synchronously inside the
    very first set() call, before anything else can observe the new value.
    This is what lets a consumer with its own anchored elapsed-time state
    (SimPlantEngine's _start_mono, _chamber_last_mono, _beer_last_mono --
    all captured at construction, via this clock, before any real daemon
    has ever synced it) re-anchor itself the moment real values arrive,
    instead of computing an elapsed time between the construction-time
    wall-clock guess and a wildly different first synced value (e.g.
    SimulatorClock's own start=1_700_000_000-ish epoch) -- a real, observed
    bug: gravity_at()'s math.exp() overflowing on an elapsed "time" of
    several hundred thousand simulated hours."""

    def __init__(self) -> None:
        self._now = time.time()
        self._mono = time.monotonic()
        self._synced_once = False
        self.on_first_sync: Callable[[], None] | None = None

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, *, now: float, monotonic: float) -> None:
        self._now = now
        self._mono = monotonic
        if not self._synced_once:
            self._synced_once = True
            if self.on_first_sync is not None:
                self.on_first_sync()

    async def sleep(self, seconds: float) -> None:
        # Nothing driven by THIS clock ever calls sleep() on it -- the
        # remote engine only ever reads now()/monotonic(); pacing is
        # entirely the daemon's own job. A real sleep anyway, for Clock
        # Protocol conformance and so a future caller that does use it gets
        # correct behavior instead of a silent no-op.
        import asyncio

        await asyncio.sleep(seconds)
