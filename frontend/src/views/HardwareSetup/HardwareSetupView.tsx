import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Button, Tag } from "../../design/primitives";
import {
  apiErrorMessage,
  useAppState,
  useDevices,
  useFermentations,
  useMapping,
  useSaveMapping,
  useScanStatus,
  useStartScan,
} from "../../api/queries";
import type { DeviceResponse } from "../../api/types";
import { ALL_ROLES, CHAMBER_BUNDLE, REQUIRED_ROLES, Role, resolve } from "../../hardware/resolve";
import type { DeviceInfo } from "../../hardware/resolve";
import { ROLE_LABELS } from "../../hardware/roleLabels";
import { HardwareWizard } from "./HardwareWizard";
import styles from "./HardwareSetupView.module.css";

type Draft = Record<Role, string | null>;

const EMPTY_DRAFT: Draft = {
  chamber_temp: null,
  chamber_cooling: null,
  chamber_heating: null,
  beer_temp: null,
  beer_gravity: null,
};

function toDeviceInfo(d: DeviceResponse): DeviceInfo {
  return {
    displayName: d.name,
    capabilities: new Set(d.capabilities as Role[]),
    // The persisted row only keeps a bool (is_bundle) -- per the role-mapping
    // spec, bundling only ever applies to the fixed 3-role chamber set (no
    // partial-bundle case exists), so CHAMBER_BUNDLE is always the right
    // value whenever is_bundle is true.
    bundledRoles: d.is_bundle ? CHAMBER_BUNDLE : new Set(),
  };
}

function readingFor(role: Role, device: DeviceResponse | undefined): string {
  if (!device) return "";
  if (device.health !== "ok") return "—";
  // Matches the reference design, which shows this as a fixed placeholder
  // rather than live outlet telemetry -- there's no per-outlet on/off signal
  // in the API today.
  if (role === Role.CHAMBER_COOLING || role === Role.CHAMBER_HEATING) return "outlet off";
  if (role === Role.BEER_GRAVITY) {
    const g = device.last_reading.gravity_sg;
    return typeof g === "number" ? g.toFixed(3) : "—";
  }
  const t = device.last_reading.temp_f;
  return typeof t === "number" ? `${t.toFixed(1)}°F` : "—";
}

export function HardwareSetupView() {
  const queryClient = useQueryClient();
  const devices = useDevices();
  const mapping = useMapping();
  const appState = useAppState();
  const fermentations = useFermentations();
  const startScan = useStartScan();
  const saveMapping = useSaveMapping();

  const [scanId, setScanId] = useState<string>();
  // Confirmed live 2026-08-22: if the scan can't even START (the daemon
  // was unreachable -- a real, real-hardware-observed case, not
  // hypothetical), the mutation had no onError handler at all, so scanId
  // just stayed undefined forever and the header silently sat on
  // "Scanning for devices..." indefinitely, with no sign anything had
  // gone wrong. This is a DIFFERENT case from a scan that completes with
  // zero devices found (that already has its own "No devices found"
  // empty-state row below) -- this is "the scan never got to run at
  // all," and needs its own explicit banner.
  const [scanStartError, setScanStartError] = useState<string>();
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [seeded, setSeeded] = useState(false);
  const [wizardDeviceId, setWizardDeviceId] = useState<string>();
  const [saveResult, setSaveResult] = useState<Awaited<ReturnType<typeof saveMapping.mutateAsync>>>();
  const [saveError, setSaveError] = useState<string>();


  // Shared by the mount effect, the "Scan again" button, and
  // handleWizardFinish below -- a real scan job, not just invalidating
  // the "hardware","devices" query. That query reads whatever the last
  // completed scan job already wrote to the devices table; invalidating
  // it alone just refetches that same, unchanged, possibly-stale row.
  // Confirmed live this session: finishing the BrewPi wizard correctly
  // saved a new beer_temp role assignment, but the "roles it plays" chips
  // still came from a scan that predated the wizard entirely -- the
  // second OneWire probe it just installed was invisible to
  // capabilities/probe_addresses until the next scan, whenever that
  // happened to be (page reload, or a manual "Scan again" click).
  function triggerScan() {
    setScanStartError(undefined);
    startScan.mutate(undefined, {
      onSuccess: (r) => setScanId(r.scan_id),
      onError: (e) => setScanStartError(apiErrorMessage(e, "Could not start a hardware scan.")),
    });
  }

  const scanStartedRef = useRef(false);
  useEffect(() => {
    if (scanStartedRef.current) return;
    scanStartedRef.current = true;
    triggerScan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const scanStatus = useScanStatus(scanId);
  const scanState = scanStatus.data?.state;
  useEffect(() => {
    if (scanState === "complete") {
      queryClient.invalidateQueries({ queryKey: ["hardware", "devices"] });
    }
  }, [scanState, queryClient]);

  const seedFromMapping = useCallback(() => {
    if (!mapping.data) return;
    const next = { ...EMPTY_DRAFT };
    for (const role of ALL_ROLES) {
      next[role] = mapping.data.roles[role]?.device_id ?? null;
    }
    setDraft(next);
  }, [mapping.data]);

  useEffect(() => {
    if (!seeded && mapping.data) {
      seedFromMapping();
      setSeeded(true);
    }
  }, [seeded, mapping.data, seedFromMapping]);

  const deviceList = useMemo(() => devices.data ?? [], [devices.data]);
  const deviceById = useMemo(() => {
    const map: Record<string, DeviceResponse> = {};
    for (const d of deviceList) map[d.device_id] = d;
    return map;
  }, [deviceList]);
  const deviceInfoMap = useMemo(() => {
    const map: Record<string, DeviceInfo> = {};
    for (const d of deviceList) map[d.device_id] = toDeviceInfo(d);
    return map;
  }, [deviceList]);

  const preview = useMemo(() => resolve(draft, deviceInfoMap), [draft, deviceInfoMap]);

  function assign(role: Role, deviceId: string | null) {
    setDraft((prev) => ({ ...prev, [role]: deviceId }));
    setSaveResult(undefined);
    setSaveError(undefined);
  }

  function rolesOfDevice(deviceId: string): Role[] {
    return ALL_ROLES.filter((r) => preview.roles[r] === deviceId);
  }

  function applySaveResult(result: NonNullable<typeof saveResult>) {
    setSaveResult(result);
    setSaveError(undefined);
    const next = { ...EMPTY_DRAFT };
    for (const role of ALL_ROLES) next[role] = result.roles[role] ?? null;
    setDraft(next);
  }

  async function handleSave() {
    setSaveError(undefined);
    try {
      applySaveResult(await saveMapping.mutateAsync(draft));
    } catch {
      setSaveError("Save failed -- the mapping wasn't updated. Check the API connection and try again.");
    }
  }

  function handleReset() {
    seedFromMapping();
    setSaveResult(undefined);
    setSaveError(undefined);
  }

  // The wizard's own save already returns the authoritative resolved
  // roles -- applying that result directly (rather than invalidating the
  // mapping query and waiting for a refetch to land) sidesteps a real race
  // that existed here: invalidateQueries only starts a background refetch,
  // and setting a "please re-seed" flag before that refetch resolved raced
  // it, leaving the checklist showing "Unmapped" after a wizard save that
  // had actually succeeded server-side.
  function handleWizardFinish(result: NonNullable<typeof saveResult>) {
    setWizardDeviceId(undefined);
    applySaveResult(result);
    queryClient.invalidateQueries({ queryKey: ["hardware", "mapping"] });
    queryClient.invalidateQueries({ queryKey: ["state"] });
    // Not just invalidateQueries(["hardware","devices"]) -- that query
    // reads whatever the last scan job already wrote, which predates
    // whatever the wizard just changed on real hardware. A real rescan
    // (same as the mount effect / "Scan again") is what actually picks
    // up the new probe/relay this run just installed; its own
    // scanState==="complete" effect below invalidates ["hardware",
    // "devices"] once that fresh scan actually lands.
    triggerScan();
  }

  if (mapping.isLoading || devices.isLoading) {
    return <p className={styles.loading}>Loading…</p>;
  }
  if (mapping.isError || devices.isError) {
    return <p className={styles.error}>Could not reach the API.</p>;
  }

  const wizardDevice = wizardDeviceId ? deviceById[wizardDeviceId] : undefined;

  const filledRequired = [...REQUIRED_ROLES].filter((r) => preview.roles[r] != null).length;
  const scanLabel = scanStartError
    ? "Scan couldn't start"
    : scanState === "running" || scanState === undefined
      ? "Scanning for devices…"
      : scanState === "complete"
        ? `${deviceList.length} device${deviceList.length === 1 ? "" : "s"} found`
        : "Scan failed -- try again";
  const scanning = !scanStartError && (scanState === "running" || scanState === undefined);

  const saved = saveResult != null && !saveError;
  const saveDisabled = !preview.valid || saveMapping.isPending || saved;
  const saveNote = saveError
    ? saveError
    : !preview.valid
      ? "Blocked -- fill in the required roles below before saving."
      : saveResult
        ? saveResult.valid
          ? "Mapping is consistent. The Krauken is using it now."
          : "Saved -- but the rig can't run yet."
        : "Ready to save.";

  return (
    <main className={styles.page}>
      <p className={styles.backLink}>
        <Link to="/">&larr; Back to {fermentations.data?.[0]?.name ?? "the dashboard"}</Link>
      </p>

      {appState.data?.active_fermentation_id != null && (
        <div className={styles.runningBanner}>
          <Tag tone="orange" size="sm">
            Running
          </Tag>
          <span>
            A fermentation is running in this chamber. Saved changes take effect on the next control cycle --
            reassigning cooling or heating will re-home the outlets mid-batch.
          </span>
        </div>
      )}

      <div className={styles.headerRow}>
        <div>
          <h1 className={styles.title}>Hardware setup</h1>
          <div className={styles.subtitle}>Map what The Krauken reads from and controls.</div>
        </div>
        <div className={styles.scanBar}>
          <div className={styles.scanStatus}>
            <span className={`${styles.scanDot} ${scanning ? styles.scanning : styles.done}`} />
            {scanLabel}
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={scanning}
            onClick={triggerScan}
          >
            Scan again
          </Button>
        </div>
      </div>

      {scanStartError && (
        <div className={`${styles.notice} ${styles.danger}`}>
          Couldn't reach the hardware scanner: {scanStartError} Check that krauken-daemon is running, then try
          "Scan again".
        </div>
      )}

      {saveResult && saveResult.auto_resolved.length > 0 && (
        <div className={styles.notices}>
          {saveResult.auto_resolved.map((n, i) => (
            <div key={i} className={`${styles.notice} ${styles.info}`}>
              {n.message}
            </div>
          ))}
        </div>
      )}

      <div className={styles.section}>
        <div className={styles.sectionHead}>
          <div className={styles.sectionLabel}>Detected hardware</div>
          <div className={styles.sectionMeta}>
            {deviceList.length} detected &middot; {filledRequired} of {REQUIRED_ROLES.size} required roles filled
          </div>
        </div>
        <div className={styles.deviceList}>
          {deviceList.map((d) => {
            const availableTests = (d.metadata.available_tests as string[] | undefined) ?? [];
            const roles = rolesOfDevice(d.device_id);
            // fire_outlet drives the full outlet-testing flow; a firmware-
            // managed controller like BrewPi (finalize_device_config) has
            // its own guided flow instead -- discovering and installing
            // probe/relay mappings itself rather than assuming they're
            // already set up. identify_probes alone is kept as a fallback
            // condition for robustness, though no current platform
            // advertises it without one of the other two.
            const guided =
              d.is_bundle &&
              (availableTests.includes("fire_outlet") ||
                availableTests.includes("finalize_device_config") ||
                availableTests.includes("identify_probes"));
            const ownsCore = roles.includes(Role.CHAMBER_TEMP) && roles.includes(Role.CHAMBER_COOLING);
            const ownsHeating = roles.includes(Role.CHAMBER_HEATING);
            const ownsBeerTemp = roles.includes(Role.BEER_TEMP);
            const unhealthy = d.health !== "ok" && roles.length > 0;
            const capableRoles = ALL_ROLES.filter((r) => deviceInfoMap[d.device_id]?.capabilities.has(r));

            const chips: { label: string; on: boolean; onClick: () => void }[] = d.is_bundle
              ? [
                  {
                    label: "Chamber temp + cooling",
                    on: ownsCore,
                    onClick: guided
                      ? () => setWizardDeviceId(d.device_id)
                      : () => {
                          assign(Role.CHAMBER_TEMP, ownsCore ? null : d.device_id);
                          assign(Role.CHAMBER_COOLING, ownsCore ? null : d.device_id);
                        },
                  },
                  {
                    label: "Chamber heating",
                    on: ownsHeating,
                    onClick: guided
                      ? () => setWizardDeviceId(d.device_id)
                      : () => assign(Role.CHAMBER_HEATING, ownsHeating ? null : d.device_id),
                  },
                  ...(capableRoles.includes(Role.BEER_TEMP)
                    ? [
                        {
                          label: "Beer temp",
                          on: ownsBeerTemp,
                          onClick: () => assign(Role.BEER_TEMP, ownsBeerTemp ? null : d.device_id),
                        },
                      ]
                    : []),
                ]
              : capableRoles.map((r) => {
                  const on = roles.includes(r);
                  return { label: ROLE_LABELS[r], on, onClick: () => assign(r, on ? null : d.device_id) };
                });

            const canFireOutlets = availableTests.includes("fire_outlet");
            const chipHint = guided
              ? ownsCore
                ? canFireOutlets
                  ? "Set up during guided setup -- reconfigure to re-identify probes and outlets."
                  : "Set up during guided setup -- reconfigure to re-discover the probe and relay pins."
                : canFireOutlets
                  ? "Not set up yet. Guided setup identifies the probes and outlets, and you opt into roles as you go."
                  : "Not set up yet. Guided setup identifies the probes and finds the cooling (and optional heating) pin itself."
              : roles.length === 0
                ? "Detected, not in use."
                : "Each role is picked independently.";

            return (
              <div key={d.device_id} className={`${styles.deviceRow} ${unhealthy ? styles.deviceRowBad : ""}`}>
                <div className={styles.deviceInfo}>
                  <div className={styles.deviceNameRow}>
                    <span className={`${styles.dot} ${unhealthy ? styles.dotBad : roles.length > 0 ? styles.dotOn : ""}`} />
                    <span className={styles.deviceName}>{d.name}</span>
                    {d.is_bundle && (
                      <Tag tone="gray" size="sm">
                        Chamber bundle
                      </Tag>
                    )}
                  </div>
                  <div className={styles.deviceKind}>{d.kind}</div>
                  {typeof d.metadata.detail_line === "string" && d.metadata.detail_line && (
                    <div className={styles.deviceDetail}>{d.metadata.detail_line}</div>
                  )}
                  {unhealthy && (
                    <div className={styles.deviceUnhealthy}>
                      Assigned to {roles.map((r) => ROLE_LABELS[r]).join(" + ")} but not answering.
                    </div>
                  )}
                </div>

                <div className={styles.deviceRoles}>
                  <div className={styles.deviceRolesHead}>
                    <span className={styles.deviceRolesLabel}>Roles it plays</span>
                    <span className={styles.deviceReading}>
                      {typeof d.metadata.reading_summary === "string" ? d.metadata.reading_summary : ""}
                    </span>
                  </div>
                  <div className={styles.chipRow}>
                    {chips.map((c) => (
                      <button
                        key={c.label}
                        type="button"
                        className={`${styles.chip} ${c.on ? styles.chipOn : ""}`}
                        onClick={c.onClick}
                      >
                        <span className={`${styles.chipMark} ${c.on ? styles.chipMarkOn : ""}`}>{c.on ? "✓" : ""}</span>
                        {c.label}
                      </button>
                    ))}
                  </div>
                  <div className={styles.chipHint}>{chipHint}</div>
                </div>

                <div className={styles.deviceActions}>
                  {guided && (
                    <Button variant={roles.length > 0 ? "ghost" : "secondary"} size="sm" onClick={() => setWizardDeviceId(d.device_id)}>
                      {roles.length > 0 ? "Reconfigure" : "Set up"}
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
          {deviceList.length === 0 && scanState === "complete" && (
            <div className={styles.deviceRow}>
              <p className={styles.deviceDetail}>No devices found. Check your wiring and try scanning again.</p>
            </div>
          )}
        </div>
      </div>

      <div className={styles.section}>
        <div className={styles.sectionLabel}>Control roles</div>
        <div className={styles.rolesCard}>
          {ALL_ROLES.map((role) => {
            const resolvedValue = preview.roles[role];
            const device = resolvedValue ? deviceById[resolvedValue] : undefined;
            const required = REQUIRED_ROLES.has(role);
            const unhealthy = device != null && device.health !== "ok";
            const blockedRow = !device && required;
            const isChamberBundlePart = device?.is_bundle && CHAMBER_BUNDLE.has(role);

            let note: string;
            let noteTone: "danger" | "warn" | "muted" = "muted";
            if (unhealthy) {
              note = "Assigned but unreachable -- live fault, not a setup error.";
              noteTone = "danger";
            } else if (blockedRow) {
              note = "The rig cannot run a fermentation until a device takes this role.";
              noteTone = "warn";
            } else if (!device && role === Role.CHAMBER_HEATING) {
              note = "Cooling-only rig -- The Krauken never calls for heat.";
            } else if (!device && role === Role.BEER_GRAVITY) {
              note = "Gravity-gated stages fall back to their time caps.";
            } else if (isChamberBundlePart) {
              note = `Part of the ${device!.name.replace(/^The /, "")} chamber bundle.`;
            } else {
              note = "Reading every 30s.";
            }

            return (
              <div key={role} className={styles.roleRow}>
                <span
                  className={`${styles.roleIcon} ${device ? (unhealthy ? styles.roleIconBad : styles.roleIconOn) : blockedRow ? styles.roleIconBad : ""}`}
                >
                  {device ? "✓" : "✕"}
                </span>
                <div className={styles.roleNameCol}>
                  <span className={styles.roleName}>{ROLE_LABELS[role]}</span>
                  <span className={required ? styles.roleReq : styles.roleOpt}>{required ? "Required" : "Optional"}</span>
                </div>
                <div className={styles.roleDeviceCol}>
                  <span className={`${styles.roleDeviceName} ${blockedRow ? styles.roleDeviceNameBad : ""}`}>
                    {device ? device.name : blockedRow ? "Nothing assigned" : "None"}
                  </span>
                  <span className={`${styles.roleNote} ${styles[noteTone]}`}>{note}</span>
                </div>
                <span className={styles.roleReading}>{readingFor(role, device)}</span>
              </div>
            );
          })}
        </div>
        <div className={styles.mapNote}>
          Chamber temp, cooling and heating always travel together as one controller. Beer temp has no fallback -- if
          it drops out mid-batch, The Krauken holds the last chamber target and alerts you.
        </div>
      </div>

      <div className={styles.saveSpacer} />
      <div className={styles.saveBar}>
        <span className={`${styles.saveNote} ${!preview.valid || saveError ? styles.saveNoteDanger : ""}`}>{saveNote}</span>
        <div className={styles.saveActions}>
          <Button variant="ghost" onClick={handleReset}>
            Reset
          </Button>
          <Button variant="primary" onClick={handleSave} disabled={saveDisabled}>
            {saveMapping.isPending ? "Saving…" : saved ? "Saved" : "Save mapping"}
          </Button>
        </div>
      </div>

      {wizardDevice && (
        <HardwareWizard
          device={wizardDevice}
          currentDraft={draft}
          open={wizardDeviceId !== undefined}
          onCancel={() => setWizardDeviceId(undefined)}
          onFinish={handleWizardFinish}
        />
      )}
    </main>
  );
}
