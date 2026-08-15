"""Which platforms are active in this process, and the one place that maps
a platform_id string to everything that platform_id means -- its discovery
driver (PlatformDriver, for scanning) and its live role drivers
(ChamberDriver/BeerTempSource/GravitySource, for daemon/drivers.py to hand
the control loop). Nothing in discovery/aggregation/the UI can tell a mock
platform from a real one. Krauken (the Hardware Supervisor platform) is
still a later milestone; Manual/Simulator/BrewPi/Tilt all exist today.

Both PLATFORM_BINDINGS' consumers (this module's build_registry() and
daemon/drivers.py's role-driver factories) read the same table instead of
each hand-maintaining their own if/elif chain of platform_id strings --
adding a platform later is one new PlatformBinding row, not four matching
branches spread across two files that can drift out of sync.

Manual and Simulator each run as their own out-of-process server
(platforms/manual/service.py, platforms/simulator/service.py) -- their
classes below are platforms/ipc_driver.py's generic IPC-backed
implementations, wrapping a PersistentIPCClient. BrewPi and Tilt, once it
came to actually building them, turned out NOT to need an IPC-backed
binding at all (an earlier docstring here speculated they would) -- both
are in-process (a persistent pyserial connection, a persistent raw-HCI BLE
listener), matching the design doc's own framing of BrewPi as "a thin
serial wrapper" and Tilt as "read directly...in the control daemon, no
separate bridge process" -- and it turns out PlatformBinding/
build_registry() never actually assumed IPC in the first place: `state_attr`
just names a ctx attribute, `cls(state_obj, device_id)` doesn't care what
kind of object state_obj is. So a real non-IPC platform binding looks
almost identical to an IPC one from this module's point of view -- a
different constructor argument type behind the same dispatch shape, not a
structural change."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from krauken.contracts.interfaces import BeerTempSource, ChamberDriver, GravitySource, PlatformDriver
from krauken.platforms.brewpi.connection import BrewPiConnection
from krauken.platforms.brewpi.live import BrewPiBeerTempSource, BrewPiChamberDriver
from krauken.platforms.brewpi.platform import BrewPiPlatform
from krauken.platforms.ipc_driver import IpcBeerTempSource, IpcChamberDriver, IpcGravitySource, IpcPlatformDriver
from krauken.platforms.tilt.live import TiltBeerTempSource, TiltGravitySource
from krauken.platforms.tilt.platform import TiltPlatform
from krauken.platforms.tilt.scanner import TiltScanner

if TYPE_CHECKING:
    from krauken.ipc.persistent_client import PersistentIPCClient


class _ManualIpcPlatform(IpcPlatformDriver):
    platform_id = "manual"
    display_name = "Manual (dev panel)"


class _SimulatorIpcPlatform(IpcPlatformDriver):
    platform_id = "simulator"
    display_name = "Simulator"


@dataclass(frozen=True)
class PlatformBinding:
    """Everything the rest of the daemon needs to know about one platform_id.

    `state_attr` is the daemon ctx attribute (and build_registry() keyword)
    holding that platform's one shared connection/client/scanner object.
    `platform_cls` (discover()-only, no particular device in mind yet) is
    always just `cls(state_obj)`. The three role-driver classes are
    `cls(state_obj, device_id)` -- device_id lets a platform with more than
    one live candidate (Tilt, today) pick the specific one that was
    actually mapped; everything else ignores it (see daemon/drivers.py's
    own docstring). Either way, dispatch needs no per-platform
    special-casing."""

    state_attr: str
    platform_cls: type[PlatformDriver]
    chamber_driver_cls: type[ChamberDriver] | None = None
    beer_temp_source_cls: type[BeerTempSource] | None = None
    gravity_source_cls: type[GravitySource] | None = None


PLATFORM_BINDINGS: dict[str, PlatformBinding] = {
    "manual": PlatformBinding(
        state_attr="manual_client",
        platform_cls=_ManualIpcPlatform,
        chamber_driver_cls=IpcChamberDriver,
        beer_temp_source_cls=IpcBeerTempSource,
        gravity_source_cls=IpcGravitySource,
    ),
    "simulator": PlatformBinding(
        state_attr="simulator_client",
        platform_cls=_SimulatorIpcPlatform,
        chamber_driver_cls=IpcChamberDriver,
        beer_temp_source_cls=IpcBeerTempSource,
        gravity_source_cls=IpcGravitySource,
    ),
    "brewpi": PlatformBinding(
        state_attr="brewpi_connection",
        platform_cls=BrewPiPlatform,
        chamber_driver_cls=BrewPiChamberDriver,
        beer_temp_source_cls=BrewPiBeerTempSource,
        # No GravitySource -- BrewPi has no hydrometer of its own.
        gravity_source_cls=None,
    ),
    "tilt": PlatformBinding(
        state_attr="tilt_scanner",
        platform_cls=TiltPlatform,
        # No ChamberDriver -- Tilt only ever fills Beer Temp/Beer Gravity.
        chamber_driver_cls=None,
        beer_temp_source_cls=TiltBeerTempSource,
        gravity_source_cls=TiltGravitySource,
    ),
    # krauken (the GPIO Hardware Supervisor platform) lands in a later
    # milestone -- an unknown platform_id simply has no binding, which
    # every lookup below already treats as "ignore" (build_registry) or
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


def build_registry(
    *,
    manual_client: "PersistentIPCClient | None" = None,
    simulator_client: "PersistentIPCClient | None" = None,
    brewpi_connection: "BrewPiConnection | None" = None,
    tilt_scanner: "TiltScanner | None" = None,
) -> list[PlatformDriver]:
    """One PlatformDriver per platform_id that both (a) is enabled
    (enabled_platform_ids()) and (b) was actually handed a state object
    here -- a platform with no state object is skipped entirely rather
    than constructed against None, since none of these drivers have a
    "static shape, no live connection" mode: every field of a
    DeviceCandidate genuinely comes from the far end (IPC) or the live
    hardware (BrewPi/Tilt), there's nothing left to fall back to on this
    side. Callers that only want the registry's SHAPE (which platform_ids
    exist, not their live discover() results) can still omit all four and
    get however many bindings-with-no-state-supplied that implies --
    today, zero."""
    state_by_attr: dict[str, Any] = {
        "manual_client": manual_client,
        "simulator_client": simulator_client,
        "brewpi_connection": brewpi_connection,
        "tilt_scanner": tilt_scanner,
    }
    drivers: list[PlatformDriver] = []
    for platform_id in enabled_platform_ids():
        binding = PLATFORM_BINDINGS.get(platform_id)
        if binding is None:
            # unknown/not-yet-implemented platform id -- ignore rather than
            # crash the daemon (krauken/brewpi/tilt land in later milestones)
            continue
        client = state_by_attr.get(binding.state_attr)
        if client is None:
            continue
        drivers.append(binding.platform_cls(client))
    return drivers
