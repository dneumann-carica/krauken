import type {
  AdvanceStageResponse,
  AlertResponse,
  DeviceResponse,
  FermentationDetail,
  FermentationStartRequest,
  FermentationStartResponse,
  FermentationSummary,
  HealthResponse,
  ClockResponse,
  ManualReading,
  ManualReadingsResponse,
  MappingGetResponse,
  MappingSaveResponse,
  ScanStartResponse,
  ScanStatusResponse,
  SeriesResponse,
  SetStageEnabledResponse,
  SettingResponse,
  SimulatorReading,
  StateResponse,
  TerminateResponse,
  TestAction,
  TestResponse,
  UpdateStagesResponse,
  YeastPresetsResponse,
} from "./types";

class ApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  status: number;

  constructor(status: number, code: string, message: string, details: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      // Light anti-CSRF measure (per project plan): a zero-auth LAN device
      // is otherwise fetchable by any hostile webpage a browser on that LAN
      // visits. This custom header can't be set by a simple/no-preflight
      // cross-origin form or fetch, so it blocks that class of attack with
      // no login required.
      "X-Krauken-Client": "1",
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = body?.error ?? {};
    throw new ApiError(res.status, err.code ?? "unknown_error", err.message ?? res.statusText, err.details ?? {});
  }
  return res.json() as Promise<T>;
}

export const api = {
  getHealth: () => request<HealthResponse>("/health"),
  getState: () => request<StateResponse>("/state"),
  getFermentations: (status?: string) =>
    request<FermentationSummary[]>(`/fermentations${status ? `?status=${status}` : ""}`),
  getFermentation: (id: number) => request<FermentationDetail>(`/fermentations/${id}`),
  getSeries: (id: number) => request<SeriesResponse>(`/fermentations/${id}/series`),

  startScan: () => request<ScanStartResponse>("/hardware/scan", { method: "POST" }),
  getScanStatus: (scanId: string) => request<ScanStatusResponse>(`/hardware/scan/${scanId}`),
  getDevices: () => request<DeviceResponse[]>("/hardware/devices"),
  getMapping: () => request<MappingGetResponse>("/hardware/mapping"),
  saveMapping: (roles: Record<string, string | null>) =>
    request<MappingSaveResponse>("/hardware/mapping", { method: "PUT", body: JSON.stringify({ roles }) }),
  startTest: (deviceId: string, action: TestAction, params: Record<string, unknown> = {}) =>
    request<TestResponse>(`/hardware/devices/${deviceId}/test`, {
      method: "POST",
      body: JSON.stringify({ action, params }),
    }),
  getTestStatus: (deviceId: string, testId: string) =>
    request<TestResponse>(`/hardware/devices/${deviceId}/test/${testId}`),
  cancelTest: (deviceId: string, testId: string) =>
    request<TestResponse>(`/hardware/devices/${deviceId}/test/${testId}/cancel`, { method: "POST" }),

  getSetting: (key: string) => request<SettingResponse>(`/settings/${key}`),
  saveSetting: (key: string, value: unknown) =>
    request<SettingResponse>(`/settings/${key}`, { method: "PUT", body: JSON.stringify({ value }) }),

  getYeasts: () => request<YeastPresetsResponse>("/yeasts"),
  startFermentation: (body: FermentationStartRequest) =>
    request<FermentationStartResponse>("/fermentations", { method: "POST", body: JSON.stringify(body) }),
  advanceStage: (fermentationId: number) =>
    request<AdvanceStageResponse>(`/fermentations/${fermentationId}/advance`, { method: "POST" }),
  terminateFermentation: (fermentationId: number, reason?: string) =>
    request<TerminateResponse>(`/fermentations/${fermentationId}/terminate`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  updateStages: (fermentationId: number, stages: Record<string, Record<string, unknown>>) =>
    request<UpdateStagesResponse>(`/fermentations/${fermentationId}/stages`, {
      method: "PUT",
      body: JSON.stringify({ stages }),
    }),
  setStageEnabled: (fermentationId: number, stageId: number, enabled: boolean) =>
    request<SetStageEnabledResponse>(`/fermentations/${fermentationId}/stages/${stageId}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  getAlerts: (fermentationId: number) => request<AlertResponse[]>(`/fermentations/${fermentationId}/alerts`),

  getManualReadings: () => request<ManualReadingsResponse>("/dev/manual"),
  setManualReading: (field: string, values: Record<string, unknown>) =>
    request<ManualReading>(`/dev/manual/${field}`, { method: "PUT", body: JSON.stringify(values) }),

  getSimulatorReadings: () => request<SimulatorReading>("/dev/simulator"),
  setSimulatorProbe2: (values: { enabled?: boolean; temp_f?: number | null }) =>
    request<SimulatorReading>("/dev/simulator/probe2", { method: "PUT", body: JSON.stringify(values) }),

  getClock: () => request<ClockResponse>("/dev/clock"),
};

export { ApiError };
