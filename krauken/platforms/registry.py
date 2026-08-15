"""Which platforms are active in this process, and the one place that maps
a platform_id string to everything that platform_id means -- its discovery
driver (PlatformDriver, for scanning) and its live role drivers
(ChamberDriver/BeerTempSource/GravitySource, for daemon/drivers.py to hand
the control loop). Nothing in discovery/aggregation/the UI can tell a mock
platform from a real one. Krauken (the Hardware Supervisor platform) is
still a later milestone; Manual/Simulator/BrewPi/Tilt all exist today.

Both PLATFORM_BINDINGS' consumers (this module's PlatformRegistry and
daemon/drivers.py's role-driver factories) read the same table instead of
each hand-maintaining their own if/elif chain of platform_id strings --
adding a platform later is one new PlatformBinding row, not four matching
branches spread across two files that can drift out of sync.

PlatformRegistry -- not the daemon -- is the ONLY thing that ever
constructs a BrewPiConnection, TiltScanner, ManualIpcConnection, or
SimulatorIpcConnection. This used to be daemon/app.py's job (DaemonContext
constructed each one directly, held it under a named ctx attribute, and
managed its start()/stop() itself) -- an abstraction violation: the daemon
composition root has no business knowing any of these concrete classes, or
even that Manual/Simulator are IPC-backed while BrewPi/Tilt are in-process,
exist at all. Now the daemon only ever holds a PlatformRegistry and asks it
generic questions (iterate for PlatformDriver.discover(), state_for(id) for
role-driver dispatch, start_all()/stop_all() for lifecycle) -- it never
sees a concrete platform class or its construction parameters (a socket
path, an hci device number) anywhere.

Manual and Simulator each run as their own out-of-process server
(platforms/manual/service.py, platforms/simulator/service.py) -- their
classes below are platforms/ipc_driver.py's generic IPC-backed
implementations, wrapping an IpcPlatformConnection (which itself resolves
its own socket path -- see that class's own docstring). BrewPi and Tilt are
in-process (a persistent pyserial connection, a persistent raw-HCI BLE
listener) -- and it turns out PlatformBinding/PlatformRegistry never
actually assumed IPC in the first place: `build_state` just constructs
whatever object this platform's state is, `cls(state_obj, device_id)`
doesn't care what kind of object that is. So a real non-IPC platform
binding looks almost identical to an IPC one from this module's point of
view -- a different constructor behind the same dispatch shape, not a
structural change.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from krauken.contracts.clock import Clock
from krauken.contracts.errors import PlatformUnavailable
from krauken.contracts.interfaces import BeerTempSource, ChamberDriver, GravitySource, PlatformDriver
from krauken.platforms.brewpi.connection import BrewPiConnection
from krauken.platforms.brewpi.live import BrewPiBeerTempSource, BrewPiChamberDriver
from krauken.platforms.brewpi.platform import BrewPiPlatform
from krauken.platforms.ipc_driver import (
    IpcBeerTempSource,
    IpcChamberDriver,
    IpcGravitySource,
    IpcPlatformDriver,
    ManualIpcConnection,
    SimulatorIpcConnection,
)
from krauken.platforms.tilt.live import TiltBeerTempSource, TiltGravitySource
from krauken.platforms.tilt.platform import TiltPlatform
from krauken.platforms.tilt.scanner import TiltScanner

log = logging.getLogger("krauken.platforms.registry")


class _ManualIpcPlatform(IpcPlatformDriver):
    platform_id = "manual"
    display_name = "Manual (dev panel)"


class _SimulatorIpcPlatform(IpcPlatformDriver):
    platform_id = "simulator"
    display_name = "Simulator"


@dataclass(frozen=True)
class PlatformBinding:
    """Everything the rest of the daemon needs to know about one platform_id.

    `build_state` constructs that platform's one shared connection/client/
    scanner object, given the daemon's Clock -- PlatformRegistry calls this
    itself, exactly once per enabled platform per daemon process; nothing
    outside this module ever calls it directly. `platform_cls`
    (discover()-only, no particular device in mind yet) is always just
    `cls(state_obj)`. The three role-driver classes are `cls(state_obj,
    device_id)` -- device_id lets a platform with more than one live
    candidate (Tilt, today) pick the specific one that was actually
    mapped; everything else ignores it (see daemon/drivers.py's own
    docstring). Either way, dispatch needs no per-platform special-casing.
    """

    build_state: Callable[[Clock], Any]
    platform_cls: type[PlatformDriver]
    chamber_driver_cls: type[ChamberDriver] | None = None
    beer_temp_source_cls: type[BeerTempSource] | None = None
    gravity_source_cls: type[GravitySource] | None = None


PLATFORM_BINDINGS: dict[str, PlatformBinding] = {
    "manual": PlatformBinding(
        build_state=lambda clock: ManualIpcConnection(),
        platform_cls=_ManualIpcPlatform,
        chamber_driver_cls=IpcChamberDriver,
        beer_temp_source_cls=IpcBeerTempSource,
        gravity_source_cls=IpcGravitySource,
    ),
    "simulator": PlatformBinding(
        build_state=lambda clock: SimulatorIpcConnection(),
        platform_cls=_SimulatorIpcPlatform,
        chamber_driver_cls=IpcChamberDriver,
        beer_temp_source_cls=IpcBeerTempSource,
        gravity_source_cls=IpcGravitySource,
    ),
    "brewpi": PlatformBinding(
        build_state=lambda clock: BrewPiConnection(clock=clock),
        platform_cls=BrewPiPlatform,
        chamber_driver_cls=BrewPiChamberDriver,
        beer_temp_source_cls=BrewPiBeerTempSource,
        # No GravitySource -- BrewPi has no hydrometer of its own.
        gravity_source_cls=None,
    ),
    "tilt": PlatformBinding(
        # No hci_device passed -- TiltScanner resolves KRAUKEN_TILT_HCI_DEVICE
        # itself (see its own module docstring/constructor).
        build_state=lambda clock: TiltScanner(clock),
        platform_cls=TiltPlatform,
        # No ChamberDriver -- Tilt only ever fills Beer Temp/Beer Gravity.
        chamber_driver_cls=None,
        beer_temp_source_cls=TiltBeerTempSource,
        gravity_source_cls=TiltGravitySource,
    ),
    # krauken (the GPIO Hardware Supervisor platform) lands in a later
    # milestone -- an unknown platform_id simply has no binding, which
    # every lookup below already treats as "ignore" (PlatformRegistry) or
    # "no such driver" (daemon/drivers.py).
}

# Decision made building BrewPi/Tilt (flagged for ratification): all four
# implemented platforms are enabled by default now, mocks and real hardware
# alike -- discover() degrades to "unavailable"/empty on a machine with no
# real hardware (a dev Mac with no /dev/ttyACM* just gets zero BrewPi
# candidates), so there's no harm in always trying. A prior comment here
# floated a KRAUKEN_ENABLE_MOCK_PLATFORMS toggle to separate "production"
# from "mock" defaults once something real existed -- deliberately NOT
# built now; introducing a mock/real split is a bigger behavior change
# than "add two new platforms" should carry, and nothing about the current
# scope needs it (KRAUKEN_PLATFORMS already lets any deployment override
# this list explicitly).
DEFAULT_PLATFORMS = ("manual", "simulator", "brewpi", "tilt")


def enabled_platform_ids() -> tuple[str, ...]:
    raw = os.environ.get("KRAUKEN_PLATFORMS")
    if raw:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return DEFAULT_PLATFORMS


class PlatformRegistry:
    """Owns constructing every enabled platform's concrete state object
    (BrewPiConnection, TiltScanner, ManualIpcConnection,
    SimulatorIpcConnection) AND wrapping each in its discover()-only
    PlatformDriver -- the daemon never does either, and never sees which
    concrete classes exist at all. Constructed once per daemon process
    (not per scan); iterable (yields PlatformDriver instances, so
    daemon/discovery.py's `for p in self.ctx.registry` works unchanged),
    and state_for(platform_id) is how daemon/drivers.py's role-dispatch
    asks for the underlying object by name instead of reaching into the
    daemon's own ctx by attribute.

    `state_overrides` exists ONLY for tests that need to inject a
    pre-built/pre-seeded fake (e.g. a TiltScanner with hand-seeded
    _readings, or a real IPC connection pointed at a throwaway test
    socket) without touching real env vars or hardware -- production
    (DaemonContext) never passes it, so "the daemon doesn't instantiate
    these classes" holds regardless of what a given test does."""

    def __init__(self, *, clock: Clock, state_overrides: Mapping[str, Any] | None = None):
        overrides = state_overrides or {}
        self._state: dict[str, Any] = {}
        self._drivers: list[PlatformDriver] = []
        for platform_id in enabled_platform_ids():
            binding = PLATFORM_BINDINGS.get(platform_id)
            if binding is None:
                # unknown/not-yet-implemented platform id -- ignore rather
                # than crash the daemon (krauken lands in a later milestone)
                continue
            state_obj = overrides.get(platform_id)
            if state_obj is None:
                state_obj = binding.build_state(clock)
            self._state[platform_id] = state_obj
            self._drivers.append(binding.platform_cls(state_obj))

    def __iter__(self):
        return iter(self._drivers)

    def state_for(self, platform_id: str) -> Any | None:
        return self._state.get(platform_id)

    async def start_all(self) -> None:
        # Generic try/except here (not special-cased to any one platform,
        # unlike daemon/app.py's old Tilt-only try/except) -- every
        # platform's start() gets the same "a dependency that's down or
        # missing must never block or crash the rest" treatment.
        for platform_id, state_obj in self._state.items():
            starter = getattr(state_obj, "start", None)
            if starter is None:
                continue
            try:
                await starter()
            except PlatformUnavailable as e:
                log.warning("%s not started: %s", platform_id, e)

    async def stop_all(self) -> None:
        for platform_id, state_obj in self._state.items():
            stopper = getattr(state_obj, "stop", None)
            if stopper is None:
                continue
            try:
                await stopper()
            except Exception:  # noqa: BLE001 -- one platform's stop() must never block the rest
                log.exception("%s failed to stop cleanly", platform_id)
