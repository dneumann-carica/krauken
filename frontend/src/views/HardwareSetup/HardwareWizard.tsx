import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Button, Dialog } from "../../design/primitives";
import { useCancelTest, useSaveMapping, useStartTest, useTestStatus } from "../../api/queries";
import type { DeviceResponse, MappingSaveResponse } from "../../api/types";
import { CHAMBER_BUNDLE, Role } from "../../hardware/resolve";
import styles from "./HardwareWizard.module.css";

type Stage =
  | "intro"
  | "relayA"
  | "heatB"
  | "coolB"
  | "noheat"
  | "nocool"
  | "probes"
  | "summary"
  // BrewPi's device-configuration wizard (platforms/brewpi/device_config.py) --
  // replaces identify_probes/confirm_heater as the SETUP mechanism for any
  // firmware-managed chamber controller: Krauken discovers and installs
  // probe/relay mappings itself, rather than assuming BrewPi's own classic
  // web UI already did it (confirmed this session that assumption is often
  // false -- the real reference rig has cooling installed and no heat
  // device installed anywhere).
  | "deviceBootstrap"
  | "deviceProbeId"
  | "deviceNoProbe"
  // Unified relay pin-identification sweep -- replaces the old two-pass
  // deviceCoolSweep/deviceHeatSweep split. Always forces a heat demand and
  // lets the human observer say what physically turned on (fridge/heater/
  // nothing); confirmed live this session that testing cool and heat as
  // separate passes left stray same-function candidates installed, which
  // BrewPi drives together rather than enforcing one-actuator-per-function.
  | "devicePinSweep"
  | "deviceNoCool"
  | "deviceFinalizing";

// Matches daemon/tests_runtime.py's FIRE_OUTLET_DURATION_S -- the real,
// settled default, not a shortened dev-testing value.
const FIRE_DURATION_S = 10;

// DeviceHardware code -- platforms/brewpi/device_config.py's exact value,
// confirmed against the real firmware source this session. identify_relay_pin
// always assigns a candidate to CHAMBER_HEAT regardless of its eventual
// role (see the REVISED DESIGN plan section), so no DeviceFunction
// constant is needed here at all -- this view only ever needs to filter
// candidates down to raw hardware pins.
const DEVICE_HARDWARE_PIN = 1;

const STEP_LABELS: Record<Stage, string> = {
  intro: "Step 1 of 3",
  relayA: "Step 2 of 3",
  heatB: "Step 2a",
  coolB: "Step 2b",
  noheat: "Step 2b · no heater",
  nocool: "Step 2b · no cooling",
  probes: "Step 3 of 3",
  summary: "Summary",
  deviceBootstrap: "Preparing",
  deviceProbeId: "Step 1 of 3",
  deviceNoProbe: "Step 1 of 3 · no probe",
  devicePinSweep: "Step 2 of 3",
  deviceNoCool: "Step 2 of 3 · no cooling",
  deviceFinalizing: "Finishing up",
};

interface Picks {
  cool?: 0 | 1;
  heat?: 0 | 1 | null;
}

// --- BrewPi device-configuration wire shapes (mirrors
// platforms/brewpi/device_config.py's BrewPiDevice.to_dict() and each
// runner's job.result exactly). ---

interface RawBrewPiDevice {
  slot: number;
  chamber: number;
  beer: number;
  function: number;
  hardware: number;
  deactivated: number;
  pin: number | null;
  address: string | null;
  calibration: number | null;
  invert: number | null;
  value: number | null;
}

interface BrewPiDevicesResult {
  devices: RawBrewPiDevice[];
}

interface IdentifyOnewireResult {
  identified_address: string | null;
  baseline_f: Record<string, number | null>;
  current_f: Record<string, number | null>;
  readable: Record<string, boolean>;
}

// begin_device_config's {"wiped": [...]} and install_probe's
// {"installed": {...}} results are never read by this view -- both stages
// only care whether the job reached "completed", not its payload -- so no
// dedicated result interface exists for either (would be unused, same as
// every other job.result shape that's genuinely only a completion signal).

type RelayOutcome = "waiting" | "engaged" | "timeout";

interface IdentifyRelayPinResult {
  outcome: RelayOutcome;
  baseline_f: number;
  forced_target_f: number;
  current_f: number | null;
  slot: number;
}

interface FinalizeResult {
  pushed: RawBrewPiDevice[];
  installed: RawBrewPiDevice[];
}

interface ProbeIdentity {
  address: string;
  pin: number;
}

interface RelayIdentity {
  pin: number;
  invert: number;
}

// Wizard-local picks for the device-config flow -- deliberately separate
// from Picks above (the outlet-fire flow's own shape): these track raw
// probe addresses and relay pins, not outlet-fired booleans.
interface DevicePicks {
  chamberProbe?: ProbeIdentity;
  beerProbe?: ProbeIdentity | null;
  cool?: RelayIdentity;
  heat?: RelayIdentity | null;
}

interface Props {
  device: DeviceResponse;
  currentDraft: Record<Role, string | null>;
  open: boolean;
  onCancel: () => void;
  onFinish: (result: MappingSaveResponse) => void;
}

export function HardwareWizard({ device, currentDraft, open, onCancel, onFinish }: Props) {
  const [stage, setStage] = useState<Stage>("intro");
  const [picks, setPicks] = useState<Picks>({});
  const [devicePicks, setDevicePicks] = useState<DevicePicks>({});
  const [testId, setTestId] = useState<string>();
  const [, setTick] = useState(0);

  // The full device list (installed + available, with live values) --
  // fetched once per device-config phase transition via brewpi_devices,
  // used to build each sweep's candidate list and to learn the OneWire
  // bus pin (every OneWire device on this rig shares one bus pin,
  // confirmed via real captured data) for the probe-identification
  // stage's config payload.
  const [deviceList, setDeviceList] = useState<RawBrewPiDevice[]>();
  // "installingChamberProbe" is a brief, automatic sub-phase between
  // confirming the chamber probe and starting the beer-probe identify --
  // installs the chamber probe as CHAMBER_TEMP immediately (see
  // runInstallProbe), rather than deferring every install to
  // finalize_device_config. Fixes a real sequencing bug found this
  // session: identify_relay_pin needs a live FridgeTemp reading to
  // compute a baseline, and nothing was ever installed with that function
  // until the very last wizard step, which runs after the relay sweep --
  // so every sweep attempt failed regardless of what was actually wired.
  const [probePhase, setProbePhase] = useState<"chamber" | "installingChamberProbe" | "beer">("chamber");
  // devicePinSweep's per-candidate state: testedPins have had both
  // polarities tried with nothing identified (excluded from the
  // candidate list going forward, alongside whichever pins devicePicks
  // already confirmed); polarityPhase tracks which polarity the *current*
  // (first untested) candidate is on.
  const [testedPins, setTestedPins] = useState<number[]>([]);
  const [polarityPhase, setPolarityPhase] = useState<"normal" | "reversed">("normal");
  // Once cooling is confirmed, heat is optional -- these two track the
  // user's answer to "keep looking for a heater, or stop here?" (asked
  // once, right after cooling's found) rather than auto-continuing to
  // burn through every remaining candidate's real anti-short-cycle wait
  // on hardware nobody has.
  const [heatSweepConfirmed, setHeatSweepConfirmed] = useState(false);
  const [heatDeclined, setHeatDeclined] = useState(false);
  // Tracks whether this sweep has EVER reached a genuine engagement
  // (outcome === "engaged") -- confirmed live 2026-08-16: the very first
  // candidate's wait is a real, unavoidable BrewPi anti-short-cycle timer
  // (observed ~2m15s), but every subsequent candidate swap is near-instant
  // once that first engagement has happened (the bypass mechanism --
  // reassigning CHAMBER_HEAT to a different pin without ever idling in
  // between leaves the firmware's timers satisfied). Before that first
  // engagement, offering a "skip" while waiting silently discards it and
  // forces the NEXT candidate to pay the same real wait from scratch, with
  // no indication anything was lost. Reset to false only on a genuine
  // restart-from-scratch (deviceNoCool's "Start over", or a fresh
  // begin_device_config), never merely on a candidate/polarity swap.
  const [everEngaged, setEverEngaged] = useState(false);

  const startTest = useStartTest();
  const cancelTest = useCancelTest();
  const testStatus = useTestStatus(device.device_id, testId);
  const saveMapping = useSaveMapping();

  const availableTests = (device.metadata.available_tests as string[] | undefined) ?? [];
  const canIdentify = availableTests.includes("identify_probes");
  // BrewPi (and any future firmware-managed chamber controller) has no
  // way to independently fire just the cooling or just the heating
  // relay -- the real, running firmware decides that internally. Those
  // devices skip straight from intro to the device-configuration wizard
  // below instead of the outlet-firing stages: Krauken discovers which
  // OneWire probe is chamber/beer and which pin drives cool/heat itself,
  // rather than assuming BrewPi's own classic web UI already set that up.
  const canFireOutlets = availableTests.includes("fire_outlet");
  const canConfigureDevices = availableTests.includes("finalize_device_config");
  const probeAddresses = (device.metadata.probe_addresses as string[] | undefined) ?? [];
  const twoProbes = probeAddresses.length >= 2;

  const testRunning = testStatus.data?.state === "running";
  const testDone = testId !== undefined && testStatus.data != null && testStatus.data.state !== "running";

  // The countdown is read from ends_at at render time, but React Query's
  // structural sharing reuses the previous response object when polling
  // returns an unchanged payload -- which skips the re-render a plain
  // Date.now()-at-render-time countdown depends on. A local tick, independent
  // of the poll, forces a re-render every 250ms so the number actually counts
  // down instead of freezing.
  useEffect(() => {
    if (!testRunning) return;
    const id = setInterval(() => setTick((t) => t + 1), 250);
    return () => clearInterval(id);
  }, [testRunning]);

  // Counting down against ends_at - Date.now() assumes the server's clock
  // and the browser's clock agree -- true for a real deployment, but the
  // dev panel's clock-advance feature (KRAUKEN_DEV_PANEL's
  // OffsettableSystemClock) can put the daemon's clock hours or days ahead
  // of real wall time, which turned "10s remaining" into "22503s
  // remaining". Anchoring on a browser-local start time and a
  // server-reported *duration* (ends_at - started_at, which cancels any
  // clock offset since both sides come from the same clock) makes the
  // countdown immune to that regardless of what the server's clock reads.
  const localStartRef = useRef<number | null>(null);
  useEffect(() => {
    if (testRunning && localStartRef.current == null) localStartRef.current = Date.now();
    if (!testRunning) localStartRef.current = null;
  }, [testRunning]);

  const totalDurationS =
    testStatus.data?.started_at && testStatus.data?.ends_at
      ? (new Date(testStatus.data.ends_at).getTime() - new Date(testStatus.data.started_at).getTime()) / 1000
      : FIRE_DURATION_S;
  const remainingS =
    testRunning && localStartRef.current != null
      ? Math.max(0, Math.ceil(totalDurationS - (Date.now() - localStartRef.current) / 1000))
      : 0;

  function resetLocal() {
    setStage("intro");
    setPicks({});
    setDevicePicks({});
    setTestId(undefined);
    setDeviceList(undefined);
    setProbePhase("chamber");
    setTestedPins([]);
    setPolarityPhase("normal");
    setHeatSweepConfirmed(false);
    setHeatDeclined(false);
    setEverEngaged(false);
  }

  function goTo(next: Stage, nextPicks?: Picks) {
    if (nextPicks !== undefined) setPicks(nextPicks);
    setTestId(undefined);
    setStage(next);
  }

  function fire(outlet: 1 | 2) {
    setTestId(undefined);
    startTest.mutate(
      { deviceId: device.device_id, action: "fire_outlet", params: { outlet, duration_s: FIRE_DURATION_S } },
      { onSuccess: (r) => { localStartRef.current = Date.now(); setTestId(r.test_id); } },
    );
  }

  function runIdentify() {
    setTestId(undefined);
    startTest.mutate(
      { deviceId: device.device_id, action: "identify_probes", params: {} },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  // --- BrewPi device-configuration helpers ---

  function runBeginDeviceConfig() {
    startTest.mutate(
      { deviceId: device.device_id, action: "begin_device_config", params: {} },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  function runBrewPiDevices() {
    startTest.mutate(
      { deviceId: device.device_id, action: "brewpi_devices", params: {} },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  function runIdentifyOnewire(exclude: string[]) {
    setTestId(undefined);
    startTest.mutate(
      { deviceId: device.device_id, action: "identify_onewire_probes", params: { exclude_addresses: exclude } },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  function runInstallProbe(role: "chamber" | "beer", probe: ProbeIdentity) {
    setTestId(undefined);
    startTest.mutate(
      { deviceId: device.device_id, action: "install_probe", params: { role, address: probe.address, pin: probe.pin } },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  // One (pin, polarity) combination per call -- no "function" param.
  // Forcing "heat" is the sole trigger used regardless of which physical
  // role this pin turns out to have; the frontend asks the human what
  // turned on. invert is passed explicitly by the caller (normal vs.
  // reversed polarity are two separate calls, never a retry the backend
  // decides on its own).
  function runIdentifyRelayPin(candidate: RawBrewPiDevice, invert: number) {
    setTestId(undefined);
    // identified_pins tells the backend which currently-installed
    // CHAMBER_HEAT device (if any) has actually been confirmed as the
    // heater or the fridge -- it only gets the safe-off flip-and-confirm
    // treatment if its pin is in this list. A pin where every polarity
    // came back "nothing happened" has no confirmed off level to aim
    // for, so it gets a bare uninstall instead (see device_config.py's
    // own docstring for the full reasoning).
    const identifiedPins = [devicePicks.cool?.pin, devicePicks.heat?.pin].filter((p): p is number => p != null);
    startTest.mutate(
      {
        deviceId: device.device_id,
        action: "identify_relay_pin",
        params: { candidate: { pin: candidate.pin, invert }, identified_pins: identifiedPins },
      },
      { onSuccess: (r) => { localStartRef.current = Date.now(); setTestId(r.test_id); } },
    );
  }

  function runFinalize(config: {
    chamber_probe?: ProbeIdentity;
    beer_probe?: ProbeIdentity | null;
    cool?: RelayIdentity;
    heat?: RelayIdentity | null;
  }) {
    setTestId(undefined);
    startTest.mutate(
      { deviceId: device.device_id, action: "finalize_device_config", params: { config } },
      { onSuccess: (r) => setTestId(r.test_id) },
    );
  }

  // The sweep can genuinely run for several real minutes on real hardware
  // (BrewPi's own anti-short-cycle protection) for the FIRST candidate
  // that ever engages -- a user who already knows the answer, or wants to
  // move to the next candidate, shouldn't have to wait it out. Also a
  // real correctness requirement, not just UX: the daemon only allows one
  // running test per device_id at a time (TestAlreadyRunning), so the
  // previous candidate's job MUST be cancelled before starting the next
  // one's -- cancel_test() is a safe no-op if it already completed.
  function cancelCurrentTest() {
    if (testId !== undefined) {
      cancelTest.mutate({ deviceId: device.device_id, testId });
    }
  }

  // brewpi_devices completing populates deviceList and clears testId --
  // the stage-specific effects below (identify_onewire_probes/
  // identify_relay_pin) only start their own real test once deviceList is
  // set, so clearing testId here is what lets them proceed with a fresh
  // test_id rather than being blocked by this now-finished lookup still
  // occupying it.
  useEffect(() => {
    if (testStatus.data?.kind === "brewpi_devices" && testStatus.data.state === "completed") {
      const result = testStatus.data.result as BrewPiDevicesResult | null;
      setDeviceList(result?.devices ?? []);
      setTestId(undefined);
    }
  }, [testStatus.data]);

  // deviceBootstrap: the wizard's very first device-config action --
  // self-heals a prior incomplete session's baseline, snapshots the
  // current clean state, and wipes every device to unassigned (see
  // begin_device_config's own docstring). Auto-starts on entry, then
  // auto-advances to deviceProbeId once complete.
  useEffect(() => {
    if (stage === "deviceBootstrap" && testId === undefined && !startTest.isPending) {
      runBeginDeviceConfig();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, testId]);

  useEffect(() => {
    if (stage === "deviceBootstrap" && testStatus.data?.kind === "begin_device_config" && testStatus.data.state === "completed") {
      setTestId(undefined);
      goTo("deviceProbeId");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, testStatus.data]);

  // Auto-starts each device-config test the instant its stage is ready for
  // it -- matches the plan's "runs on entry" design (the user is expected
  // to already be warming a probe, or waiting on a relay, by the time they
  // see the corresponding screen) rather than requiring an extra click.
  useEffect(() => {
    if (stage === "deviceProbeId" && deviceList === undefined && testId === undefined && !startTest.isPending) {
      runBrewPiDevices();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, deviceList, testId]);

  useEffect(() => {
    if (stage !== "deviceProbeId" || deviceList === undefined) return;
    if (probePhase === "installingChamberProbe") return; // handled by its own effect below
    if (testId !== undefined || startTest.isPending) return;
    const exclude = probePhase === "beer" && devicePicks.chamberProbe ? [devicePicks.chamberProbe.address] : [];
    runIdentifyOnewire(exclude);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, deviceList, probePhase]);

  // Installs the chamber probe as CHAMBER_TEMP the instant it's confirmed
  // -- not deferred to finalize_device_config (see runInstallProbe's own
  // comment; fixes a real sequencing bug found this session). Auto-starts,
  // then auto-advances to the beer-probe identify once complete.
  useEffect(() => {
    if (stage !== "deviceProbeId" || probePhase !== "installingChamberProbe") return;
    if (testId !== undefined || startTest.isPending) return;
    if (!devicePicks.chamberProbe) return;
    runInstallProbe("chamber", devicePicks.chamberProbe);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, probePhase, testId, devicePicks.chamberProbe]);

  useEffect(() => {
    if (probePhase !== "installingChamberProbe") return;
    if (testStatus.data?.kind === "install_probe" && testStatus.data.state === "completed") {
      setTestId(undefined);
      setProbePhase("beer");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [probePhase, testStatus.data]);

  // Candidates still worth testing: every raw hardware pin not yet
  // conclusively identified as cool or heat, and not yet exhausted (both
  // polarities tried, nothing found). Always the FIRST entry is "the
  // current candidate" -- there is no separate index to keep in sync with
  // a shrinking/reordering list.
  function buildRelayCandidates(): RawBrewPiDevice[] {
    const excluded = new Set(
      [devicePicks.cool?.pin, devicePicks.heat?.pin, ...testedPins].filter((p): p is number => p != null),
    );
    return (deviceList ?? []).filter((d) => d.hardware === DEVICE_HARDWARE_PIN && d.pin != null && !excluded.has(d.pin));
  }

  useEffect(() => {
    if (stage === "devicePinSweep" && deviceList === undefined && !startTest.isPending) runBrewPiDevices();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, deviceList]);

  // Cooling is required, so keep testing for it unconditionally. Heat is
  // optional -- once cooling's known, only keep testing remaining
  // candidates for it if the user explicitly said so (the
  // heatSweepConfirmed interstitial, rendered below); otherwise this
  // effect intentionally does nothing further, and the exit-condition
  // effect below moves on to finalize/deviceNoCool.
  useEffect(() => {
    if (stage !== "devicePinSweep" || deviceList === undefined) return;
    if (testId !== undefined || startTest.isPending) return;
    const needCool = devicePicks.cool === undefined;
    const needHeat = devicePicks.heat === undefined && !heatDeclined && heatSweepConfirmed;
    if (!needCool && !needHeat) return;
    const candidates = buildRelayCandidates();
    const candidate = candidates[0];
    if (candidate === undefined) return; // exhausted -- exit-condition effect handles this
    const invert = polarityPhase === "reversed" ? (candidate.invert ? 0 : 1) : (candidate.invert ?? 0);
    runIdentifyRelayPin(candidate, invert);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, deviceList, testId, polarityPhase, testedPins, devicePicks.cool, devicePicks.heat, heatDeclined, heatSweepConfirmed]);

  // A real 600s timeout with nothing identified auto-advances -- never a
  // user-facing choice (the user isn't asked to try reversed polarity;
  // see advancePolarityOrCandidate).
  useEffect(() => {
    if (stage !== "devicePinSweep") return;
    if (testStatus.data?.kind !== "identify_relay_pin" || testStatus.data.state !== "completed") return;
    const result = testStatus.data.result as IdentifyRelayPinResult | null;
    if (result?.outcome !== "timeout") return;
    const candidate = buildRelayCandidates()[0];
    if (candidate) advancePolarityOrCandidate(candidate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, testStatus.data]);

  // Latches the first genuine engagement this sweep -- see everEngaged's
  // own declaration for why this matters (the Skip button is only safe to
  // offer once this has happened at least once).
  useEffect(() => {
    if (stage !== "devicePinSweep" || everEngaged) return;
    if (testStatus.data?.kind !== "identify_relay_pin") return;
    const result = testStatus.data.result as IdentifyRelayPinResult | null;
    if (result?.outcome === "engaged") setEverEngaged(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, testStatus.data, everEngaged]);

  // The exit transitions belong here (real effects), not inline during
  // render.
  useEffect(() => {
    if (stage !== "devicePinSweep" || deviceList === undefined) return;
    const candidates = buildRelayCandidates();
    const coolKnown = devicePicks.cool !== undefined;
    const heatSettled = devicePicks.heat !== undefined || heatDeclined || candidates.length === 0;
    if (coolKnown && heatSettled) {
      goTo("deviceFinalizing");
    } else if (!coolKnown && candidates.length === 0) {
      goTo("deviceNoCool");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, deviceList, devicePicks.cool, devicePicks.heat, heatDeclined, testedPins]);

  // Shared by the automatic timeout-advance effect and the "Nothing
  // happened" button: on the pin's first (normal) polarity, automatically
  // move to reversed polarity on the SAME pin -- never a user choice (the
  // user isn't asked whether to try reversed polarity, they wouldn't know
  // how to answer). Only after both polarities have found nothing does
  // this pin get excluded and the sweep move to the next candidate.
  function advancePolarityOrCandidate(candidate: RawBrewPiDevice) {
    cancelCurrentTest();
    setTestId(undefined);
    if (polarityPhase === "normal") {
      setPolarityPhase("reversed");
    } else {
      setPolarityPhase("normal");
      setTestedPins((prev) => [...prev, candidate.pin as number]);
    }
  }

  const heatingConfirmed = picks.heat === 0 || picks.heat === 1 || (devicePicks.heat !== undefined && devicePicks.heat !== null);

  async function handleFinish() {
    const merged = { ...currentDraft };
    merged.chamber_temp = device.device_id;
    merged.chamber_cooling = device.device_id;
    merged.chamber_heating = heatingConfirmed
      ? device.device_id
      : merged.chamber_heating === device.device_id
        ? null
        : merged.chamber_heating;
    onFinish(await saveMapping.mutateAsync(merged));
  }

  function handleCancel() {
    // Best-effort cleanup on any device* stage: reset_brewpi now restores
    // the baseline snapshot begin_device_config captured at the start of
    // this session (if one exists) and clears the file -- a real "revert
    // to the beginning state," not just a bare reset. Fire it
    // unconditionally (fire-and-forget, doesn't block closing the dialog)
    // whenever there's device-config state that might need reverting.
    if (canConfigureDevices && stage.startsWith("device")) {
      cancelCurrentTest();
      startTest.mutate({ deviceId: device.device_id, action: "reset_brewpi", params: {} });
    }
    resetLocal();
    onCancel();
  }

  function firePanel(outlet: 1 | 2, note: string) {
    return (
      <div className={styles.firePanel}>
        <p className={styles.body}>{note}</p>
        <button
          type="button"
          className={`${styles.fireButton} ${testRunning ? styles.fireRunning : ""}`}
          onClick={() => fire(outlet)}
          disabled={testRunning}
        >
          {testRunning ? `Outlet ${outlet} on — ${remainingS}s remaining` : `Turn on Outlet ${outlet} for ${FIRE_DURATION_S}s`}
        </button>
      </div>
    );
  }

  function choice(label: string, sub: string, onClick: () => void) {
    return (
      <button type="button" className={styles.choiceButton} onClick={onClick}>
        <span className={styles.choiceLabel}>{label}</span>
        <span className={styles.choiceSub}>{sub}</span>
      </button>
    );
  }

  let subtitle = "";
  let body: ReactNode = null;
  let showNext = false;
  let nextLabel = "";
  let nextDisabled = false;
  let onNext: () => void = () => {};

  if (stage === "intro") {
    subtitle = canFireOutlets
      ? "Three steps: switch each outlet on to learn what it controls, then identify the probes."
      : "This controller manages cooling and heating internally -- we'll identify the probes, then find the cooling (and optional heating) pin.";
    body = (
      <p className={styles.body}>
        Nothing is written until you finish. Cooling and chamber temp are required; heating and beer temp are
        optional and can be skipped.
      </p>
    );
    showNext = true;
    nextLabel = "Start setup";
    onNext = () => goTo(canFireOutlets ? "relayA" : canConfigureDevices ? "deviceBootstrap" : "probes");
  } else if (stage === "deviceProbeId") {
    subtitle =
      probePhase === "beer"
        ? "Identifying the beer probe, if one is wired."
        : probePhase === "installingChamberProbe"
          ? "Saving the chamber probe."
          : "Identifying the chamber probe.";
    const result = testStatus.data?.result as IdentifyOnewireResult | null | undefined;
    const addresses = Object.keys(result?.current_f ?? {});
    const pinFor = (addr: string) => deviceList?.find((d) => d.address === addr)?.pin ?? 0;

    if (probePhase === "installingChamberProbe") {
      body = <p className={styles.body}>Installing the chamber probe…</p>;
    } else if (deviceList === undefined || testId === undefined || testStatus.data === undefined) {
      // testStatus.data can briefly be undefined even once testId is set
      // (TanStack Query resets `data` to undefined the instant the query
      // key -- which includes testId -- changes, before the new job's
      // first poll response arrives). Without this check, that one-render
      // gap fell through to the `addresses.length === 0` branch below and
      // rendered a false "no probe detected" -- reproduced live this
      // session on every job-to-job transition in this stage, not just
      // the first one.
      body = <p className={styles.body}>Reading the controller's currently-wired sensors…</p>;
    } else if (addresses.length === 0) {
      body = <p className={styles.body}>No {probePhase} probe detected.</p>;
      if (!testRunning) {
        showNext = true;
        nextLabel = "Continue";
        onNext = () =>
          probePhase === "chamber"
            ? goTo("deviceNoProbe")
            : (setDevicePicks((p) => ({ ...p, beerProbe: null })), goTo("devicePinSweep"));
      }
    } else if (addresses.length === 1) {
      // Nothing to compare against -- same "just confirm it responds"
      // shape as the existing single-probe identify_probes path.
      const addr = addresses[0];
      const reading = result?.current_f?.[addr] ?? null;
      const readable = result?.readable?.[addr] ?? false;
      body = (
        <div className={styles.probeReadout}>
          <span className={styles.probeReadoutLabel}>{probePhase === "chamber" ? "Chamber probe" : "Beer probe"}</span>
          <span className={styles.probeReadoutValue}>
            {readable && reading != null ? `${reading.toFixed(1)}°F` : "not currently reading"}
          </span>
        </div>
      );
      if (!testRunning) {
        showNext = true;
        nextLabel = "Continue";
        onNext = () => {
          if (probePhase === "chamber") {
            setDevicePicks((p) => ({ ...p, chamberProbe: { address: addr, pin: pinFor(addr) } }));
            setTestId(undefined); // let the install-probe effect start its own fresh test
            setProbePhase("installingChamberProbe");
          } else {
            setDevicePicks((p) => ({ ...p, beerProbe: { address: addr, pin: pinFor(addr) } }));
            goTo("devicePinSweep");
          }
        };
      }
    } else {
      body = (
        <>
          <p className={styles.body}>
            {result?.identified_address
              ? "Confirmed -- the highlighted probe moved."
              : testRunning
                ? `Warm the ${probePhase} probe in your hand and watch for a 3°F change…`
                : "No temperature change detected yet -- warm the probe and try again."}
          </p>
          <div className={styles.probeGrid}>
            {addresses.map((addr) => {
              const reading = result?.current_f?.[addr] ?? null;
              const baseline = result?.baseline_f?.[addr] ?? null;
              const readable = result?.readable?.[addr] ?? false;
              const delta = reading != null && baseline != null ? reading - baseline : null;
              const moved = result?.identified_address === addr;
              return (
                <div key={addr} className={`${styles.probeRow} ${moved ? styles.probeRowMoved : ""}`}>
                  <span className={styles.probeReadoutLabel}>{addr}</span>
                  <span className={styles.probeReadoutValue}>
                    {readable && reading != null ? `${reading.toFixed(1)}°F` : "not currently reading"}
                  </span>
                  {delta != null && <span className={styles.probeDelta}>{delta >= 0 ? "+" : ""}{delta.toFixed(1)}°F</span>}
                </div>
              );
            })}
          </div>
        </>
      );
      if (result?.identified_address) {
        showNext = true;
        nextLabel = "Continue";
        onNext = () => {
          const addr = result.identified_address as string;
          if (probePhase === "chamber") {
            setDevicePicks((p) => ({ ...p, chamberProbe: { address: addr, pin: pinFor(addr) } }));
            setTestId(undefined); // let the install-probe effect start its own fresh test
            setProbePhase("installingChamberProbe");
          } else {
            setDevicePicks((p) => ({ ...p, beerProbe: { address: addr, pin: pinFor(addr) } }));
            goTo("devicePinSweep");
          }
        };
      } else if (!testRunning) {
        showNext = true;
        nextLabel = "Try again";
        onNext = () => runIdentifyOnewire(probePhase === "beer" && devicePicks.chamberProbe ? [devicePicks.chamberProbe.address] : []);
      }
    }
    // Skip button for the beer pass only -- a chamber-only rig is valid,
    // and a detected-but-unwanted second sensor shouldn't force a beer
    // mapping the user doesn't want.
    if (probePhase === "beer" && !testRunning) {
      body = (
        <>
          {body}
          <Button variant="ghost" onClick={() => { setDevicePicks((p) => ({ ...p, beerProbe: null })); goTo("devicePinSweep"); }}>
            No beer probe
          </Button>
        </>
      );
    }
  } else if (stage === "deviceBootstrap") {
    subtitle = "Preparing device configuration.";
    if (testStatus.data?.state === "failed") {
      body = (
        <>
          <p className={styles.body}>Preparing device configuration failed: {testStatus.data?.error ?? "unknown error"}.</p>
          <div className={styles.choiceGrid}>
            {choice("Retry", "Try again.", () => setTestId(undefined))}
            {choice("Cancel setup", "Nothing further is written.", handleCancel)}
          </div>
        </>
      );
    } else {
      body = <p className={styles.body}>Checking the controller's current configuration…</p>;
    }
  } else if (stage === "deviceNoProbe") {
    subtitle = "No OneWire temperature sensor was detected at all.";
    body = (
      <p className={styles.body}>
        A chamber probe is required -- check that a OneWire sensor is actually wired to this controller, then try
        again.
      </p>
    );
    showNext = true;
    nextLabel = "Try again";
    onNext = () => { setDeviceList(undefined); setProbePhase("chamber"); goTo("deviceProbeId"); };
  } else if (stage === "devicePinSweep") {
    const candidates = buildRelayCandidates();
    const candidate = candidates[0];
    const result = testStatus.data?.result as IdentifyRelayPinResult | null | undefined;
    const coolKnown = devicePicks.cool !== undefined;

    subtitle = coolKnown
      ? "Optional -- a heater makes cold-garage ferments possible, but a cooling-only rig is valid."
      : "Finding which pin drives cooling (checking for a heater along the way).";

    if (coolKnown && devicePicks.heat === undefined && !heatDeclined && !heatSweepConfirmed) {
      // One-time interstitial, asked once cooling is known and there are
      // still untested candidates -- heat is optional, so continuing to
      // burn through every remaining candidate's real anti-short-cycle
      // wait needs an explicit yes, not an automatic continuation.
      body = (
        <>
          <p className={styles.body}>
            Cooling confirmed on pin {devicePicks.cool?.pin}. Want to also look for a heater?
          </p>
          <div className={styles.choiceGrid}>
            {choice("Test for a heater", "Sweep the remaining pins for one that engages.", () => setHeatSweepConfirmed(true))}
            {choice("No heater", "Cooling-only rig -- heating stays unfilled.", () => setHeatDeclined(true))}
          </div>
        </>
      );
    } else if (deviceList === undefined) {
      body = <p className={styles.body}>Reading the controller's currently-wired pins…</p>;
    } else if (candidate === undefined) {
      // Exhausted -- the exit-condition effect handles the real
      // transition (to deviceFinalizing or deviceNoCool); this is just
      // the brief frame in between.
      body = <p className={styles.body}>Checking results…</p>;
    } else if (testStatus.data?.state === "failed") {
      // A failed job (e.g. the chamber probe isn't reading) is a systemic
      // precondition failure, not "this pin wasn't it" -- surfacing it as
      // an ordinary timeout would silently hide the real error and invite
      // cycling through every remaining candidate against the identical
      // failure. Offer Retry (same pin/polarity) instead of "try the next
      // pin."
      body = (
        <>
          <p className={styles.body}>Something went wrong: {testStatus.data.error ?? "unknown error"}.</p>
          <div className={styles.choiceGrid}>
            {choice("Retry", "Try this test again.", () => setTestId(undefined))}
            {choice("Cancel setup", "Nothing further is written.", handleCancel)}
          </div>
        </>
      );
    } else if (testId === undefined || (result === undefined && testRunning === false && !testDone)) {
      body = <p className={styles.body}>Starting the live test on pin {candidate.pin}…</p>;
    } else if (result?.outcome === "waiting" || (testRunning && result === undefined)) {
      body = (
        <>
          <p className={styles.body}>
            Forced the target to {result?.forced_target_f?.toFixed(1) ?? "…"}°F (chamber was{" "}
            {result?.baseline_f?.toFixed(1) ?? "…"}°F) on pin {candidate.pin}. Waiting for the compressor-protection
            timer to clear -- no countdown is available, but this is normal.
          </p>
          {everEngaged ? (
            <div className={styles.choiceGrid}>
              {choice("Skip -- try the next test", "Move on without waiting further.", () => advancePolarityOrCandidate(candidate))}
            </div>
          ) : (
            // No skip before this sweep's first genuine engagement --
            // confirmed live 2026-08-16: skipping here doesn't save time,
            // it discards the one real wait BrewPi's anti-short-cycle
            // timer requires and forces the next candidate to pay it
            // again from scratch, with no indication anything was lost.
            <p className={styles.body}>
              This first test can take up to 10 minutes -- BrewPi's compressor-protection timer has to clear once
              before anything can switch on for the first time. This only happens once.
            </p>
          )}
        </>
      );
    } else if (result?.outcome === "engaged") {
      body = (
        <>
          <p className={styles.body}>
            Pin {candidate.pin}'s state just switched to a heating demand. Chamber probe:{" "}
            {result.current_f != null ? `${result.current_f.toFixed(1)}°F` : "reading…"}. Did anything turn on?
          </p>
          <div className={styles.choiceGrid}>
            {choice("The heater came on", "This pin is confirmed as heating.", () => {
              const invert = polarityPhase === "reversed" ? (candidate.invert ? 0 : 1) : (candidate.invert ?? 0);
              setDevicePicks((p) => ({ ...p, heat: { pin: candidate.pin as number, invert } }));
              setPolarityPhase("normal");
            })}
            {choice("The fridge came on", "This pin is confirmed as cooling.", () => {
              const invert = polarityPhase === "reversed" ? (candidate.invert ? 0 : 1) : (candidate.invert ?? 0);
              setDevicePicks((p) => ({ ...p, cool: { pin: candidate.pin as number, invert } }));
              setPolarityPhase("normal");
            })}
            {choice("Nothing happened", "Check the next test.", () => advancePolarityOrCandidate(candidate))}
          </div>
        </>
      );
    } else {
      // A brief transitional frame -- outcome === "timeout" auto-advances
      // via its own effect with no user interaction required.
      body = <p className={styles.body}>Nothing detected on pin {candidate.pin} -- checking the next test…</p>;
    }
  } else if (stage === "deviceNoCool") {
    subtitle = "Cooling is required -- no pin engaged cooling across every candidate.";
    body = (
      <p className={styles.body}>
        Check that the fridge/compressor relay is actually wired to one of this controller's actuator terminals,
        then try again.
      </p>
    );
    showNext = true;
    nextLabel = "Start over";
    onNext = () => {
      setDeviceList(undefined);
      setTestedPins([]);
      setPolarityPhase("normal");
      setHeatSweepConfirmed(false);
      setHeatDeclined(false);
      setEverEngaged(false);
      goTo("devicePinSweep");
    };
  } else if (stage === "deviceFinalizing") {
    subtitle = "Pushing the final configuration.";
    const result = testStatus.data?.result as FinalizeResult | null | undefined;

    if (testId === undefined && !startTest.isPending) {
      runFinalize({
        chamber_probe: devicePicks.chamberProbe,
        beer_probe: devicePicks.beerProbe,
        cool: devicePicks.cool,
        heat: devicePicks.heat,
      });
      body = <p className={styles.body}>Writing the configuration and resetting the controller…</p>;
    } else if (testRunning || testId === undefined) {
      body = <p className={styles.body}>Writing the configuration and resetting the controller…</p>;
    } else if (testStatus.data?.state === "completed") {
      body = <p className={styles.body}>Configuration pushed -- {result?.installed?.length ?? 0} devices installed.</p>;
      showNext = true;
      nextLabel = "Continue";
      onNext = () => goTo("summary");
    } else {
      body = (
        <>
          <p className={styles.body}>Pushing the configuration failed: {testStatus.data?.error ?? "unknown error"}.</p>
          <div className={styles.choiceGrid}>
            {choice("Retry", "Try pushing the same configuration again.", () => {
              setTestId(undefined);
              runFinalize({
                chamber_probe: devicePicks.chamberProbe,
                beer_probe: devicePicks.beerProbe,
                cool: devicePicks.cool,
                heat: devicePicks.heat,
              });
            })}
            {choice("Cancel setup", "Nothing further is written.", handleCancel)}
          </div>
        </>
      );
    }
  } else if (stage === "relayA") {
    subtitle = "Turn on the first outlet and watch what comes on.";
    if (testDone) {
      body = (
        <>
          <p className={styles.body}>
            Outlet 1 was on for {FIRE_DURATION_S} seconds, then switched off. What came on?
          </p>
          <div className={styles.choiceGrid}>
            {choice("The fridge came on", "Outlet 1 is the cooling outlet.", () => goTo("heatB", { cool: 0 }))}
            {choice("The heater came on", "Outlet 1 is the heating outlet — next we look for cooling.", () =>
              goTo("coolB", { heat: 0 }),
            )}
            {choice("Nothing happened", "Nothing is plugged into Outlet 1 — next we try the other one.", () =>
              goTo("coolB", {}),
            )}
          </div>
        </>
      );
    } else {
      body = firePanel(
        1,
        "Stand where you can hear the fridge and see any heat source. Nothing is assigned yet -- this is how The Krauken learns which outlet is which.",
      );
    }
  } else if (stage === "heatB") {
    subtitle = "Optional -- a heater makes cold-garage ferments possible, but a cooling-only rig is valid.";
    if (testDone) {
      body = (
        <>
          <p className={styles.body}>Outlet 2 was closed for {FIRE_DURATION_S} seconds. Did a heat source come on?</p>
          <div className={styles.choiceGrid}>
            {choice("The heater came on", "Outlet 2 becomes the heating outlet.", () =>
              goTo("probes", { ...picks, heat: 1 }),
            )}
            {choice("Skip setting up a heater", "Cooling-only rig -- heating stays unfilled.", () =>
              goTo("probes", { ...picks, heat: null }),
            )}
          </div>
        </>
      );
    } else {
      body = firePanel(2, "If a heat belt, pad, or lamp is wired to Outlet 2, firing it should switch it on.");
      showNext = true;
      nextLabel = "Don't configure a heater";
      nextDisabled = testRunning;
      onNext = () => goTo("probes", { ...picks, heat: null });
    }
  } else if (stage === "coolB") {
    subtitle = "Cooling is required -- turn on the other outlet.";
    if (testDone) {
      body = (
        <>
          <p className={styles.body}>Outlet 2 was on for {FIRE_DURATION_S} seconds. Did the fridge come on?</p>
          <div className={styles.choiceGrid}>
            {choice(
              "The fridge came on",
              picks.heat === 0
                ? "Outlet 2 runs the fridge, Outlet 1 runs the heater — both outlets mapped."
                : "Outlet 2 is the cooling outlet.",
              () => (picks.heat === 0 ? goTo("probes", { ...picks, cool: 1 }) : goTo("noheat", { cool: 1, heat: null })),
            )}
            {choice("Nothing came on", "Neither outlet runs a fridge — cooling cannot be mapped.", () =>
              goTo("nocool", {}),
            )}
          </div>
        </>
      );
    } else {
      body = firePanel(
        2,
        picks.heat === 0
          ? "Outlet 1 switches your heater, so the fridge should be on Outlet 2."
          : "Nothing answered on Outlet 1. The fridge should be on Outlet 2.",
      );
    }
  } else if (stage === "noheat") {
    subtitle = "Cooling is mapped. No heating outlet was found.";
    body = (
      <>
        <p className={styles.body}>
          Outlet 2 runs the fridge. Nothing responded on Outlet 1, so this rig can cool but not heat -- fine for a
          warm room, but it cannot hold a target below ambient drift in winter.
        </p>
        <div className={styles.choiceGrid}>
          {choice(
            "Continue without a heater",
            "Valid cooling-only rig. You can run this wizard again after wiring one.",
            () => goTo("probes"),
          )}
          {choice("Start over", "Re-run both outlet tests from the top.", () => goTo("relayA", {}))}
        </div>
      </>
    );
  } else if (stage === "nocool") {
    subtitle = "Nothing responded on either outlet.";
    body = (
      <>
        <p className={styles.body}>
          Cooling is required -- without it there is no rig to run. Check that the fridge is plugged into the
          switched outlet and that the outlet has power, then try both outlets again.
        </p>
        <div className={styles.choiceGrid}>
          {choice("Start over", "Re-run the outlet tests from Outlet 1.", () => goTo("relayA", {}))}
          {choice("Cancel setup", "Nothing is written. The existing mapping is untouched.", handleCancel)}
        </div>
      </>
    );
  } else if (stage === "probes") {
    subtitle = twoProbes ? "Telling the two probes apart." : "Confirming the chamber probe is responding.";
    const result = testStatus.data?.result as
      | { identified_address: string | null; baseline_f: Record<string, number | null>; current_f: Record<string, number | null> }
      | null
      | undefined;

    if (!canIdentify) {
      body = <p className={styles.body}>This controller has no separate probe check -- continuing to the summary.</p>;
      showNext = true;
      nextLabel = "Continue";
      onNext = () => goTo("summary");
    } else if (!twoProbes) {
      if (testId === undefined) {
        body = <p className={styles.body}>The Krauken checks that its chamber probe is answering before this is saved.</p>;
        showNext = true;
        nextLabel = "Check probe";
        onNext = () => runIdentify();
      } else {
        const reading = result?.current_f?.[probeAddresses[0]] ?? null;
        body = (
          <div className={styles.probeReadout}>
            <span className={styles.probeReadoutLabel}>Chamber probe</span>
            <span className={styles.probeReadoutValue}>{reading != null ? `${reading.toFixed(1)}°F` : "reading…"}</span>
          </div>
        );
        if (!testRunning) {
          showNext = true;
          nextLabel = "Continue";
          onNext = () => goTo("summary");
        }
      }
    } else {
      const [chamberAddr, beerAddr] = probeAddresses;
      if (testId === undefined) {
        body = (
          <p className={styles.body}>
            Two probes are wired to this controller. Warm the beer probe in your hand (or nudge it a few degrees on
            the dev panel) -- The Krauken will highlight whichever one moves.
          </p>
        );
        showNext = true;
        nextLabel = "Check probes";
        onNext = () => runIdentify();
      } else {
        const rows: Array<{ addr: string; label: string }> = [
          { addr: chamberAddr, label: "Chamber probe" },
          { addr: beerAddr, label: "Beer probe" },
        ];
        body = (
          <>
            <p className={styles.body}>
              {result?.identified_address
                ? "Confirmed -- the highlighted probe moved."
                : testRunning
                  ? "Watching both probes for a 3°F change…"
                  : "No movement was seen -- warm a probe and try again."}
            </p>
            <div className={styles.probeGrid}>
              {rows.map(({ addr, label }) => {
                const reading = result?.current_f?.[addr] ?? null;
                const baseline = result?.baseline_f?.[addr] ?? null;
                const delta = reading != null && baseline != null ? reading - baseline : null;
                const moved = result?.identified_address === addr;
                return (
                  <div key={addr} className={`${styles.probeRow} ${moved ? styles.probeRowMoved : ""}`}>
                    <span className={styles.probeReadoutLabel}>{label}</span>
                    <span className={styles.probeReadoutValue}>{reading != null ? `${reading.toFixed(1)}°F` : "reading…"}</span>
                    {delta != null && <span className={styles.probeDelta}>{delta >= 0 ? "+" : ""}{delta.toFixed(1)}°F</span>}
                  </div>
                );
              })}
            </div>
          </>
        );
        if (result?.identified_address) {
          showNext = true;
          nextLabel = "Continue";
          onNext = () => goTo("summary");
        } else if (!testRunning) {
          showNext = true;
          nextLabel = "Try again";
          onNext = () => runIdentify();
        }
      }
    }
  } else if (stage === "summary") {
    subtitle = `${device.name} will take these roles.`;
    body = (
      <div className={styles.summaryList}>
        {[...CHAMBER_BUNDLE].map((role) => {
          const included = role !== Role.CHAMBER_HEATING || heatingConfirmed;
          return (
            <div key={role} className={styles.summaryRow}>
              <span>{role.replace(/_/g, " ")}</span>
              <span>{included ? device.name : "left unmapped -- no heater confirmed"}</span>
            </div>
          );
        })}
        {devicePicks.chamberProbe && (
          <div className={styles.summaryRow}>
            <span>chamber probe</span>
            <span>{devicePicks.chamberProbe.address}</span>
          </div>
        )}
        {devicePicks.beerProbe && (
          <div className={styles.summaryRow}>
            <span>beer probe</span>
            <span>{devicePicks.beerProbe.address}</span>
          </div>
        )}
        {devicePicks.cool && (
          <div className={styles.summaryRow}>
            <span>cooling pin</span>
            <span>pin {devicePicks.cool.pin}{devicePicks.cool.invert ? " (inverted)" : ""}</span>
          </div>
        )}
        {devicePicks.heat && (
          <div className={styles.summaryRow}>
            <span>heating pin</span>
            <span>pin {devicePicks.heat.pin}{devicePicks.heat.invert ? " (inverted)" : ""}</span>
          </div>
        )}
      </div>
    );
    showNext = true;
    nextLabel = saveMapping.isPending ? "Saving…" : "Finish setup";
    nextDisabled = saveMapping.isPending;
    onNext = handleFinish;
  }

  const closeLabel = stage === "summary" ? "Back out" : "Cancel setup";

  return (
    <Dialog open={open} onClose={handleCancel}>
      <div className={styles.dialogInner}>
        <div className={styles.header}>
          <div className={styles.headerTop}>
            <div className={styles.title}>Set up {device.name}</div>
            <span className={styles.stepLabel}>{STEP_LABELS[stage]}</span>
          </div>
          <div className={styles.subtitle}>{subtitle}</div>
        </div>
        <div className={styles.content}>{body}</div>
        <div className={styles.footer}>
          <Button variant="ghost" onClick={handleCancel}>
            {closeLabel}
          </Button>
          {showNext && (
            <Button variant="primary" onClick={onNext} disabled={nextDisabled}>
              {nextLabel}
            </Button>
          )}
        </div>
      </div>
    </Dialog>
  );
}
