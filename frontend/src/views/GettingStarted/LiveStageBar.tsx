import type { FermentationDetail, StageResponse } from "../../api/types";
import { Button, Card, Switch, Tag } from "../../design/primitives";
import type { StageEndCriteriaFormat } from "../../lib/stageFormat";
import { describeStageEndCriteria } from "../../lib/stageFormat";
import styles from "./GettingStartedView.module.css";

// Days+hours, not a raw hour count or a fractional day -- "452h elapsed"
// isn't something a human parses at a glance, and "18.8 days" isn't much
// better.
function fmtHours(hours: number): string {
  return hours >= 24 ? `${(hours / 24).toFixed(hours % 24 === 0 ? 0 : 1)} days` : `${hours}h`;
}

function fmtTemp(v: number | null): string {
  return v === null ? "--" : `${v.toFixed(1)}°F`;
}

const CRITERIA_FORMAT: StageEndCriteriaFormat = {
  hours: (h) => fmtHours(h ?? 0),
  tempF: (t) => `${t?.toFixed(1)}°F`,
  gravity: (g) => `${g?.toFixed(3)}`,
};

function runningLine(stage: StageResponse, target: number | null): string {
  if (stage.temp_mode === "stepped" && stage.temp_from_f != null && stage.temp_to_f != null) {
    return `Stepping beer temp ${fmtTemp(stage.temp_from_f)} → ${fmtTemp(stage.temp_to_f)}`;
  }
  return `Holding beer temp at ${fmtTemp(target)}`;
}

function stageEndSentence(stage: StageResponse): string {
  const capHours = stage.max_hours ?? (stage.end_mode === "time" ? stage.end_hours : null);
  const criteria = `Ends when ${describeStageEndCriteria(stage, CRITERIA_FORMAT)}`;
  if (capHours == null || !stage.started_at) {
    return `${criteria} -- no time cap set.`;
  }
  const end = new Date(new Date(stage.started_at).getTime() + capHours * 3_600_000);
  const dateLabel = end.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const timeLabel = end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${criteria} -- projected ${dateLabel}, ${timeLabel}.`;
}

interface Props {
  batch: FermentationDetail;
  currentStage: StageResponse;
  nextStage: StageResponse | undefined;
  stageReady: boolean;
  stageIdx: number;
  lastTarget: number | null;
  onAdvance: () => void;
  advancePending: boolean;
  onSetAutoAdvance: (auto: boolean) => void;
  onEditProfile: () => void;
}

/** The live batch's current-stage status card -- what's running, why/when
 * it'll end, the auto/manual advance toggle, and the "advance now"/"edit
 * profile" actions. Only rendered while a fermentation is active. */
export function LiveStageBar({
  batch,
  currentStage,
  nextStage,
  stageReady,
  stageIdx,
  lastTarget,
  onAdvance,
  advancePending,
  onSetAutoAdvance,
  onEditProfile,
}: Props) {
  return (
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
            <Switch checked={currentStage.advance_mode === "auto"} onChange={onSetAutoAdvance} />
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
            onClick={onAdvance}
            disabled={advancePending}
          >
            Advance to {nextStage.name}
          </Button>
        )}
        <Button variant="outline" size="sm" onClick={onEditProfile}>
          Edit profile
        </Button>
      </div>
    </Card>
  );
}
