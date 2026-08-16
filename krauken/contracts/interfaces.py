"""The driver interfaces every hardware platform implements against. These
are the seam between the daemon's control logic and hardware -- BrewPi
(integrated, serial) and Krauken (composed, via the Hardware Supervisor)
both resolve to the same ChamberDriver shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from krauken.contracts.models import BeerReading, ChamberReading, DeviceCandidate, GravityReading


@runtime_checkable
class ChamberDriver(Protocol):
    async def read_chamber(self) -> ChamberReading: ...
    async def set_target(self, temp_f: float | None) -> None:
        """None = idle (no target -- chamber controller de-energizes)."""
        ...
    async def commanded_target(self) -> float | None:
        """Whatever the last set_target() call commanded, right now --
        None if nothing has ever been commanded, or the most recent call
        was set_target(None). A pure peek: unlike read_chamber(), this
        never advances any physics/clock state, so it's safe to call from
        an on-demand status check (e.g. "is the chamber still holding a
        setpoint with no fermentation running") without the timing
        concerns read_chamber()'s own docstring warns about."""
        ...
    async def set_ambient_location(self, location: str | None) -> None:
        """The room/enclosure the chamber sits in (e.g. "Garage",
        "Basement") -- purely a hint for drivers that model (or display)
        an ambient-temperature effect; a no-op for any driver with no such
        concept (real hardware has no simulated ambient to hint at). Called
        every control tick regardless of which platform is actually
        mapped, cheaply and idempotently, so the control loop itself never
        needs to know which platform cares."""
        ...
    async def probe_temps(self) -> dict[str, float | None]:
        """Live per-probe temps, keyed by whatever probe-address strings
        this platform's discover() advertised in the device's
        DeviceCandidate.identity["probe_addresses"] -- the one place the
        guided wizard's identify_probes test (daemon/tests_runtime.py)
        needs live per-probe state instead of read_chamber()'s single
        reading. A one-probe rig returns a single-entry dict keyed by that
        probe's own address; a driver with no multi-probe concept at all
        may return an empty dict."""
        ...


@runtime_checkable
class BeerTempSource(Protocol):
    async def read(self) -> BeerReading: ...


@runtime_checkable
class GravitySource(Protocol):
    async def read(self) -> GravityReading: ...


@runtime_checkable
class PlatformDriver(Protocol):
    """A hardware platform (Krauken, BrewPi, Tilt, Manual, Simulator). Every
    driver -- real or mock -- implements discover() so a single scan can
    fan out across all of them uniformly."""

    platform_id: str
    display_name: str

    async def discover(self, ctx: Mapping[str, Any]) -> Sequence[DeviceCandidate]: ...


@dataclass(frozen=True)
class PlatformTestRunner:
    """One entry in a platforms/registry.py PlatformBinding.test_runners
    table -- a platform-owned hardware-SETUP test action that doesn't fit
    the three role-driver Protocols above (device CONFIGURATION, not
    device operation). Lives here rather than in registry.py itself so
    that a platform's own test-runner module (e.g.
    platforms/brewpi/device_config.py) can import this type without a
    circular import: registry.py already imports device_config.py to wire
    up PLATFORM_BINDINGS, so device_config.py importing anything back from
    registry.py would cycle; this module (contracts/interfaces.py) is
    beneath both.

    `run` is the async job body (ctx, job, device_id, params) -> None,
    same shape every runner already has. `requires_no_active_fermentation`
    is a declared flag, not something daemon/tests_runtime.py inspects the
    action name to decide -- start_test() checks it SYNCHRONOUSLY, before
    ever creating the task, the same way confirm_heater's own inline
    check already works (an async check buried inside the task body would
    only ever surface as an unhandled exception in a Task, never as
    something the caller of start_test() can catch). False by default:
    most platform-owned actions genuinely do need this (they mutate real
    hardware config), but a deliberate safety-net action (BrewPi's
    reset_brewpi) must NOT be gated by it, since gating would defeat
    exactly the case it exists for."""

    run: Callable[[Any, Any, str, dict], Any]
    requires_no_active_fermentation: bool = False
