import { useEffect, useState } from "react";
import {
  apiErrorMessage,
  useInsertStage,
  useReorderStages,
  useSetStageEnabled,
  useStartFermentation,
  useUpdateStages,
  useYeasts,
} from "../../api/queries";
import type { StageInput, StageResponse } from "../../api/types";
import { Button, Combobox, Dialog, Switch, Tag } from "../../design/primitives";
import type { ComboboxOption } from "../../design/primitives";
import type { StageEndCriteriaFields, StageEndCriteriaFormat } from "../../lib/stageFormat";
import { describeStageEndCriteria } from "../../lib/stageFormat";
import styles from "./FermentationPlanDialog.module.css";

type TempMode = "constant" | "stepped";
type EndMode = "gravity" | "time" | "temp_hold" | "gravity_below";

export interface StageForm {
  key: string;
  name: string;
  on: boolean;
  /** Can't be switched off -- the first stage of a new plan (a fermentation
   * needs at least one stage) or the currently active stage (edit mode).
   * Position-based, not tied to any particular stage's identity -- stages
   * are freeform, there's no fixed "primary" slot to key off. */
  locked: boolean;
  /** Already happened -- every control is read-only, informational only. */
  disabled: boolean;
  /** The real lifecycle state behind `disabled` -- "pending" for a
   * not-yet-persisted draft. Only read to pick the disabled tag's wording
   * (Finished vs. Skipped); everything else uses on/locked/disabled. */
  rawState: string;
  tempMode: TempMode;
  tempF: string;
  tempFrom: string;
  tempTo: string;
  rampHours: string;
  endMode: EndMode;
  endHours: string;
  holdTemp: string;
  holdHours: string;
  gravHi: string;
  gravH: string;
  auto: boolean;
}

function num(s: string): number | null {
  return s === "" ? null : Number(s);
}

// A fresh, not-yet-persisted draft stage needs SOME unique local key to
// react-key/identify it by before a real DB id exists (see fromStageInput's
// own comment below) -- crypto.randomUUID() is overkill for that and, worse,
// unavailable outside a secure context (hit a real user: the vite dev server
// reached over a plain-HTTP LAN address, not localhost, throws
// "crypto.randomUUID is not a function" there instead of returning one).
// This key is never sent to the server and never compared across sessions,
// so a plain incrementing counter is exactly as unique as this needs to be,
// with no availability dependency at all -- see this file's own test for a
// regression pin that calls this with crypto.randomUUID deleted entirely.
let nextLocalStageKeySeq = 0;
export function newLocalStageKey(): string {
  nextLocalStageKeySeq += 1;
  return `local-${nextLocalStageKeySeq}`;
}

/** `key`/`index` are only meaningful for a not-yet-created draft stage (the
 * "new fermentation" flow, before there's a real DB id) -- fromStageResponse
 * below overrides both with the stage's real id and its running-state-based
 * lock once one exists. */
function fromStageInput(s: StageInput, key: string, index: number): StageForm {
  return {
    key,
    name: s.name,
    on: true,
    locked: index === 0,
    disabled: false,
    rawState: "pending",
    tempMode: s.temp_mode,
    tempF: s.temp_f != null ? String(s.temp_f) : "",
    tempFrom: s.temp_from_f != null ? String(s.temp_from_f) : "",
    tempTo: s.temp_to_f != null ? String(s.temp_to_f) : "",
    rampHours: s.ramp_hours != null ? String(s.ramp_hours) : "",
    endMode: s.end_mode,
    endHours: s.end_hours != null ? String(s.end_hours) : "",
    holdTemp: s.hold_temp_f != null ? String(s.hold_temp_f) : "",
    holdHours: s.hold_hours != null ? String(s.hold_hours) : "",
    // toFixed(3), not String() -- a stored 1.040 is indistinguishable from
    // 1.04 as a JS number (no trailing zero to preserve), so a bare
    // String() conversion silently drops it. Gravity is always shown to
    // 3 decimal places elsewhere in this app (see GettingStartedView's
    // criteriaDescription); this is the one spot that wasn't doing that.
    gravHi: s.gravity_hi != null ? s.gravity_hi.toFixed(3) : "",
    gravH: s.gravity_stable_hours != null ? String(s.gravity_stable_hours) : "",
    auto: s.advance_mode === "auto",
  };
}

/** StageResponse's wire shape widens temp_mode/end_mode/advance_mode to
 * plain `string` (whatever the backend returns) where StageInput's authoring
 * shape narrows them to real literal unions -- an explicit field-by-field
 * copy instead of a blind `as unknown as StageInput` cast, so only those
 * three fields need a (narrow, well-scoped) assertion instead of silently
 * bypassing every field's shape checking. */
function stageResponseToStageInput(s: StageResponse): StageInput {
  return {
    name: s.name,
    temp_mode: s.temp_mode as StageInput["temp_mode"],
    temp_f: s.temp_f,
    temp_from_f: s.temp_from_f,
    temp_to_f: s.temp_to_f,
    ramp_hours: s.ramp_hours,
    end_mode: s.end_mode as StageInput["end_mode"],
    end_hours: s.end_hours,
    hold_temp_f: s.hold_temp_f,
    hold_hours: s.hold_hours,
    gravity_hi: s.gravity_hi,
    gravity_stable_hours: s.gravity_stable_hours,
    min_hours: s.min_hours,
    max_hours: s.max_hours,
    advance_mode: s.advance_mode as StageInput["advance_mode"],
  };
}

function fromStageResponse(s: StageResponse): StageForm {
  const completed = s.state === "finished" || (s.state === "skipped" && s.started_at !== null);
  return {
    ...fromStageInput(stageResponseToStageInput(s), String(s.id), -1),
    key: String(s.id),
    on: s.state !== "skipped",
    locked: s.state === "running",
    disabled: completed,
    rawState: s.state,
  };
}

function toStageInput(s: StageForm): StageInput {
  return {
    name: s.name,
    temp_mode: s.tempMode,
    temp_f: s.tempMode === "constant" ? num(s.tempF) : null,
    temp_from_f: s.tempMode === "stepped" ? num(s.tempFrom) : null,
    temp_to_f: s.tempMode === "stepped" ? num(s.tempTo) : null,
    ramp_hours: s.tempMode === "stepped" ? num(s.rampHours) : null,
    end_mode: s.endMode,
    end_hours: s.endMode === "time" ? num(s.endHours) : null,
    hold_temp_f: s.endMode === "temp_hold" ? num(s.holdTemp) : null,
    // hold_hours is shared with temp_hold -- both mean "how long a
    // condition must hold continuously," just applied to different things.
    hold_hours: s.endMode === "temp_hold" || s.endMode === "gravity_below" ? num(s.holdHours) : null,
    // gravity_hi is shared with "gravity" -- both mean "at or below this
    // value"; gravity_below just doesn't also require flatness.
    gravity_hi: s.endMode === "gravity" || s.endMode === "gravity_below" ? num(s.gravHi) : null,
    gravity_stable_hours: s.endMode === "gravity" ? num(s.gravH) : null,
    advance_mode: s.auto ? "auto" : "manual",
  };
}

const FORM_CRITERIA_FORMAT: StageEndCriteriaFormat = {
  hours: (h) => (h != null ? `${h}h` : "?h"),
  tempF: (t) => (t != null ? `${t.toFixed(1)}°F` : "?°F"),
  gravity: (g) => (g != null ? g.toFixed(3) : "?"),
};

function summaryLine(s: StageForm): string {
  const temp = s.tempMode === "constant" ? `${s.tempF || "?"}°F` : `${s.tempFrom || "?"}→${s.tempTo || "?"}°F`;
  const criteria: StageEndCriteriaFields = {
    end_mode: s.endMode,
    end_hours: num(s.endHours),
    hold_temp_f: num(s.holdTemp),
    hold_hours: num(s.holdHours),
    gravity_hi: num(s.gravHi),
    gravity_stable_hours: num(s.gravH),
  };
  return `${temp} · ends when ${describeStageEndCriteria(criteria, FORM_CRITERIA_FORMAT)}`;
}

function setMode(stages: StageForm[], key: string, patch: Partial<StageForm>): StageForm[] {
  return stages.map((s) => (s.key === key ? { ...s, ...patch } : s));
}

/** A freshly-added stage's starting point -- continues at whatever the
 * previous stage's own temperature ends on, so it doesn't open with a
 * jarring, disconnected default. No brewing logic beyond that (no implied
 * stage "type", no suggested end criteria beyond a plain 24h timer) --
 * the user shapes it from here via the same fields every other stage has.
 * Exported (like geometry.ts's pure functions) so the reorder/add logic is
 * directly unit-testable without rendering the whole dialog. */
export function blankStage(continuesFromF: number | null): StageInput {
  return {
    name: "New stage",
    temp_mode: "constant",
    temp_f: continuesFromF ?? 68,
    end_mode: "time",
    end_hours: 24,
    advance_mode: "auto",
  };
}

export function lastTempF(s: StageForm | undefined): number | null {
  if (!s) return null;
  return s.tempMode === "constant" ? num(s.tempF) : num(s.tempTo);
}

/** Only the stages eligible to move at all (not locked, not already
 * decided) actually change position -- everything else stays exactly
 * where it was. `newOrder` is `all`'s own reorderable subset, permuted;
 * walking `all` in its original order and substituting from `newOrder`
 * wherever the original slot was reorderable keeps every locked/disabled
 * stage's absolute position untouched regardless of how the reorderable
 * ones are interleaved among them. */
export function applyReorder(all: StageForm[], newOrder: StageForm[]): StageForm[] {
  let i = 0;
  return all.map((s) => (!s.locked && !s.disabled ? newOrder[i++] : s));
}

export function swapAdjacent<T>(arr: T[], i: number, j: number): T[] {
  const copy = [...arr];
  [copy[i], copy[j]] = [copy[j], copy[i]];
  return copy;
}

interface StageEditorProps {
  stage: StageForm;
  onChange: (patch: Partial<StageForm>) => void;
  /** Present only when this stage is eligible to be switched on/off -- the
   * running-profile editor's not-yet-reached stages (skip/re-enable is
   * that editor's only removal mechanism -- there's no row to delete). */
  onToggle?: (on: boolean) => void;
  /** Present only in the new-fermentation editor, and only on a non-first
   * stage -- freeform stages can just be deleted outright before anything
   * exists to skip. */
  onRemove?: () => void;
  /** Each present iff this stage can move that direction -- absent (not
   * just disabled) at either end of the reorderable run, and always
   * absent on a locked/disabled stage. */
  onMoveUp?: () => void;
  onMoveDown?: () => void;
  /** True for the last stage in the plan's actual sequence (not just the
   * last reorderable one) -- there's nothing after it to advance TO, so
   * the auto/manual advance toggle doesn't apply and isn't shown. */
  isLastStage: boolean;
}

function StageEditor({ stage: s, onChange, onToggle, onRemove, onMoveUp, onMoveDown, isLastStage }: StageEditorProps) {
  const fieldsDisabled = s.disabled;
  const canMove = onMoveUp !== undefined || onMoveDown !== undefined;
  // Names are free text and there's no longer a fixed set of stage
  // identities to pick from (see the freeform-stages migration) -- click
  // to rename is the only way to give a freshly-added stage a real name at
  // all, not just a nice-to-have for the shipped presets' stages.
  const [editingName, setEditingName] = useState(false);
  return (
    <div className={styles.stageCard}>
      <div className={styles.stageHead}>
        <div>
          {editingName ? (
            <input
              className={styles.stageNameInput}
              autoFocus
              value={s.name}
              onChange={(e) => onChange({ name: e.target.value })}
              onBlur={() => setEditingName(false)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  setEditingName(false);
                }
              }}
            />
          ) : (
            <button
              type="button"
              className={styles.stageNameButton}
              disabled={fieldsDisabled}
              onClick={() => setEditingName(true)}
            >
              {s.name}
            </button>
          )}
          <div className={styles.stageSummary}>{summaryLine(s)}</div>
        </div>
        <div className={styles.stageActions}>
          {canMove && (
            <div className={styles.moveButtons}>
              <Button type="button" variant="outline" size="sm" className={styles.iconButton} disabled={!onMoveUp} onClick={onMoveUp} aria-label="Move stage earlier">
                ↑
              </Button>
              <Button type="button" variant="outline" size="sm" className={styles.iconButton} disabled={!onMoveDown} onClick={onMoveDown} aria-label="Move stage later">
                ↓
              </Button>
            </div>
          )}
          {s.locked ? (
            <Tag tone="navy" size="sm">
              {/* locked is true for two different reasons (see StageForm's
                  own doc comment above) -- rawState is what actually tells
                  them apart: "running" only ever happens on an existing,
                  already-started fermentation's current stage (fromStageResponse),
                  never on a new plan's first stage (fromStageInput always
                  stamps "pending" there, even though it's locked too). Says
                  "Active" so it reads as "can't remove this, it's currently
                  executing" rather than the generic "Required" a first-time
                  planner sees before anything's even started. */}
              {s.rawState === "running" ? "Active" : "Required"}
            </Tag>
          ) : s.disabled ? (
            <Tag tone="gray" size="sm">
              {s.rawState === "skipped" ? "Skipped" : "Finished"}
            </Tag>
          ) : onRemove ? (
            <Button type="button" variant="outline" size="sm" className={styles.iconButton} onClick={onRemove} aria-label="Remove stage">
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <path
                  d="M3 4.5h10M6.5 4.5V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1.5M4.5 4.5l.6 8.4a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8.4M6.5 7v4M9.5 7v4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </Button>
          ) : (
            onToggle && <Switch checked={s.on} onChange={onToggle} />
          )}
        </div>
      </div>

      {s.on && (
        <>
          <div className={styles.fieldGrid}>
            <div className={styles.fieldCol}>
              <span className={styles.fieldLabel}>Beer temperature</span>
              <select
                className={styles.select}
                value={s.tempMode}
                disabled={fieldsDisabled}
                onChange={(e) => onChange({ tempMode: e.target.value as TempMode })}
              >
                <option value="constant">Hold a constant temp</option>
                <option value="stepped">Step from one temp to another</option>
              </select>
              {s.tempMode === "constant" ? (
                <div className={styles.inputRow}>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.tempF}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ tempF: e.target.value })}
                  />
                  <span className={styles.unit}>°F</span>
                </div>
              ) : (
                <div className={styles.inputRow}>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.tempFrom}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ tempFrom: e.target.value })}
                  />
                  <span className={styles.unit}>→</span>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.tempTo}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ tempTo: e.target.value })}
                  />
                  <span className={styles.unit}>°F over</span>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.rampHours}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ rampHours: e.target.value })}
                  />
                  <span className={styles.unit}>h</span>
                </div>
              )}
            </div>

            <div className={styles.fieldCol}>
              <span className={styles.fieldLabel}>Stage ends</span>
              <select
                className={styles.select}
                value={s.endMode}
                disabled={fieldsDisabled}
                onChange={(e) => onChange({ endMode: e.target.value as EndMode })}
              >
                <option value="gravity">When gravity flattens</option>
                <option value="gravity_below">When gravity drops below a value</option>
                <option value="time">After a set amount of time</option>
                <option value="temp_hold">After holding a temp</option>
              </select>
              {s.endMode === "gravity" && (
                <div className={styles.inputRow}>
                  <span className={styles.unit}>at or below</span>
                  <input
                    className={`${styles.input} ${styles.gravityInput}`}
                    type="number"
                    step="0.001"
                    value={s.gravHi}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ gravHi: e.target.value })}
                  />
                  <span className={styles.unit}>flat for</span>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.gravH}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ gravH: e.target.value })}
                  />
                  <span className={styles.unit}>h</span>
                </div>
              )}
              {s.endMode === "gravity_below" && (
                <div className={styles.inputRow}>
                  <span className={styles.unit}>drops below</span>
                  <input
                    className={`${styles.input} ${styles.gravityInput}`}
                    type="number"
                    step="0.001"
                    value={s.gravHi}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ gravHi: e.target.value })}
                  />
                  <span className={styles.unit}>and stays there for</span>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.holdHours}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ holdHours: e.target.value })}
                  />
                  <span className={styles.unit}>h</span>
                </div>
              )}
              {s.endMode === "time" && (
                <div className={styles.inputRow}>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.endHours}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ endHours: e.target.value })}
                  />
                  <span className={styles.unit}>h</span>
                </div>
              )}
              {s.endMode === "temp_hold" && (
                <div className={styles.inputRow}>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.holdTemp}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ holdTemp: e.target.value })}
                  />
                  <span className={styles.unit}>°F for</span>
                  <input
                    className={styles.input}
                    type="number"
                    value={s.holdHours}
                    disabled={fieldsDisabled}
                    onChange={(e) => onChange({ holdHours: e.target.value })}
                  />
                  <span className={styles.unit}>h</span>
                </div>
              )}
            </div>
          </div>

          {!isLastStage && (
            <label className={styles.autoRow}>
              <Switch checked={s.auto} disabled={fieldsDisabled} onChange={(auto) => onChange({ auto })} />
              <span>Advance to the next stage automatically when the criteria are met</span>
            </label>
          )}
        </>
      )}
    </div>
  );
}

/** What both "clone an existing batch's plan into a new one" and "view a
 * finished batch's plan read-only" need -- just enough to seed the stage
 * list and, for cloning, the yeast dropdown. Not a full FermentationDetail:
 * this dialog has no use for og/status/timestamps/etc. */
export interface CloneSource {
  name: string;
  yeast_id: string | null;
  stages: StageResponse[];
}

interface NewProps {
  mode: "new";
  open: boolean;
  onClose: () => void;
  onStarted: (fermentationId: number) => void;
  /** Set to seed the plan from an existing batch's stages/yeast instead of
   * a yeast preset's defaults -- "Clone as new batch." Absent/null for an
   * ordinary "Start a new fermentation." */
  cloneFrom?: CloneSource | null;
}

interface EditProps {
  mode: "edit";
  open: boolean;
  onClose: () => void;
  fermentationId: number;
  stages: StageResponse[];
  onSaved: () => void;
  // No onClone here, deliberately -- a fermentation is always active while
  // this dialog is reachable (it's the LIVE-batch editor; a finished batch
  // gets the read-only "view" dialog instead), and the daemon refuses to
  // start a second fermentation while one is already running (see
  // daemon/fermentation.py's FermentationAlreadyActive). Cloning only ever
  // makes sense from ViewProps, once the source batch has actually ended.
}

interface ViewProps {
  mode: "view";
  open: boolean;
  onClose: () => void;
  fermentation: CloneSource;
  /** "Clone as new batch" from here -- the caller closes this dialog and
   * opens a "new" one with cloneFrom set to the same fermentation. */
  onClone: () => void;
}

type Props = NewProps | EditProps | ViewProps;

export function FermentationPlanDialog(props: Props) {
  const { open, onClose } = props;
  const yeasts = useYeasts();
  const startFermentation = useStartFermentation();
  const updateStages = useUpdateStages();
  const setStageEnabled = useSetStageEnabled();
  const insertStage = useInsertStage();
  const reorderStages = useReorderStages();

  const [name, setName] = useState("");
  const [yeastId, setYeastId] = useState<string>();
  const [stages, setStages] = useState<StageForm[]>([]);
  const [error, setError] = useState<string>();

  const yeastOptions = Object.entries(yeasts.data?.yeasts ?? {});
  // Fixed category render/search order, not alphabetical -- most-common-
  // first (Ale, Lager) beats "Belgian/Saison" sorting ahead of "Ale".
  // Custom always last regardless of how many real strains get added
  // later. Combobox appends anything present but unlisted here (an
  // unrecognized category shouldn't happen, but a preset silently
  // vanishing from its own picker on a typo would be a worse failure mode
  // than an extra trailing group).
  const YEAST_CATEGORY_ORDER = ["Ale", "Lager", "Belgian/Saison", "Wheat/Hefeweizen", "Kveik", "Custom"];
  const yeastComboOptions: ComboboxOption[] = yeastOptions.map(([id, preset]) => ({
    id,
    label: preset.name,
    group: preset.category ?? "Custom",
  }));
  const selectedYeastId = yeastId ?? yeastOptions[0]?.[0];
  const selectedYeast = selectedYeastId ? yeasts.data?.yeasts[selectedYeastId] : undefined;
  const cloneFrom = props.mode === "new" ? props.cloneFrom : undefined;

  useEffect(() => {
    if (!open) return;
    setError(undefined);
    if (props.mode === "edit") {
      setStages(props.stages.map(fromStageResponse));
    } else if (props.mode === "view") {
      // Read-only regardless of each stage's real state (a terminated
      // batch can still have stray 'pending' stages it never got to --
      // see daemon/fermentation.py's terminate_fermentation, which only
      // resolves the one stage that was actually running). locked:false
      // too -- "Required" is a forward-looking planning constraint,
      // meaningless for a plan that already ran its course.
      setStages(props.fermentation.stages.map((s) => ({ ...fromStageResponse(s), locked: false, disabled: true })));
    } else if (cloneFrom) {
      setName(`${cloneFrom.name} (copy)`);
      setYeastId(cloneFrom.yeast_id ?? undefined);
      setStages(cloneFrom.stages.map((s, i) => fromStageInput(stageResponseToStageInput(s), String(i), i)));
    } else if (selectedYeast) {
      setName("");
      setStages(selectedYeast.default_stages.map((s, i) => fromStageInput(s, String(i), i)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, props.mode, selectedYeast, cloneFrom]);

  const onCount = stages.filter((s) => s.on).length;
  const onNames = stages.filter((s) => s.on).map((s) => s.name);

  function patchStage(key: string, patch: Partial<StageForm>) {
    setStages((prev) => setMode(prev, key, patch));
    setError(undefined);
  }

  async function handleToggleStage(key: string, on: boolean) {
    patchStage(key, { on }); // optimistic -- reverted below on failure
    if (props.mode !== "edit") return;
    try {
      await setStageEnabled.mutateAsync({ fermentationId: props.fermentationId, stageId: Number(key), enabled: on });
    } catch (err) {
      patchStage(key, { on: !on });
      setError(apiErrorMessage(err, "Could not update that stage."));
    }
  }

  function handleRemoveStage(key: string) {
    setStages((prev) => prev.filter((s) => s.key !== key));
    setError(undefined);
  }

  async function handleAddStage() {
    setError(undefined);
    const stage = blankStage(lastTempF(stages[stages.length - 1]));
    if (props.mode !== "edit") {
      setStages((prev) => [...prev, fromStageInput(stage, newLocalStageKey(), prev.length)]);
      return;
    }
    try {
      const afterStageId = stages.length > 0 ? Number(stages[stages.length - 1].key) : undefined;
      const result = await insertStage.mutateAsync({ fermentationId: props.fermentationId, stage, afterStageId });
      setStages((prev) => [...prev, fromStageInput(stage, String(result.stage_id), prev.length)]);
    } catch (err) {
      setError(apiErrorMessage(err, "Could not add a stage."));
    }
  }

  async function handleMoveStage(key: string, direction: -1 | 1) {
    const reorderable = stages.filter((s) => !s.locked && !s.disabled);
    const idx = reorderable.findIndex((s) => s.key === key);
    const swapIdx = idx + direction;
    if (idx < 0 || swapIdx < 0 || swapIdx >= reorderable.length) return;

    const newOrder = swapAdjacent(reorderable, idx, swapIdx);
    const prevStages = stages;
    setStages(applyReorder(stages, newOrder));
    setError(undefined);
    if (props.mode !== "edit") return; // new-mode order is purely local until submit
    try {
      await reorderStages.mutateAsync({
        fermentationId: props.fermentationId, stageIds: newOrder.map((s) => Number(s.key)),
      });
    } catch (err) {
      setStages(prevStages);
      setError(apiErrorMessage(err, "Could not reorder stages."));
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(undefined);
    try {
      if (props.mode === "new") {
        if (!name.trim()) {
          setError("Give this batch a name.");
          return;
        }
        const result = await startFermentation.mutateAsync({
          name: name.trim(),
          yeast_id: selectedYeastId,
          yeast_name: selectedYeast?.name,
          og: null,
          stages: stages.filter((s) => s.on).map(toStageInput),
        });
        props.onStarted(result.fermentation_id);
      } else if (props.mode === "edit") {
        // Completed stages are read-only, and a switched-off stage was
        // already marked skipped the moment it was toggled (handleToggleStage)
        // -- update_stages only accepts edits to a stage that's still
        // pending or running, so only those belong in this payload.
        const patch: Record<string, Record<string, unknown>> = {};
        for (const s of stages) {
          if (s.disabled || !s.on) continue;
          patch[s.key] = toStageInput(s) as unknown as Record<string, unknown>;
        }
        await updateStages.mutateAsync({ fermentationId: props.fermentationId, stages: patch });
        props.onSaved();
      }
      // "view" mode has no submit-type button in its footer, so this
      // branch is unreachable for it -- no case needed, just no crash.
    } catch (err) {
      setError(apiErrorMessage(err, "Could not save this profile."));
    }
  }

  const pending = props.mode === "new" ? startFermentation.isPending : props.mode === "edit" ? updateStages.isPending : false;

  return (
    <Dialog open={open} onClose={onClose}>
      <form className={styles.dialogInner} onSubmit={handleSubmit}>
        <div className={styles.header}>
          <div className={styles.title}>
            {props.mode === "new" ? "New fermentation profile" : props.mode === "view" ? "Fermentation profile" : "Edit running profile"}
          </div>
          <div className={styles.subtitle}>
            {props.mode === "new"
              ? "The first stage is required. Add, remove, or reorder any stage after it."
              : props.mode === "view"
                ? "Read-only -- this batch has already finished."
                : "Edit, add, reorder, or switch off stages that haven't finished yet. Completed stages are shown for reference."}
          </div>
        </div>

        <div className={styles.content}>
          {props.mode === "new" && (
            <div className={styles.topRow}>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Batch name</span>
                <input
                  className={styles.input}
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    setError(undefined);
                  }}
                  placeholder="New batch"
                />
              </div>
              <div className={styles.field}>
                <span className={styles.fieldLabel}>Yeast</span>
                <Combobox
                  options={yeastComboOptions}
                  groupOrder={YEAST_CATEGORY_ORDER}
                  value={selectedYeastId}
                  placeholder="Search yeasts…"
                  onChange={(id) => {
                    setYeastId(id);
                    setError(undefined);
                  }}
                />
              </div>
            </div>
          )}

          <div className={styles.stageList}>
            {(() => {
              const reorderableKeys = stages.filter((r) => !r.locked && !r.disabled).map((r) => r.key);
              return stages.map((s, i) => {
                const rIdx = reorderableKeys.indexOf(s.key);
                const canReorder = rIdx !== -1;
                return (
                  <StageEditor
                    key={s.key}
                    stage={s}
                    onChange={(patch) => patchStage(s.key, patch)}
                    onToggle={props.mode === "edit" ? (on) => handleToggleStage(s.key, on) : undefined}
                    onRemove={props.mode === "new" && !s.locked ? () => handleRemoveStage(s.key) : undefined}
                    onMoveUp={canReorder && rIdx > 0 ? () => handleMoveStage(s.key, -1) : undefined}
                    onMoveDown={
                      canReorder && rIdx < reorderableKeys.length - 1 ? () => handleMoveStage(s.key, 1) : undefined
                    }
                    isLastStage={i === stages.length - 1}
                  />
                );
              });
            })()}
          </div>

          {props.mode !== "view" && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleAddStage}
              disabled={props.mode === "edit" && insertStage.isPending}
            >
              + Add stage
            </Button>
          )}
        </div>

        <div className={styles.footer}>
          {error ? (
            <span className={styles.footerError}>{error}</span>
          ) : (
            (props.mode === "new" || props.mode === "view") &&
            onCount > 0 && (
              <span className={styles.footerSummary}>
                {onCount} stage{onCount === 1 ? "" : "s"} &middot; {onNames.join(" → ")}
              </span>
            )
          )}
          <div className={styles.footerActions}>
            {props.mode === "view" ? (
              <>
                <Button type="button" variant="ghost" onClick={onClose}>
                  Close
                </Button>
                <Button type="button" variant="primary" onClick={props.onClone}>
                  Clone as new batch
                </Button>
              </>
            ) : (
              <>
                <Button type="button" variant="ghost" onClick={onClose}>
                  Cancel
                </Button>
                <Button type="submit" variant="primary" disabled={pending}>
                  {pending ? "Saving…" : props.mode === "new" ? "Start fermentation" : "Save changes"}
                </Button>
              </>
            )}
          </div>
        </div>
      </form>
    </Dialog>
  );
}
