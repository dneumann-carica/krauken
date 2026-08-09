import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Button, Dialog } from "../../design/primitives";
import { useSaveMapping, useStartTest, useTestStatus } from "../../api/queries";
import type { DeviceResponse, MappingSaveResponse } from "../../api/types";
import { CHAMBER_BUNDLE, Role } from "../../hardware/resolve";
import styles from "./HardwareWizard.module.css";

type Stage = "intro" | "relayA" | "heatB" | "coolB" | "noheat" | "nocool" | "probes" | "summary";

// Matches daemon/tests_runtime.py's FIRE_OUTLET_DURATION_S -- the real,
// settled default, not a shortened dev-testing value.
const FIRE_DURATION_S = 10;

const STEP_LABELS: Record<Stage, string> = {
  intro: "Step 1 of 3",
  relayA: "Step 2 of 3",
  heatB: "Step 2a",
  coolB: "Step 2b",
  noheat: "Step 2b · no heater",
  nocool: "Step 2b · no cooling",
  probes: "Step 3 of 3",
  summary: "Summary",
};

interface Picks {
  cool?: 0 | 1;
  heat?: 0 | 1 | null;
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
  const [testId, setTestId] = useState<string>();
  const [, setTick] = useState(0);

  const startTest = useStartTest();
  const testStatus = useTestStatus(device.device_id, testId);
  const saveMapping = useSaveMapping();

  const availableTests = (device.metadata.available_tests as string[] | undefined) ?? [];
  const canIdentify = availableTests.includes("identify_probes");
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
    setTestId(undefined);
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

  const heatingConfirmed = picks.heat === 0 || picks.heat === 1;

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
    subtitle = "Three steps: switch each outlet on to learn what it controls, then identify the probes.";
    body = (
      <p className={styles.body}>
        Nothing is written until you finish. Cooling and chamber temp are required; heating and beer temp are
        optional and can be skipped.
      </p>
    );
    showNext = true;
    nextLabel = "Start setup";
    onNext = () => goTo("relayA");
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
