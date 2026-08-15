"""Resolves a mapped role to a live driver instance. Dispatches through
platforms/registry.py's PLATFORM_BINDINGS table -- the one place a
platform_id string is mapped to its classes -- rather than hand-maintaining
a second if/elif chain here that would need to stay in sync with the
registry's own. Krauken (the GPIO Hardware Supervisor platform) isn't
implemented yet -- a role mapped to it currently has no binding, same as
an unmapped role, so this module returns None for both. The control loop
treats "no driver" and "driver present but reading unhealthy" as distinct
cases (see contracts/failsafe.py); this module only ever returns None for
the former.

device_id is threaded through every dispatch function and forwarded as
each driver class's second constructor argument -- most platforms ignore
it entirely (Manual/Simulator/BrewPi only ever have one conceptual device
per role anyway), but Tilt needs it: discover() can surface several
detected Tilt colors as independent candidates, and without device_id a
driver instance would have no way to know which mapped color it's
actually supposed to be reading (see platforms/tilt/live.py's own
docstring on this -- a real limitation this exact change closes)."""
from __future__ import annotations

from typing import Any

from krauken.contracts.interfaces import BeerTempSource, ChamberDriver, GravitySource
from krauken.platforms.ipc_driver import IpcPlatformConnection, sync_clock
from krauken.platforms.registry import PLATFORM_BINDINGS


def chamber_driver(ctx: Any, platform: str | None, device_id: str | None = None) -> ChamberDriver | None:
    binding = PLATFORM_BINDINGS.get(platform)
    if binding is None or binding.chamber_driver_cls is None:
        return None
    state_obj = ctx.registry.state_for(platform)
    if state_obj is None:
        return None
    return binding.chamber_driver_cls(state_obj, device_id)


def beer_temp_source(ctx: Any, platform: str | None, device_id: str | None = None) -> BeerTempSource | None:
    binding = PLATFORM_BINDINGS.get(platform)
    if binding is None or binding.beer_temp_source_cls is None:
        return None
    state_obj = ctx.registry.state_for(platform)
    if state_obj is None:
        return None
    return binding.beer_temp_source_cls(state_obj, device_id)


def gravity_source(ctx: Any, platform: str | None, device_id: str | None = None) -> GravitySource | None:
    binding = PLATFORM_BINDINGS.get(platform)
    if binding is None or binding.gravity_source_cls is None:
        return None
    state_obj = ctx.registry.state_for(platform)
    if state_obj is None:
        return None
    return binding.gravity_source_cls(state_obj, device_id)


async def sync_remote_clocks(ctx: Any) -> None:
    """Tells every IPC-backed platform's RemoteClock what time the daemon
    is currently using (contracts/clock.py's RemoteClock docstring) --
    called once per control tick, regardless of which platform (if any) is
    actually mapped to a role right now, same "cheap and idempotent, called
    unconditionally" idiom as ChamberDriver.set_ambient_location. Iterates
    PLATFORM_BINDINGS rather than naming "simulator"/"manual" specifically,
    so a future real Supervisor binding picks this up automatically the
    moment its own state object is an IpcPlatformConnection -- nothing here
    needs to change when that lands. Asks ctx.registry for each platform's
    state object by name (state_for()), not ctx itself -- the daemon
    doesn't hold these under named attributes any more."""
    now = ctx.clock.now()
    monotonic = ctx.clock.monotonic()
    for platform_id in PLATFORM_BINDINGS:
        state_obj = ctx.registry.state_for(platform_id)
        if isinstance(state_obj, IpcPlatformConnection):
            await sync_clock(state_obj, now=now, monotonic=monotonic)
