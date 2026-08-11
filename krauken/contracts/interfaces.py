"""The driver interfaces every hardware platform implements against. These
are the seam between the daemon's control logic and hardware -- BrewPi
(integrated, serial) and Krauken (composed, via the Hardware Supervisor)
both resolve to the same ChamberDriver shape.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

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
