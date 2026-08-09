import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { FermentationStartRequest, TestAction } from "./types";

export function useAppState() {
  return useQuery({
    queryKey: ["state"],
    queryFn: api.getState,
    refetchInterval: 5000,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: api.getHealth,
    refetchInterval: 10000,
  });
}

// M1 charts are read-only historical views (no live mode yet, see plan
// M2) -- no refetchInterval needed here.
export function useFermentations(status?: string) {
  return useQuery({
    queryKey: ["fermentations", status ?? "all"],
    queryFn: () => api.getFermentations(status),
  });
}

export function useFermentation(id: number | undefined, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: ["fermentation", id],
    queryFn: () => api.getFermentation(id!),
    enabled: id !== undefined,
    refetchInterval: opts.live ? 10_000 : false,
  });
}

// M1's read-only historical charts had no live mode; M2 fermentations that
// are still `active` poll so the NOW marker/projection and duty caption
// stay current -- callers pass live:true only for an active fermentation.
export function useSeries(id: number | undefined, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: ["series", id],
    queryFn: () => api.getSeries(id!),
    enabled: id !== undefined,
    refetchInterval: opts.live ? 15_000 : false,
  });
}

export function useDevices() {
  return useQuery({ queryKey: ["hardware", "devices"], queryFn: api.getDevices });
}

export function useMapping() {
  return useQuery({ queryKey: ["hardware", "mapping"], queryFn: api.getMapping });
}

export function useStartScan() {
  return useMutation({ mutationFn: api.startScan });
}

// Polls while the scan is running, stops the instant it lands on a terminal
// state -- a fixed-interval poll that never turned itself off would keep
// hitting the daemon (and the SPA nothing on this page needs it for after
// that point).
export function useScanStatus(scanId: string | undefined) {
  return useQuery({
    queryKey: ["hardware", "scan", scanId],
    queryFn: () => api.getScanStatus(scanId!),
    enabled: scanId !== undefined,
    refetchInterval: (query) => (query.state.data?.state === "running" ? 700 : false),
  });
}

export function useSaveMapping() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (roles: Record<string, string | null>) => api.saveMapping(roles),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["hardware", "mapping"] });
      queryClient.invalidateQueries({ queryKey: ["state"] });
    },
  });
}

export function useStartTest() {
  return useMutation({
    mutationFn: ({ deviceId, action, params }: { deviceId: string; action: TestAction; params?: Record<string, unknown> }) =>
      api.startTest(deviceId, action, params),
  });
}

export function useTestStatus(deviceId: string | undefined, testId: string | undefined) {
  return useQuery({
    queryKey: ["hardware", "test", deviceId, testId],
    queryFn: () => api.getTestStatus(deviceId!, testId!),
    enabled: deviceId !== undefined && testId !== undefined,
    refetchInterval: (query) => (query.state.data?.state === "running" ? 400 : false),
  });
}

export function useCancelTest() {
  return useMutation({
    mutationFn: ({ deviceId, testId }: { deviceId: string; testId: string }) => api.cancelTest(deviceId, testId),
  });
}

export function useSetting(key: string) {
  return useQuery({ queryKey: ["settings", key], queryFn: () => api.getSetting(key) });
}

export function useSaveSetting() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => api.saveSetting(key, value),
    onSuccess: (_data, { key }) => {
      queryClient.invalidateQueries({ queryKey: ["settings", key] });
    },
  });
}

// Static, shipped-with-the-app data -- never refetch.
export function useYeasts() {
  return useQuery({ queryKey: ["yeasts"], queryFn: api.getYeasts, staleTime: Infinity });
}

export function useStartFermentation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: FermentationStartRequest) => api.startFermentation(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["state"] });
      queryClient.invalidateQueries({ queryKey: ["fermentations"] });
    },
  });
}

export function useAdvanceStage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (fermentationId: number) => api.advanceStage(fermentationId),
    onSuccess: (_data, fermentationId) => {
      queryClient.invalidateQueries({ queryKey: ["fermentation", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["series", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["state"] });
    },
  });
}

export function useTerminateFermentation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ fermentationId, reason }: { fermentationId: number; reason?: string }) =>
      api.terminateFermentation(fermentationId, reason),
    onSuccess: (_data, { fermentationId }) => {
      queryClient.invalidateQueries({ queryKey: ["fermentation", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["series", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["state"] });
      queryClient.invalidateQueries({ queryKey: ["fermentations"] });
    },
  });
}

export function useUpdateStages() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ fermentationId, stages }: { fermentationId: number; stages: Record<string, Record<string, unknown>> }) =>
      api.updateStages(fermentationId, stages),
    onSuccess: (_data, { fermentationId }) => {
      queryClient.invalidateQueries({ queryKey: ["fermentation", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["series", fermentationId] });
    },
  });
}

export function useSetStageEnabled() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ fermentationId, stageId, enabled }: { fermentationId: number; stageId: number; enabled: boolean }) =>
      api.setStageEnabled(fermentationId, stageId, enabled),
    onSuccess: (_data, { fermentationId }) => {
      queryClient.invalidateQueries({ queryKey: ["fermentation", fermentationId] });
      queryClient.invalidateQueries({ queryKey: ["series", fermentationId] });
    },
  });
}

// Polls only while live -- a completed/terminated batch's alert history is
// frozen (whatever was open at end is what it is), no need to keep asking.
export function useAlerts(fermentationId: number | undefined, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: ["alerts", fermentationId],
    queryFn: () => api.getAlerts(fermentationId!),
    enabled: fermentationId !== undefined,
    refetchInterval: opts.live ? 10_000 : false,
  });
}

// retry:false -- a 403 (dev panel disabled) is a real, meaningful answer,
// not a transient failure worth retrying.
export function useManualReadings() {
  return useQuery({ queryKey: ["dev-manual"], queryFn: api.getManualReadings, retry: false });
}

export function useSetManualReading() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ field, values }: { field: string; values: Record<string, unknown> }) =>
      api.setManualReading(field, values),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["dev-manual"] }),
  });
}

export function useSimulatorReadings() {
  return useQuery({ queryKey: ["dev-simulator"], queryFn: api.getSimulatorReadings, retry: false });
}

export function useSetSimulatorProbe2() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: { enabled?: boolean; temp_f?: number | null }) => api.setSimulatorProbe2(values),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["dev-simulator"] }),
  });
}

export function useClock() {
  return useQuery({ queryKey: ["dev-clock"], queryFn: api.getClock, retry: false, refetchInterval: 5000 });
}
