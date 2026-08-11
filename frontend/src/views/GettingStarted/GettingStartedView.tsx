import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { FermentationChart } from "../../chart/FermentationChart";
import type { PillTone } from "../../design/primitives";
import { Button, Card, Dialog, MetricStat, StatusPill, Switch, Tag } from "../../design/primitives";
import {
  useAdvanceStage,
  useAlerts,
  useAppState,
  useChamberStatus,
  useFermentation,
  useFermentations,
  useSeries,
  useStopChamber,
  useTerminateFermentation,
  useUpdateStages,
} from "../../api/queries";
import type { FermentationSummary, RoleStatus, StageResponse } from "../../api/types";
import type { Role } from "../../hardware/resolve";
import { ROLE_LABELS } from "../../hardware/roleLabels";
import type { CloneSource } from "./FermentationPlanDialog";
import { FermentationPlanDialog } from "./FermentationPlanDialog";
import styles from "./GettingStartedView.module.css";

function lastDefined<T>(arr: (T | null)[]): T | null {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] !== null) return arr[i];
  }
  return null;
}

function fmtTemp(v: number | null): string {
  return v === null ? "--" : `${v.toFixed(1)}°F`;
}

function dotColor(role: RoleStatus): string {
  if (!role.filled) return "var(--kr-border-strong)";
  if (role.health === "ok") return "var(--kr-ok)";
  if (role.health === "degraded") return "var(--kr-warn)";
  return "var(--kr-danger)";
}

function daysBetween(startIso: string, endIso: string | null): number {
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  return Math.round((end - start) / 86_400_000);
}

function fmtElapsed(ms: number): string {
  const totalMin = Math.max(0, Math.round(ms / 60_000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h${m}m` : `${m}m`;
}

// How long the chamber has held its current mode -- walks back from the
// latest sample while chamber_mode stays the same, then diffs timestamps.
// Sampling is variable-interval by design, so this has to read real
// timestamps rather than assuming a fixed cadence.
function elapsedInMode(ts: string[], modes: string[]): number | null {
  if (modes.length === 0) return null;
  const lastMode = modes[modes.length - 1];
  let i = modes.length - 1;
  while (i > 0 && modes[i - 1] === lastMode) i--;
  const start = new Date(ts[i]).getTime();
  const end = new Date(ts[ts.length - 1]).getTime();
  return end - start;
}

function fmtMenuDate(iso: string, includeYear: boolean): string {
  return new Date(iso).toLocaleDateString(
    undefined,
    includeYear ? { month: "short", day: "numeric", year: "numeric" } : { month: "short", day: "numeric" },
  );
}

// The title-menu batch list's secondary line -- "Active" for the one
// currently running (a date range would just show "started, still going",
// which is what "Active" already says more plainly), a real start–end
// range for anything finished. The end date gets its year appended once
// it's no longer this year -- otherwise last December's batch and one
// from three weeks ago both just read "Dec 30", indistinguishable at a
// glance. The start date never does; a range implies "same era as its own
// end date" well enough on its own.
function fmtMenuDateRange(f: FermentationSummary): string {
  if (f.status === "active") return "Active";
  if (!f.ended_at) return fmtMenuDate(f.started_at, false);
  // Not just "ended before this year" -- an accelerated SimulatorClock
  // batch can just as easily race PAST the current year into next year
  // (started from a real-now anchor, then ticked through weeks of
  // simulated time in seconds). Either direction is equally ambiguous
  // without a year shown, and equally capable of producing a menu order
  // that looks wrong at a glance if you assume everything's "this year."
  const differentYear = new Date(f.ended_at).getFullYear() !== new Date().getFullYear();
  return `${fmtMenuDate(f.started_at, false)} – ${fmtMenuDate(f.ended_at, differentYear)}`;
}

function fmtHours(hours: number): string {
  return hours >= 24 ? `${(hours / 24).toFixed(hours % 24 === 0 ? 0 : 1)} days` : `${hours}h`;
}

// Days+hours, not a raw hour count or a fractional day -- "452h elapsed"
// isn't something a human parses at a glance, and "18.8 days" isn't much
// better. Used for actual ELAPSED durations specifically; a PLANNED
// duration (fmtHours above, e.g. "4d" in the stage ribbon) reads fine as a
// single rounded unit since it's describing a round authored number, not
// an arbitrary elapsed one.
function fmtDaysHours(hours: number): string {
  const totalHours = Math.round(hours);
  const days = Math.floor(totalHours / 24);
  const rem = totalHours % 24;
  if (days === 0) return `${rem}h`;
  return rem === 0 ? `${days}d` : `${days}d ${rem}h`;
}

function criteriaDescription(stage: StageResponse): string {
  if (stage.end_mode === "time") return `${fmtHours(stage.end_hours ?? 0)} elapse`;
  if (stage.end_mode === "temp_hold") return `beer holds ${stage.hold_temp_f?.toFixed(1)}°F for ${fmtHours(stage.hold_hours ?? 0)}`;
  return `gravity flattens at or below ${stage.gravity_hi?.toFixed(3)} for ${fmtHours(stage.gravity_stable_hours ?? 0)}`;
}

function runningLine(stage: StageResponse, target: number | null): string {
  if (stage.temp_mode === "stepped" && stage.temp_from_f != null && stage.temp_to_f != null) {
    return `Stepping beer temp ${fmtTemp(stage.temp_from_f)} → ${fmtTemp(stage.temp_to_f)}`;
  }
  return `Holding beer temp at ${fmtTemp(target)}`;
}

function modeLabelFor(mode: string): string {
  return mode === "cool" ? "Cooling" : mode === "heat" ? "Heating" : "Idle";
}

// Which stage was actually running at a given timestamp -- used while
// scrubbing so the Setpoint tile's stage name reflects the scrubbed
// moment, not whatever stage happens to be current "now".
function stageAtTs(stages: StageResponse[], ts: string): StageResponse | undefined {
  const t = new Date(ts).getTime();
  for (let i = stages.length - 1; i >= 0; i--) {
    const started = stages[i].started_at;
    if (started && new Date(started).getTime() <= t) return stages[i];
  }
  return stages[0];
}

function stageEndSentence(stage: StageResponse): string {
  const capHours = stage.max_hours ?? (stage.end_mode === "time" ? stage.end_hours : null);
  const criteria = `Ends when ${criteriaDescription(stage)}`;
  if (capHours == null || !stage.started_at) {
    return `${criteria} -- no time cap set.`;
  }
  const end = new Date(new Date(stage.started_at).getTime() + capHours * 3_600_000);
  const dateLabel = end.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const timeLabel = end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${criteria} -- projected ${dateLabel}, ${timeLabel}.`;
}

export function GettingStartedView() {
  const navigate = useNavigate();
  const state = useAppState();
  const fermentations = useFermentations();
  // Only meaningful when nothing's running -- a live fermentation's chamber
  // is trivially "on" the whole time it's active, so there's no "orphaned
  // target" question to ask until it's the only thing still holding one.
  // Called unconditionally (Rules of Hooks) even though `state.data` may
  // still be loading here -- `enabled` just leans permissive until the
  // guards below resolve it for real; chamberOrphaned re-derives the
  // authoritative answer from `hasActiveBatch` once they have.
  const chamberStatus = useChamberStatus(state.data?.active_fermentation_id == null);
  // The viewed batch lives in the URL, not plain component state -- a
  // refresh must keep showing whatever you were looking at, not silently
  // fall back to the default. Only in the absence of that (a fresh load
  // with no ?batch= at all) does it pick a default, and that default
  // prefers whatever's actively running over merely "most recently
  // started" -- fermentations_list sorts by started_at, which a
  // SimulatorClock-driven batch can't be trusted to place correctly
  // relative to wall-clock-timed ones.
  const [searchParams, setSearchParams] = useSearchParams();
  const viewedIdParam = searchParams.get("batch");
  const parsedViewedId = viewedIdParam !== null ? Number(viewedIdParam) : NaN;
  const defaultBatchId = state.data?.active_fermentation_id ?? fermentations.data?.[0]?.id;
  const batchId = Number.isFinite(parsedViewedId) ? parsedViewedId : defaultBatchId;
  const isLive = state.data?.active_fermentation_id !== undefined && state.data?.active_fermentation_id === batchId;
  const detail = useFermentation(batchId, { live: isLive });
  const series = useSeries(batchId, { live: isLive });
  const alerts = useAlerts(isLive ? batchId : undefined, { live: isLive });

  // Independent polling intervals (state every 5s, detail/series on their
  // own slower cadences while live) mean isLive can flip false the moment a
  // batch completes, stopping detail/series polling, BEFORE their own next
  // scheduled poll would have picked up the truly-final state -- the
  // observed symptom was the chart freezing mid-stage with a stale NOW
  // marker and projection that never cleared. One extra fetch right on the
  // true->false transition closes that gap; nothing needs to keep polling
  // after that, since the daemon's completion writes (final sample, status,
  // stage state) all land in the same control tick, not staggered later.
  // Deliberately NOT alerts: unlike detail/series (always queried on
  // batchId, gated only on *polling* via live:isLive), alerts is gated on
  // *identity* too (isLive ? batchId : undefined) since it's never shown
  // for a non-live batch -- calling .refetch() on it here bypasses its own
  // enabled:false and fires with fermentationId already undefined.
  const wasLiveRef = useRef(isLive);
  useEffect(() => {
    if (wasLiveRef.current && !isLive) {
      detail.refetch();
      series.refetch();
    }
    wasLiveRef.current = isLive;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLive]);

  const advanceStage = useAdvanceStage();
  const terminateFermentation = useTerminateFermentation();
  const stopChamber = useStopChamber();
  const updateStages = useUpdateStages();

  const [showStartForm, setShowStartForm] = useState(false);
  const [showEditProfile, setShowEditProfile] = useState(false);
  const [showViewProfile, setShowViewProfile] = useState(false);
  const [showEndConfirm, setShowEndConfirm] = useState(false);
  // Set alongside showStartForm to seed the "new" dialog from an existing
  // batch's plan instead of a yeast preset -- "Clone as new batch," from
  // either the header action or the read-only view dialog. Cleared on
  // close so a later plain "Start a new fermentation" doesn't reuse it.
  const [cloneFrom, setCloneFrom] = useState<CloneSource | null>(null);
  const menuRef = useRef<HTMLDetailsElement>(null);

  // Native <details> has no "click outside to close" behavior of its own --
  // only reopening the <summary> toggles it back off. A capturing pointerdown
  // listener on the whole document, closing whenever the click lands outside
  // the element, is the standard way to bolt that on. Attached unconditionally
  // (not just while open) since it's cheap and the element itself is stable
  // for the component's whole lifetime -- no need to add/remove it every
  // open/close cycle.
  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      const el = menuRef.current;
      if (el && el.open && e.target instanceof Node && !el.contains(e.target)) {
        el.removeAttribute("open");
      }
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, []);

  function openCloneDialog(source: CloneSource) {
    setCloneFrom(source);
    setShowViewProfile(false);
    setShowStartForm(true);
  }

  // Chart scrubbing (mouse-down/touch-drag on FermentationChart, see its
  // onScrub prop): while set, the stat tiles below show this sample's
  // values instead of the batch's latest/final ones. Reset whenever the
  // viewed batch changes so a leftover index never applies to a
  // different fermentation's series.
  const [scrubIndex, setScrubIndex] = useState<number | null>(null);
  useEffect(() => {
    setScrubIndex(null);
  }, [batchId]);

  if (state.isLoading || fermentations.isLoading) {
    return <p className={styles.loading}>Loading…</p>;
  }
  if (state.isError || fermentations.isError) {
    return <p className={styles.error}>Could not reach the API.</p>;
  }

  const appState = state.data!;
  const batch = detail.data;
  const seriesData = series.data;
  const hasActiveBatch = appState.active_fermentation_id !== null;
  const canStartNew = appState.can_start_fermentation && !hasActiveBatch;
  const chamberOrphaned = !hasActiveBatch && chamberStatus.data?.commanded_target_f != null;
  // The two blocking reasons are mutually exclusive in practice (you can't
  // be missing hardware AND mid-batch in a way that matters here), but
  // check the real one rather than always blaming hardware -- a batch
  // already being active reads very differently to a brewer than "go set
  // up your probes."
  const cannotStartReason = hasActiveBatch
    ? "Finish or terminate the current batch first"
    : "Unlocks once hardware is mapped";

  const lastBeer = seriesData ? lastDefined(seriesData.beer_temp_f) : null;
  const lastChamber = seriesData ? lastDefined(seriesData.chamber_temp_f) : null;
  const lastGravity = seriesData ? lastDefined(seriesData.gravity) : null;
  const lastTarget = seriesData ? lastDefined(seriesData.effective_target_f) : null;
  const hasGravity = lastGravity !== null;

  // While scrubbing the chart, the stat tiles below show this sample's
  // values instead of the batch's latest/final ones -- everything else
  // on the page (header status pill, live bar, elapsed time) keeps
  // reflecting the batch's actual current state regardless.
  const scrubTs = scrubIndex !== null && seriesData ? seriesData.ts[scrubIndex] : null;
  const displayBeer = scrubTs && seriesData ? seriesData.beer_temp_f[scrubIndex!] : lastBeer;
  const displayChamber = scrubTs && seriesData ? seriesData.chamber_temp_f[scrubIndex!] : lastChamber;
  const displayGravity = scrubTs && seriesData ? seriesData.gravity[scrubIndex!] : lastGravity;
  const displayTarget = scrubTs && seriesData ? seriesData.effective_target_f[scrubIndex!] : lastTarget;

  const currentStage = batch?.stages.find((s) => s.state === "running") ?? batch?.stages[batch.stages.length - 1];
  const nextStage = currentStage ? batch?.stages.find((s) => s.seq === currentStage.seq + 1) : undefined;
  const isComplete = batch?.status !== "active";
  // "at end"/"Final" framing only applies to the batch's actual final
  // values -- while scrubbing, the tiles are showing SOME earlier point in
  // time regardless of whether the batch itself has finished, so they need
  // the same plain labels a still-running fermentation uses.
  const showFinalLabels = isComplete && scrubIndex === null;
  const lastMode = seriesData?.chamber_mode[seriesData.chamber_mode.length - 1] ?? "idle";
  const displayMode = scrubTs && seriesData ? seriesData.chamber_mode[scrubIndex!] : lastMode;
  const chamberModeLabel = modeLabelFor(displayMode);
  const displayStage = scrubTs && batch ? (stageAtTs(batch.stages, scrubTs) ?? currentStage) : currentStage;
  const modeTone: PillTone = isComplete ? "neutral" : lastMode === "cool" ? "cool" : lastMode === "heat" ? "heat" : "idle";
  const modeLabel = isComplete
    ? batch?.status === "terminated"
      ? "Terminated"
      : "Batch complete"
    : lastMode === "cool"
      ? "Cooling"
      : lastMode === "heat"
        ? "Heating"
        : "Idle · in range";
  const elapsedDisplay =
    isComplete && batch
      ? `${daysBetween(batch.started_at, batch.ended_at)} days`
      : seriesData
        ? (() => {
            const ms = elapsedInMode(seriesData.ts, seriesData.chamber_mode);
            return ms != null ? fmtElapsed(ms) : null;
          })()
        : null;

  const stageIdx = currentStage ? (batch?.stages.findIndex((s) => s.id === currentStage.id) ?? -1) : -1;
  const stageReady = currentStage?.criteria_met_at != null;

  function closeMenu() {
    menuRef.current?.removeAttribute("open");
  }

  return (
    <main className={styles.page}>
      {chamberOrphaned && (
        <div className={styles.banner}>
          <div className={styles.bannerBody}>
            <div className={styles.bannerTitleRow}>
              <Tag tone="orange" size="sm">
                Chamber still on
              </Tag>
              <span className={styles.bannerTitle}>No fermentation is running, but the chamber is still holding a setpoint</span>
            </div>
            <div className={styles.bannerText}>Once you've removed the fermented beer from the chamber, turn off the chamber.</div>
          </div>
          <Button variant="primary" disabled={stopChamber.isPending} onClick={() => stopChamber.mutate()}>
            {stopChamber.isPending ? "Turning off…" : "Turn off chamber"}
          </Button>
        </div>
      )}

      {appState.setup_needed && (
        <div className={styles.banner}>
          <div className={styles.bannerBody}>
            <div className={styles.bannerTitleRow}>
              <Tag tone="orange" size="sm">
                Setup needed
              </Tag>
              <span className={styles.bannerTitle}>No hardware is configured yet</span>
            </div>
            <div className={styles.bannerText}>
              The Krauken can't read temperatures or switch an outlet until you tell it what is plugged in. Map your
              probes and outlets — about three minutes — and the menu on the batch name unlocks so you can start
              your first fermentation.
            </div>
          </div>
          <Button variant="primary" onClick={() => navigate("/hardware")}>
            Set up hardware
          </Button>
        </div>
      )}

      {batch?.demo && (
        <div className={styles.demoBanner}>
          <Tag tone="gray" size="sm">
            Sample data
          </Tag>
          <span className={styles.demoBannerText}>
            Everything below is a finished demo batch shipped with The Krauken, so you can see what a full ferment
            looks like. It disappears once you start your own.
          </span>
        </div>
      )}

      <div className={styles.headerRow}>
        <div className={styles.headerTitleCol}>
          <div className={styles.eyebrow}>The Krauken · Release the Krausen</div>
          <details ref={menuRef} className={styles.titleMenu}>
            <summary className={styles.titleMenuButton}>
              <span className={styles.titleRow}>
                <h1 className={styles.title}>{batch?.name ?? "No fermentation yet"}</h1>
                {batch?.demo && (
                  <Tag tone="gray" size="sm">
                    Demo
                  </Tag>
                )}
              </span>
              <span className={styles.chevronCircle}>
                {/* An inline SVG, not the Unicode ⌄ glyph -- that character's
                    ink sits visibly above center in most fonts (glyph metrics,
                    not a flex-centering bug), so no amount of align-items:
                    center on .chevronCircle could fix it. A hand-drawn path
                    centers by construction instead of by font luck. */}
                <svg className={styles.chevron} viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
            </summary>
            <div className={styles.titleMenuPanel}>
              <button
                type="button"
                className={styles.titleMenuAction}
                disabled={!canStartNew}
                onClick={() => {
                  if (!canStartNew) return;
                  closeMenu();
                  setCloneFrom(null);
                  setShowStartForm(true);
                }}
              >
                <span>Start a new fermentation</span>
                {!canStartNew && <span className={styles.titleMenuNote}>{cannotStartReason}</span>}
              </button>
              <div className={styles.titleMenuDivider} />
              <div className={styles.titleMenuLabel}>Fermentations</div>
              {fermentations.data && fermentations.data.length > 0 ? (
                fermentations.data.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={`${styles.titleMenuItem} ${f.id === batchId ? styles.titleMenuItemActive : ""}`}
                    onClick={() => {
                      setSearchParams({ batch: String(f.id) });
                      closeMenu();
                    }}
                  >
                    <span>{f.name}</span>
                    <span className={styles.titleMenuNote}>{fmtMenuDateRange(f)}</span>
                  </button>
                ))
              ) : (
                <div className={styles.titleMenuNote}>Your own batches appear here once you start one.</div>
              )}
            </div>
          </details>
        </div>

        <div className={styles.headerActions}>
          {batch && (
            <span className={styles.statusWithElapsed}>
              <StatusPill tone={modeTone}>{modeLabel}</StatusPill>
              {elapsedDisplay != null && <span className={styles.statusElapsed}>{elapsedDisplay}</span>}
            </span>
          )}
          {batch && !isLive && (
            <Button variant="outline" size="sm" onClick={() => setShowViewProfile(true)}>
              View profile
            </Button>
          )}
          {isLive && batch && (
            <Button variant="outline" size="sm" onClick={() => setShowEndConfirm(true)}>
              End fermentation
            </Button>
          )}
        </div>
      </div>

      {isLive && (alerts.data?.length ?? 0) > 0 && (
        <div className={styles.alertBanner} role="alert">
          {alerts.data!.map((a) => (
            <div key={a.field} className={styles.alertRow}>
              <span className={styles.alertDot} />
              <span>{a.message}</span>
            </div>
          ))}
        </div>
      )}

      {isLive && batch && currentStage && (
        <Card padding="sm" className={styles.liveBar}>
          <div className={styles.liveInfo}>
            <div className={styles.liveTagRow}>
              <Tag tone={stageReady ? "orange" : "blue"} size="sm">
                {currentStage.name} &middot; stage {stageIdx + 1} of {batch.stages.length}
              </Tag>
              <span className={styles.liveInfoTitle}>{runningLine(currentStage, lastTarget)}</span>
            </div>
            <span className={styles.liveInfoSub}>{stageEndSentence(currentStage)}</span>
            {nextStage ? (
              <label className={styles.autoRow}>
                <Switch
                  checked={currentStage.advance_mode === "auto"}
                  onChange={(auto) =>
                    updateStages.mutate({
                      fermentationId: batch.id,
                      stages: { [String(currentStage.id)]: { advance_mode: auto ? "auto" : "manual" } },
                    })
                  }
                />
                <span>
                  {currentStage.advance_mode === "auto"
                    ? "Auto-advance on -- the next stage starts as soon as the criteria are met."
                    : `Manual advance -- the Krauken holds ${currentStage.name} after the criteria are met until you say go.`}
                </span>
              </label>
            ) : (
              <span className={styles.liveLastStageNote}>
                Last stage -- the Krauken holds here until you end the fermentation.
              </span>
            )}
          </div>
          <div className={styles.liveActions}>
            {nextStage && (
              <Button
                variant={stageReady && currentStage.advance_mode === "manual" ? "primary" : "outline"}
                size="sm"
                onClick={() => advanceStage.mutate(batch.id)}
                disabled={advanceStage.isPending}
              >
                Advance to {nextStage.name}
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={() => setShowEditProfile(true)}>
              Edit profile
            </Button>
          </div>
        </Card>
      )}

      {batch && seriesData && (
        <>
          <div className={styles.statsGrid}>
            <Card padding="sm">
              <MetricStat
                value={fmtTemp(displayBeer)}
                label={showFinalLabels ? "Beer temp at end" : "Beer temp"}
                sublabel={`Target ${fmtTemp(displayTarget)}`}
                accent="orange"
              />
            </Card>
            <Card padding="sm">
              <MetricStat
                value={fmtTemp(displayChamber)}
                label={showFinalLabels ? "Chamber at end" : "Chamber"}
                sublabel={chamberModeLabel}
                accent="blue"
              />
            </Card>
            {hasGravity && (
              <Card padding="sm">
                <MetricStat
                  value={displayGravity !== null ? displayGravity.toFixed(3) : "--"}
                  label={showFinalLabels ? "Final gravity" : "Gravity"}
                  sublabel={
                    batch.og != null
                      ? `OG ${batch.og.toFixed(3)}${batch.abv_pct != null ? ` · ${batch.abv_pct.toFixed(1)}% ABV` : ""}`
                      : !isComplete
                        ? "Detecting OG…" // auto-detection hasn't settled yet -- see contracts/og_detection.py
                        : undefined
                  }
                  accent="navy"
                />
              </Card>
            )}
            <Card padding="sm">
              <MetricStat
                value={fmtTemp(displayTarget)}
                label="Setpoint"
                sublabel={`${displayStage?.name ?? ""} · ±0.5°F`}
                accent="plan"
              />
            </Card>
          </div>

          <Card padding="sm">
            <FermentationChart series={seriesData} stages={batch.stages} complete={isComplete} onScrub={setScrubIndex} />
          </Card>

          <div className={styles.legendCaption}>
            <span>
              Shaded background shows outlet activity — <span className={styles.legendCool}>cooling</span> and{" "}
              <span className={styles.legendHeat}>heating</span>. White means neither.
            </span>
            <span className={styles.legendMono}>
              Duty cycle, {fmtDaysHours(seriesData.duty.window_hours)} elapsed — cool {seriesData.duty.cool_pct}% &middot; heat{" "}
              {seriesData.duty.heat_pct}% &middot; idle {seriesData.duty.idle_pct}%
            </span>
          </div>
        </>
      )}

      <Link to="/hardware" className={styles.hardwareBar}>
        <span className={styles.hardwareLabel}>Hardware</span>
        <span className={styles.hardwareDots}>
          {appState.roles.map((r) => (
            <span key={r.role} className={styles.hardwareDot}>
              <span className={styles.dot} style={{ background: dotColor(r) }} />
              <span>
                {ROLE_LABELS[r.role as Role] ?? r.role} {r.filled ? `· ${r.device_name}` : "· unmapped"}
              </span>
            </span>
          ))}
        </span>
        <span className={styles.hardwareCta}>Set up hardware →</span>
      </Link>

      <FermentationPlanDialog
        mode="new"
        open={showStartForm}
        cloneFrom={cloneFrom}
        onClose={() => {
          setShowStartForm(false);
          setCloneFrom(null);
        }}
        onStarted={(fermentationId) => {
          setShowStartForm(false);
          setCloneFrom(null);
          setSearchParams({ batch: String(fermentationId) });
        }}
      />

      {batch && (
        <FermentationPlanDialog
          mode="edit"
          open={showEditProfile}
          onClose={() => setShowEditProfile(false)}
          fermentationId={batch.id}
          stages={batch.stages}
          onSaved={() => setShowEditProfile(false)}
        />
      )}

      {batch && (
        <FermentationPlanDialog
          mode="view"
          open={showViewProfile}
          onClose={() => setShowViewProfile(false)}
          fermentation={{ name: batch.name, yeast_id: batch.yeast_id, stages: batch.stages }}
          onClone={() => openCloneDialog({ name: batch.name, yeast_id: batch.yeast_id, stages: batch.stages })}
        />
      )}

      {batch && (
        <Dialog open={showEndConfirm} onClose={() => setShowEndConfirm(false)}>
          <div className={styles.confirmDialog}>
            <div className={styles.confirmTitle}>End this fermentation?</div>
            <p className={styles.confirmBody}>
              {batch.name} will stop being controlled. This can't be undone from here.
            </p>
            <div className={styles.confirmActions}>
              <Button variant="ghost" onClick={() => setShowEndConfirm(false)}>
                Keep running
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  terminateFermentation.mutate({ fermentationId: batch.id });
                  setShowEndConfirm(false);
                }}
              >
                End fermentation
              </Button>
            </div>
          </div>
        </Dialog>
      )}
    </main>
  );
}
