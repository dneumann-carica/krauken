"""Core value types shared by the daemon, supervisor, and API tier.

These are the wire/domain shapes -- no I/O, no hardware deps, importable
anywhere (including on a dev Mac with no GPIO).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class Health(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    FAULT = "fault"


class ChamberMode(StrEnum):
    COOL = "cool"
    HEAT = "heat"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class ChamberReading:
    temp_f: float | None
    mode: ChamberMode
    health: Health
    last_good_ts: float | None
    commanded_target_f: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BeerReading:
    temp_f: float | None
    health: Health
    last_good_ts: float | None


@dataclass(frozen=True, slots=True)
class GravityReading:
    gravity_sg: float | None
    health: Health
    last_good_ts: float | None


@dataclass(frozen=True, slots=True)
class DeviceCandidate:
    """What a platform driver's discover() returns, and what a Hardware Setup
    scan aggregates across every registered platform (real or mock)."""

    device_id: str
    platform: str
    display_name: str
    kind_label: str
    capabilities: frozenset[str]  # Role values this device can fill
    bundled_roles: frozenset[str]  # non-empty -> all-or-nothing chamber bundle
    health: Health
    health_note: str = ""
    detail_line: str = ""
    reading_summary: str | None = None
    readings: Mapping[str, float | None] = field(default_factory=dict)
    identity: Mapping[str, Any] = field(default_factory=dict)
    last_seen_ts: float | None = None
    requires_setup: bool = False
    available_tests: tuple[str, ...] = ()
    simulated: bool = False
    platform_config: Mapping[str, Any] = field(default_factory=dict)
