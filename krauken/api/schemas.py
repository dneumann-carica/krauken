"""Pydantic response/request models -- the source of the OpenAPI document,
which in turn generates the frontend's TypeScript types (openapi-typescript).
Backend is the single source of truth for wire shapes. Minimal for M0;
grows substantially in M1/M2 per the project plan.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    api: Literal["ok"] = "ok"
    daemon: Literal["ok", "unavailable"]
    db_version: int
    uptime_s: float


class StageInput(BaseModel):
    """The authored half of a stage -- runtime columns (state/started_at/
    etc) aren't inputs, the control loop owns those.

    Freeform: no stage_type enum. `name` is the sole label (and the sole
    identity for a not-yet-created stage) -- there's no fixed set of named
    slots a plan has to fill, so a yeast preset's default_stages list (see
    YeastPreset below) can be any length/order/composition, including no
    diacetyl-rest-shaped entry at all for a strain that doesn't need one.
    """

    name: str = Field(min_length=1)
    temp_mode: Literal["constant", "stepped"]
    temp_f: float | None = None
    temp_from_f: float | None = None
    temp_to_f: float | None = None
    ramp_hours: float | None = None
    end_mode: Literal["time", "temp_hold", "gravity", "gravity_below"]
    end_hours: float | None = None
    hold_temp_f: float | None = None
    hold_hours: float | None = None
    gravity_hi: float | None = None
    gravity_stable_hours: float | None = None
    min_hours: float | None = None
    max_hours: float | None = None
    advance_mode: Literal["auto", "manual"] = "auto"


class YeastPreset(BaseModel):
    name: str
    # Dropdown-grouping label only (Ale/Lager/Belgian-Saison/Wheat-
    # Hefeweizen/Kveik/Custom) -- cosmetic, never branched on for stage
    # logic (see default_stages below for why). Optional so hand-authored
    # presets from before this field existed don't need a backfill.
    category: str | None = None
    # The new-fermentation dialog's starting plan for this strain, verbatim
    # -- literal StageInput templates, not flags for the UI to interpret
    # (e.g. a diacetyl-negative strain's list just has no diacetyl-rest-
    # shaped entry in it; nothing downstream branches on "why").
    default_stages: list[StageInput]


class YeastPresetsResponse(BaseModel):
    yeasts: dict[str, YeastPreset]


class RoleStatus(BaseModel):
    role: str
    required: bool
    filled: bool
    device_id: str | None = None
    device_name: str | None = None
    health: str | None = None


class StateResponse(BaseModel):
    setup_needed: bool
    roles: list[RoleStatus]
    can_start_fermentation: bool
    active_fermentation_id: int | None = None
    live: dict[str, Any]


class DeviceResponse(BaseModel):
    device_id: str
    platform: str
    name: str
    kind: str
    capabilities: list[str]
    is_bundle: bool
    health: str
    first_seen_at: str
    last_seen_at: str
    metadata: dict[str, Any]
    last_reading: dict[str, Any]


class TestStartRequest(BaseModel):
    action: Literal["fire_outlet", "identify_probes", "live_read"]
    params: dict[str, Any] = {}


class TestResponse(BaseModel):
    test_id: str
    kind: str
    device_id: str
    state: str
    started_at: str
    ends_at: str | None
    result: dict[str, Any] | None = None
    error: str | None = None


class ScanStartResponse(BaseModel):
    scan_id: str
    state: str


class ScanStatusResponse(BaseModel):
    scan_id: str
    state: str
    platform_status: dict[str, Any]
    devices: list[DeviceResponse]
    error: str | None = None


class MappingRole(BaseModel):
    device_id: str | None
    platform: str | None
    platform_config: dict[str, Any]


class MappingGetResponse(BaseModel):
    roles: dict[str, MappingRole]


class MappingSaveRequest(BaseModel):
    roles: dict[str, str | None]


class AutoResolvedNoticeResponse(BaseModel):
    device_id: str
    device_name: str
    roles_cleared: list[str]
    reason: str
    message: str


class MappingIssue(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class MappingSaveResponse(BaseModel):
    valid: bool
    roles: dict[str, str | None]
    auto_resolved: list[AutoResolvedNoticeResponse]
    blocking: list[MappingIssue]
    warnings: list[MappingIssue]


class FermentationSummary(BaseModel):
    id: int
    name: str
    profile_id: int
    status: Literal["active", "completed", "terminated"]
    started_at: str
    ended_at: str | None
    end_reason: str | None
    og: float | None
    fg: float | None
    abv_pct: float | None
    simulated: bool
    demo: bool
    yeast_id: str | None
    yeast_name: str | None


class StageResponse(BaseModel):
    id: int
    fermentation_id: int
    seq: int
    name: str
    temp_mode: str
    temp_f: float | None
    temp_from_f: float | None
    temp_to_f: float | None
    ramp_hours: float | None
    end_mode: str
    end_hours: float | None
    hold_temp_f: float | None
    hold_hours: float | None
    gravity_hi: float | None
    gravity_stable_hours: float | None
    min_hours: float | None
    max_hours: float | None
    advance_mode: str
    state: str
    started_at: str | None
    ended_at: str | None
    criteria_met_at: str | None
    end_actual_reason: str | None


class FermentationDetail(FermentationSummary):
    profile_name: str
    stages: list[StageResponse]


class AlertResponse(BaseModel):
    field: str
    since: str
    message: str


class FermentationStartRequest(BaseModel):
    name: str
    yeast_id: str | None = None
    yeast_name: str | None = None
    og: float | None = None
    stages: list[StageInput]


class FermentationStartResponse(BaseModel):
    fermentation_id: int
    profile_id: int
    stage_ids: list[int]


class AdvanceStageResponse(BaseModel):
    advanced: bool
    next_stage_id: int | None


class TerminateRequest(BaseModel):
    reason: str | None = None


class TerminateResponse(BaseModel):
    terminated: bool


class StopChamberResponse(BaseModel):
    stopped: bool


class ChamberStatusResponse(BaseModel):
    # Null whenever there's nothing to release: unmapped, or mapped but
    # currently holding no target. See daemon/hardware.py's chamber_status().
    commanded_target_f: float | None
    mapped: bool


class UpdateStagesRequest(BaseModel):
    stages: dict[str, dict[str, Any]]


class UpdateStagesResponse(BaseModel):
    updated_stage_ids: list[int]


class SetStageEnabledRequest(BaseModel):
    enabled: bool


class SetStageEnabledResponse(BaseModel):
    stage_id: int
    enabled: bool


class InsertStageRequest(BaseModel):
    # None appends at the end; otherwise the new stage lands immediately
    # after this existing stage (which must still be live -- see
    # daemon/fermentation.py's insert_stage).
    after_stage_id: int | None = None
    stage: StageInput


class InsertStageResponse(BaseModel):
    stage_id: int


class ReorderStagesRequest(BaseModel):
    # Every currently pending/skipped stage id, in the requested order --
    # a permutation of the full reorderable set, not a partial list.
    stage_ids: list[int]


class ReorderStagesResponse(BaseModel):
    stage_ids: list[int]


class DutyResponse(BaseModel):
    window_hours: float
    cool_pct: float
    heat_pct: float
    idle_pct: float


class SeriesGap(BaseModel):
    from_: str = Field(alias="from")
    to: str
    minutes: float

    model_config = {"populate_by_name": True}


class ProjectionResponse(BaseModel):
    ts: list[str]
    beer_temp_f: list[float | None]
    chamber_temp_f: list[float | None]
    gravity: list[float | None]
    effective_target_f: list[float | None]


class SeriesResponse(BaseModel):
    fermentation_id: int
    point_count: int
    ts: list[str]
    beer_temp_f: list[float | None]
    chamber_temp_f: list[float | None]
    gravity: list[float | None]
    effective_target_f: list[float | None]
    chamber_mode: list[str]
    beer_temp_ok: list[bool]
    target_source: list[str]
    projection: ProjectionResponse | None = None
    duty: DutyResponse
    gaps: list[SeriesGap]


class SettingResponse(BaseModel):
    key: str
    value: Any | None


class SettingSaveRequest(BaseModel):
    value: Any


class ManualReadingResponse(BaseModel):
    temp_f: float | None = None
    mode: str | None = None
    gravity_sg: float | None = None
    health: str
    cooling_on: bool | None = None
    heating_on: bool | None = None
    heating_enabled: bool | None = None
    probe2_enabled: bool | None = None
    probe2_temp_f: float | None = None
    available: bool | None = None
    # Chamber-only, read-only: the most recent temp_f the daemon's control
    # loop sent via ChamberDriver.set_target() (contracts/interfaces.py) --
    # not settable via ManualSetReadingRequest, only ever written by the
    # daemon itself (platforms/manual/live.py's ManualChamberDriver.set_target).
    # Always None for the tilt reading, which has no such concept.
    commanded_target_f: float | None = None


class ManualReadingsResponse(BaseModel):
    chamber: ManualReadingResponse
    tilt: ManualReadingResponse


class ManualSetReadingRequest(BaseModel):
    """Every field optional and independently settable -- exclude_unset at
    the router is what distinguishes "leave this alone" from "set this to
    null" (a real, meaningful dev-panel action: simulate a sensor that
    stopped reporting). Which fields actually apply depends on whether the
    URL targets chamber or tilt; daemon/ops/dev_panel.py rejects anything
    not valid for that target."""

    temp_f: float | None = None
    mode: Literal["idle", "cool", "heat"] | None = None
    gravity_sg: float | None = None
    health: Literal["ok", "degraded", "unreachable", "fault"] | None = None
    cooling_on: bool | None = None
    heating_on: bool | None = None
    heating_enabled: bool | None = None
    probe2_enabled: bool | None = None
    probe2_temp_f: float | None = None
    available: bool | None = None


class SimulatorReadingResponse(BaseModel):
    chamber_temp_f: float | None = None
    mode: str
    probe2_enabled: bool
    probe2_temp_f: float | None = None


class SimulatorSetProbe2Request(BaseModel):
    enabled: bool | None = None
    temp_f: float | None = None


class ClockResponse(BaseModel):
    now: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorDetail
