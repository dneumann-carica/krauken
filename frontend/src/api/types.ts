// Hand-written for M0. Once the API's OpenAPI schema is real (M1+), these
// get generated via openapi-typescript instead -- the backend is meant to
// be the single source of truth for wire shapes, not this file by hand.

export interface RoleStatus {
  role: string;
  required: boolean;
  filled: boolean;
  device_id: string | null;
  device_name: string | null;
  health: string | null;
}

export interface StateResponse {
  setup_needed: boolean;
  roles: RoleStatus[];
  can_start_fermentation: boolean;
  active_fermentation_id: number | null;
  live: {
    as_of: string;
    payload: Record<string, unknown>;
  };
}

export interface HealthResponse {
  api: "ok";
  daemon: "ok" | "unavailable";
  db_version: number;
  uptime_s: number;
}

export interface FermentationSummary {
  id: number;
  name: string;
  profile_id: number;
  status: "active" | "completed" | "terminated";
  started_at: string;
  ended_at: string | null;
  end_reason: string | null;
  og: number | null;
  fg: number | null;
  abv_pct: number | null;
  simulated: boolean;
  demo: boolean;
  yeast_name: string | null;
}

export interface StageResponse {
  id: number;
  fermentation_id: number;
  seq: number;
  stage_type: string;
  name: string;
  temp_mode: string;
  temp_f: number | null;
  temp_from_f: number | null;
  temp_to_f: number | null;
  ramp_hours: number | null;
  end_mode: string;
  end_hours: number | null;
  hold_temp_f: number | null;
  hold_hours: number | null;
  gravity_lo: number | null;
  gravity_hi: number | null;
  gravity_stable_hours: number | null;
  min_hours: number | null;
  max_hours: number | null;
  advance_mode: string;
  state: "pending" | "running" | "finished" | "skipped";
  started_at: string | null;
  ended_at: string | null;
  criteria_met_at: string | null;
  end_actual_reason: string | null;
}

export interface FermentationDetail extends FermentationSummary {
  profile_name: string;
  stages: StageResponse[];
}

export interface SeriesGap {
  from: string;
  to: string;
  minutes: number;
}

export interface DeviceResponse {
  device_id: string;
  platform: string;
  name: string;
  kind: string;
  capabilities: string[];
  is_bundle: boolean;
  health: string;
  first_seen_at: string;
  last_seen_at: string;
  metadata: Record<string, unknown>;
  last_reading: Record<string, unknown>;
}

export interface ScanStartResponse {
  scan_id: string;
  state: string;
}

export interface ScanStatusResponse {
  scan_id: string;
  state: "running" | "complete" | "failed" | string;
  platform_status: Record<string, unknown>;
  devices: DeviceResponse[];
  error: string | null;
}

export interface MappingRole {
  device_id: string | null;
  platform: string | null;
  platform_config: Record<string, unknown>;
}

export interface MappingGetResponse {
  roles: Record<string, MappingRole>;
}

export interface AutoResolvedNoticeResponse {
  device_id: string;
  device_name: string;
  roles_cleared: string[];
  reason: string;
  message: string;
}

export interface MappingIssue {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface MappingSaveResponse {
  valid: boolean;
  roles: Record<string, string | null>;
  auto_resolved: AutoResolvedNoticeResponse[];
  blocking: MappingIssue[];
  warnings: MappingIssue[];
}

export type TestAction = "fire_outlet" | "identify_probes" | "live_read";

export interface TestResponse {
  test_id: string;
  kind: string;
  device_id: string;
  state: "running" | "completed" | "cancelled" | "failed" | string;
  started_at: string;
  ends_at: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

export interface SettingResponse {
  key: string;
  value: unknown;
}

export interface ManualReading {
  temp_f: number | null;
  mode: string | null;
  gravity_sg: number | null;
  health: string;
  cooling_on: boolean | null;
  heating_on: boolean | null;
  heating_enabled: boolean | null;
  probe2_enabled: boolean | null;
  probe2_temp_f: number | null;
  available: boolean | null;
  // Chamber-only, read-only: the most recent temp_f the daemon's control
  // loop sent via ChamberDriver.set_target(). Always null on the tilt reading.
  commanded_target_f: number | null;
}

export interface ManualReadingsResponse {
  chamber: ManualReading;
  tilt: ManualReading;
}

export interface SimulatorReading {
  chamber_temp_f: number | null;
  mode: string;
  probe2_enabled: boolean;
  probe2_temp_f: number | null;
}

export interface ClockResponse {
  now: string;
}

export interface AlertResponse {
  field: string;
  since: string;
  message: string;
}

export interface ProjectionResponse {
  ts: string[];
  beer_temp_f: (number | null)[];
  chamber_temp_f: (number | null)[];
  gravity: (number | null)[];
  effective_target_f: (number | null)[];
}

export interface SeriesResponse {
  fermentation_id: number;
  point_count: number;
  ts: string[];
  beer_temp_f: (number | null)[];
  chamber_temp_f: (number | null)[];
  gravity: (number | null)[];
  effective_target_f: (number | null)[];
  chamber_mode: string[];
  beer_temp_ok: boolean[];
  target_source: string[];
  projection: ProjectionResponse | null;
  duty: { window_hours: number; cool_pct: number; heat_pct: number; idle_pct: number };
  gaps: SeriesGap[];
}

export interface StageInput {
  stage_type: "primary" | "free_rise" | "diacetyl_rest" | "conditioning" | "cold_crash";
  name: string;
  temp_mode: "constant" | "stepped";
  temp_f?: number | null;
  temp_from_f?: number | null;
  temp_to_f?: number | null;
  ramp_hours?: number | null;
  end_mode: "time" | "temp_hold" | "gravity";
  end_hours?: number | null;
  hold_temp_f?: number | null;
  hold_hours?: number | null;
  gravity_lo?: number | null;
  gravity_hi?: number | null;
  gravity_stable_hours?: number | null;
  min_hours?: number | null;
  max_hours?: number | null;
  advance_mode: "auto" | "manual";
}

export interface FermentationStartRequest {
  name: string;
  yeast_id?: string | null;
  yeast_name?: string | null;
  og?: number | null;
  stages: StageInput[];
}

export interface FermentationStartResponse {
  fermentation_id: number;
  profile_id: number;
  stage_ids: number[];
}

export interface AdvanceStageResponse {
  advanced: boolean;
  next_stage_id: number | null;
}

export interface TerminateResponse {
  terminated: boolean;
}

export interface UpdateStagesResponse {
  updated_stage_ids: number[];
}

export interface SetStageEnabledResponse {
  stage_id: number;
  enabled: boolean;
}

export interface YeastPreset {
  name: string;
  stage_defaults: Record<string, Record<string, number>>;
}

export interface YeastPresetsResponse {
  yeasts: Record<string, YeastPreset>;
}
