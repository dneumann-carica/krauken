import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { FermentationChart } from "../../chart/FermentationChart";
import type { PillTone } from "../../design/primitives";
import { Button, Card, Dialog, MetricStat, StatusPill, Tag } from "../../design/primitives";
import { useAdvanceStage, useChamberStatus, useStopChamber, useTerminateFermentation, useUpdateStages } from "../../api/queries";
import type { RoleStatus, StageResponse } from "../../api/types";
import type { Role } from "../../hardware/resolve";
import { ROLE_LABELS } from "../../hardware/roleLabels";
import { BatchTitleMenu } from "./BatchTitleMenu";
import type { CloneSource } from "./FermentationPlanDialog";
import { FermentationPlanDialog } from "./FermentationPlanDialog";
import { LiveStageBar } from "./LiveStageBar";
import { useBatchSelection } from "./useBatchSelection";
import { useScrubbedSeries } from "./useScrubbedSeries";
import styles from "./GettingStartedView.module.css";

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

// Days+hours, not a raw hour count or a fractional day -- "452h elapsed"
// isn't something a human parses at a glance, and "18.8 days" isn't much
// better.
function fmtDaysHours(hours: number): string {
  const totalHours = Math.round(hours);
  const days = Math.floor(totalHours / 24);
  const rem = totalHours % 24;
  if (days === 0) return `${rem}h`;
  return rem === 0 ? `${days}d` : `${days}d ${rem}h`;
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

export function GettingStartedView() {
  const navigate = useNavigate();
  const { state, fermentations, batchId, isLive, detail, series, alerts, setSearchParams } = useBatchSelection();
  // Only meaningful when nothing's running -- a live fermentation's chamber
  // is trivially "on" the whole time it's active, so there's no "orphaned
  // target" question to ask until it's the only thing still holding one.
  // Called unconditionally (Rules of Hooks) even though `state.data` may
  // still be loading here -- `enabled` just leans permissive until the
  // guards below resolve it for real; chamberOrphaned re-derives the
  // authoritative answer from `hasActiveBatch` once they have.
  const chamberStatus = useChamberStatus(state.data?.active_fermentation_id == null);

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

  // While scrubbing the chart, the stat tiles below show this sample's
  // values instead of the batch's latest/final ones -- everything else
  // on the page (header status pill, live bar, elapsed time) keeps
  // reflecting the batch's actual current state regardless. Called
  // unconditionally, before the loading/error guards below, same as every
  // other hook in this component (Rules of Hooks) -- seriesData may still
  // be undefined here, which the hook itself already treats as "no data
  // yet."
  const scrubbed = useScrubbedSeries(series.data, scrubIndex);

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

  const currentStage = batch?.stages.find((s) => s.state === "running") ?? batch?.stages[batch.stages.length - 1];
  const nextStage = currentStage ? batch?.stages.find((s) => s.seq === currentStage.seq + 1) : undefined;
  const isComplete = batch?.status !== "active";
  // "at end"/"Final" framing only applies to the batch's actual final
  // values -- while scrubbing, the tiles are showing SOME earlier point in
  // time regardless of whether the batch itself has finished, so they need
  // the same plain labels a still-running fermentation uses.
  const showFinalLabels = isComplete && scrubIndex === null;
  const chamberModeLabel = modeLabelFor(scrubbed.mode);
  const displayStage = scrubbed.ts && batch ? (stageAtTs(batch.stages, scrubbed.ts) ?? currentStage) : currentStage;
  const modeTone: PillTone =
    isComplete ? "neutral" : scrubbed.mode === "cool" ? "info" : scrubbed.mode === "heat" ? "accent" : "positive";
  const modeLabel = isComplete
    ? batch?.status === "terminated"
      ? "Terminated"
      : "Batch complete"
    : scrubbed.mode === "cool"
      ? "Cooling"
      : scrubbed.mode === "heat"
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
        <BatchTitleMenu
          batchName={batch?.name ?? "No fermentation yet"}
          isDemo={batch?.demo ?? false}
          canStartNew={canStartNew}
          cannotStartReason={cannotStartReason}
          fermentations={fermentations.data}
          currentBatchId={batchId}
          onStartNew={() => {
            setCloneFrom(null);
            setShowStartForm(true);
          }}
          onSelectBatch={(id) => setSearchParams({ batch: String(id) })}
        />

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
        <LiveStageBar
          batch={batch}
          currentStage={currentStage}
          nextStage={nextStage}
          stageReady={stageReady}
          stageIdx={stageIdx}
          lastTarget={scrubbed.target}
          onAdvance={() => advanceStage.mutate(batch.id)}
          advancePending={advanceStage.isPending}
          onSetAutoAdvance={(auto) =>
            updateStages.mutate({
              fermentationId: batch.id,
              stages: { [String(currentStage.id)]: { advance_mode: auto ? "auto" : "manual" } },
            })
          }
          onEditProfile={() => setShowEditProfile(true)}
        />
      )}

      {batch && seriesData && (
        <>
          <div className={styles.statsGrid}>
            <Card padding="sm">
              <MetricStat
                value={fmtTemp(scrubbed.beer)}
                label={showFinalLabels ? "Beer temp at end" : "Beer temp"}
                sublabel={`Target ${fmtTemp(scrubbed.target)}`}
                accent="orange"
              />
            </Card>
            <Card padding="sm">
              <MetricStat
                value={fmtTemp(scrubbed.chamber)}
                label={showFinalLabels ? "Chamber at end" : "Chamber"}
                sublabel={chamberModeLabel}
                accent="blue"
              />
            </Card>
            {scrubbed.hasGravity && (
              <Card padding="sm">
                <MetricStat
                  value={scrubbed.gravity !== null ? scrubbed.gravity.toFixed(3) : "--"}
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
                value={fmtTemp(scrubbed.target)}
                label="Setpoint"
                sublabel={`${displayStage?.name ?? ""} · ±0.5°F`}
                accent="gray"
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
