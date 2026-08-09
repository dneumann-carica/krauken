"""The 5-role hardware model, per krauken-hardware-role-mapping.md.

Chamber Temp/Cooling/Heating bundle into exactly one controller (BrewPi or
Krauken) because compressor protection must never be split from its own
switching -- this is the ONLY reason the bundle exists, not a general
"same physical device" rule (Tilt fills Beer Temp and/or Beer Gravity
independently and is never bundled).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from krauken.contracts.models import DeviceCandidate


class Role(StrEnum):
    CHAMBER_TEMP = "chamber_temp"
    CHAMBER_COOLING = "chamber_cooling"
    CHAMBER_HEATING = "chamber_heating"
    BEER_TEMP = "beer_temp"
    BEER_GRAVITY = "beer_gravity"


CHAMBER_BUNDLE: frozenset[Role] = frozenset(
    {Role.CHAMBER_TEMP, Role.CHAMBER_COOLING, Role.CHAMBER_HEATING}
)

REQUIRED_ROLES: frozenset[Role] = frozenset(
    {Role.CHAMBER_TEMP, Role.CHAMBER_COOLING, Role.BEER_TEMP}
)

ALL_ROLES: tuple[Role, ...] = (
    Role.CHAMBER_TEMP,
    Role.CHAMBER_COOLING,
    Role.CHAMBER_HEATING,
    Role.BEER_TEMP,
    Role.BEER_GRAVITY,
)


def probe_display_index(all_addresses: list[str], addr: str) -> int:
    """README's rule: 1-Wire ROM address is the stored identity; the
    displayed "Probe N" number is derived by sorting addresses ascending and
    numbering from 1. Accepted to re-sort if a probe is physically swapped --
    callers must never persist this index, only the address."""
    ordered = sorted(all_addresses)
    return ordered.index(addr) + 1


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AutoResolvedNotice:
    device_id: str
    device_name: str
    roles_cleared: tuple[Role, ...]
    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class Resolution:
    roles: Mapping[Role, str | None]
    auto_resolved: tuple[AutoResolvedNotice, ...]
    blocking: tuple[Issue, ...]
    warnings: tuple[Issue, ...]

    @property
    def valid(self) -> bool:
        return not self.blocking


def _is_bundle_capable(device_id: str, devices: Mapping[str, DeviceCandidate]) -> bool:
    dev = devices.get(device_id)
    return dev is not None and bool(dev.bundled_roles)


def resolve(
    draft: Mapping[Role, str | None], devices: Mapping[str, DeviceCandidate]
) -> Resolution:
    """The auto-resolve algorithm (project decision: automatic, not the
    original spec's manual revert-or-remove flow). One rule replaces the
    written spec's separate "propagate" and "resolve conflict" steps:
    whichever device sits in chamber_temp -- or, if that's unfilled,
    whichever sibling role's device is bundle-capable -- becomes the
    chamber "owner" and claims all three chamber roles. Anything else
    (another bundle device, or an independent single-role device like a
    smart plug) that was pinned to one of those roles gets cleared with a
    notice. This one rule handles both the "two controllers disagree" case
    and the "a bundle device collides with an independent single-role
    device" case uniformly, and leaves a genuinely all-independent
    chamber setup (no bundle device involved in any of the three roles)
    completely untouched, since no owner is ever chosen in that case.
    """
    roles: dict[Role, str | None] = dict(draft)
    blocking: list[Issue] = []

    for role, device_id in list(roles.items()):
        if device_id is not None and device_id not in devices:
            blocking.append(
                Issue("unknown_device", f"{device_id} is not a known device", {"role": role.value})
            )
            roles[role] = None

    owner: str | None = None
    temp_device = roles.get(Role.CHAMBER_TEMP)
    if temp_device is not None and _is_bundle_capable(temp_device, devices):
        owner = temp_device
    else:
        for role in (Role.CHAMBER_COOLING, Role.CHAMBER_HEATING):
            candidate = roles.get(role)
            if candidate is not None and _is_bundle_capable(candidate, devices):
                owner = candidate
                break

    cleared_by_device: dict[str, list[Role]] = {}
    if owner is not None:
        for role in CHAMBER_BUNDLE:
            current = roles.get(role)
            if current is not None and current != owner:
                cleared_by_device.setdefault(current, []).append(role)
                roles[role] = None
            if roles.get(role) is None:
                roles[role] = owner

    notices = tuple(
        AutoResolvedNotice(
            device_id=loser,
            device_name=devices[loser].display_name if loser in devices else loser,
            roles_cleared=tuple(cleared),
            reason="chamber_bundle_moved",
            message=(
                f"Chamber roles were cleared off "
                f"{devices[loser].display_name if loser in devices else loser}. "
                "A controller owns the whole chamber -- sensing and switching -- or none of it."
            ),
        )
        for loser, cleared in cleared_by_device.items()
    )

    for role in REQUIRED_ROLES:
        if roles.get(role) is None:
            blocking.append(Issue("hardware_incomplete", f"{role.value} is required and unfilled", {"role": role.value}))

    for role, device_id in roles.items():
        if device_id is None:
            continue
        dev = devices.get(device_id)
        if dev is not None and role not in dev.capabilities:
            blocking.append(
                Issue(
                    "unqualified_assignment",
                    f"{dev.display_name} cannot fill {role.value}",
                    {"role": role.value, "device_id": device_id},
                )
            )

    warnings = tuple(
        Issue(
            f"{role.value}_unfilled",
            f"{role.value.replace('_', ' ')} is unfilled (optional)",
            {"role": role.value},
        )
        for role in (Role.CHAMBER_HEATING, Role.BEER_GRAVITY)
        if roles.get(role) is None
    )

    return Resolution(roles=roles, auto_resolved=notices, blocking=tuple(blocking), warnings=warnings)
