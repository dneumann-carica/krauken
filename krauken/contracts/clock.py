"""Injectable clock. Control-loop and protection-timer code must read time
through this, never via time.time()/time.monotonic() directly -- that's what
lets tests compress multi-week fermentations and lets restart-safety tests
inject a mutated clock across a simulated crash.

Two implementations: ProductionClock (real wall-clock passthrough -- used
whenever Manual or any future real hardware platform is mapped to any role,
since both depend on genuine real-time pacing) and SimulatorClock (never
actually waits -- sleep() advances its own counters by the full requested
amount and returns immediately, so a whole simulated fermentation lifecycle
completes in real seconds rather than real weeks).

Deliberately ignorant of the simulator's own physics/relay-protection
state -- this module only ever hands back elapsed time, never calls into
platforms/simulator/live.py or any other driver. See daemon/app.py's
_select_clock for how a daemon picks which implementation to construct."""
from __future__ import annotations

import time
from typing import Protocol


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
