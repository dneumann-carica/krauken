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
