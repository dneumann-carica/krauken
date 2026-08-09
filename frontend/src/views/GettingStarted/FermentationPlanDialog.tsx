import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { useSetStageEnabled, useStartFermentation, useUpdateStages, useYeasts } from "../../api/queries";
import type { StageInput, StageResponse } from "../../api/types";
import { Button, Dialog, Switch, Tag } from "../../design/primitives";
import { buildDefaultStages } from "../../fermentation/defaultStages";
import styles from "./FermentationPlanDialog.module.css";

type TempMode = "constant" | "stepped";
type EndMode = "gravity" | "time" | "temp_hold";

interface StageForm {
  key: string;
  stageType: StageInput["stage_type"];
  name: string;
  on: boolean;
  /** Can't be switched off -- the required primary stage (new mode) or the
   * currently active stage (edit mode). */
  locked: boolean;
  /** Already happened -- every control is read-only, informational only. */
  disabled: boolean;
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

function fromStageInput(s: StageInput): StageForm {
  return {
    key: s.stage_type,
    stageType: s.stage_type,
    name: s.name,
    on: true,
    locked: s.stage_type === "primary",
    disabled: false,
    tempMode: s.temp_mode,
    tempF: s.temp_f != null ? String(s.temp_f) : "",
    tempFrom: s.temp_from_f != null ? String(s.temp_from_f) : "",
    tempTo: s.temp_to_f != null ? String(s.temp_to_f) : "",
    rampHours: s.ramp_hours != null ? String(s.ramp_hours) : "",
    endMode: s.end_mode,
    endHours: s.end_hours != null ? String(s.end_hours) : "",
    holdTemp: s.hold_temp_f != null ? String(s.hold_temp_f) : "",
    holdHours: s.hold_hours != null ? String(s.hold_hours) : "",
    gravHi: s.gravity_hi != null ? String(s.gravity_hi) : "",
    gravH: s.gravity_stable_hours != null ? String(s.gravity_stable_hours) : "",
    auto: s.advance_mode === "auto",
  };
}

function fromStageResponse(s: StageResponse): StageForm {
  const completed = s.state === "finished" || (s.state === "skipped" && s.started_at !== null);
  return {
    ...fromStageInput(s as unknown as StageInput),
    key: String(s.id),
    on: s.state !== "skipped",
    locked: s.state === "running",
    disabled: completed,
  };
}

function toStageInput(s: StageForm): StageInput {
  return {
    stage_type: s.stageType,
    name: s.name,
    temp_mode: s.tempMode,
    temp_f: s.tempMode === "constant" ? num(s.tempF) : null,
    temp_from_f: s.tempMode === "stepped" ? num(s.tempFrom) : null,
    temp_to_f: s.tempMode === "stepped" ? num(s.tempTo) : null,
    ramp_hours: s.tempMode === "stepped" ? num(s.rampHours) : null,
    end_mode: s.endMode,
    end_hours: s.endMode === "time" ? num(s.endHours) : null,
    hold_temp_f: s.endMode === "temp_hold" ? num(s.holdTemp) : null,
    hold_hours: s.endMode === "temp_hold" ? num(s.holdHours) : null,
    gravity_lo: null,
    gravity_hi: s.endMode === "gravity" ? num(s.gravHi) : null,
    gravity_stable_hours: s.endMode === "gravity" ? num(s.gravH) : null,
    advance_mode: s.auto ? "auto" : "manual",
  };
}

function summaryLine(s: StageForm): string {
  const temp = s.tempMode === "constant" ? `${s.tempF || "?"}°F` : `${s.tempFrom || "?"}→${s.tempTo || "?"}°F`;
  const end =
    s.endMode === "gravity"
      ? `ends when gravity flattens at or below ${s.gravHi || "?"} for ${s.gravH || "?"}h`
      : s.endMode === "temp_hold"
        ? `ends after holding ${s.holdTemp || "?"}°F for ${s.holdHours || "?"}h`
        : `ends when ${s.endHours || "?"}h elapse`;
  return `${temp} · ${end}`;
}

function setMode(stages: StageForm[], key: string, patch: Partial<StageForm>): StageForm[] {
  return stages.map((s) => (s.key === key ? { ...s, ...patch } : s));
}

interface StageEditorProps {
  stage: StageForm;
  onChange: (patch: Partial<StageForm>) => void;
  /** Present only when this stage is eligible to be switched on/off --
   * the new-fermentation editor's optional stages, or a not-yet-reached
   * stage in the running-profile editor. */
  onToggle?: (on: boolean) => void;
}

function StageEditor({ stage: s, onChange, onToggle }: StageEditorProps) {
  const fieldsDisabled = s.disabled;
  return (
    <div className={styles.stageCard}>
      <div className={styles.stageHead}>
        <div>
          <div className={styles.stageName}>{s.name}</div>
          <div className={styles.stageSummary}>{summaryLine(s)}</div>
        </div>
        {s.locked ? (
          <Tag tone="navy" size="sm">
            Required
          </Tag>
        ) : s.disabled ? (
          <Tag tone="gray" size="sm">
            Completed
          </Tag>
        ) : (
          onToggle && <Switch checked={s.on} onChange={onToggle} />
        )}
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
                <option value="time">After a set amount of time</option>
                <option value="temp_hold">After holding a temp</option>
              </select>
              {s.endMode === "gravity" && (
                <div className={styles.inputRow}>
                  <span className={styles.unit}>at or below</span>
                  <input
                    className={styles.input}
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

          <label className={styles.autoRow}>
            <Switch checked={s.auto} disabled={fieldsDisabled} onChange={(auto) => onChange({ auto })} />
            <span>Advance to the next stage automatically when the criteria are met</span>
          </label>
        </>
      )}
    </div>
  );
}

interface NewProps {
  mode: "new";
  open: boolean;
  onClose: () => void;
  onStarted: () => void;
}

interface EditProps {
  mode: "edit";
  open: boolean;
  onClose: () => void;
  fermentationId: number;
  stages: StageResponse[];
  onSaved: () => void;
}

type Props = NewProps | EditProps;

export function FermentationPlanDialog(props: Props) {
  const { open, onClose } = props;
  const yeasts = useYeasts();
  const startFermentation = useStartFermentation();
  const updateStages = useUpdateStages();
  const setStageEnabled = useSetStageEnabled();

  const [name, setName] = useState("");
  const [yeastId, setYeastId] = useState<string>();
  const [stages, setStages] = useState<StageForm[]>([]);
  const [error, setError] = useState<string>();

  const yeastOptions = Object.entries(yeasts.data?.yeasts ?? {});
  const selectedYeastId = yeastId ?? yeastOptions[0]?.[0];
  const selectedYeast = selectedYeastId ? yeasts.data?.yeasts[selectedYeastId] : undefined;

  useEffect(() => {
    if (!open) return;
    setError(undefined);
    if (props.mode === "edit") {
      setStages(props.stages.map(fromStageResponse));
    } else if (selectedYeast) {
      setName("");
      setStages(buildDefaultStages(selectedYeast.stage_defaults).map(fromStageInput));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, props.mode, selectedYeast]);

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
      setError(err instanceof ApiError ? err.message : "Could not update that stage.");
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
        await startFermentation.mutateAsync({
          name: name.trim(),
          yeast_id: selectedYeastId,
          yeast_name: selectedYeast?.name,
          og: null,
          stages: stages.filter((s) => s.on).map(toStageInput),
        });
        props.onStarted();
      } else {
        // Completed stages are read-only, and a switched-off stage was
        // already marked skipped the moment it was toggled (handleToggleStage)
        // -- update_stages only accepts edits to a stage that's still
        // pending or running, so only those belong in this payload.
        const patch: Record<string, Record<string, unknown>> = {};
        for (const s of stages) {
          if (s.disabled || !s.on) continue;
          const input = toStageInput(s);
          const { stage_type, ...editable } = input;
          void stage_type;
          patch[s.key] = editable;
        }
        await updateStages.mutateAsync({ fermentationId: props.fermentationId, stages: patch });
        props.onSaved();
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save this profile.");
    }
  }

  const pending = props.mode === "new" ? startFermentation.isPending : updateStages.isPending;

  return (
    <Dialog open={open} onClose={onClose}>
      <form className={styles.dialogInner} onSubmit={handleSubmit}>
        <div className={styles.header}>
          <div className={styles.title}>{props.mode === "new" ? "New fermentation profile" : "Edit running profile"}</div>
          <div className={styles.subtitle}>
            {props.mode === "new"
              ? "Primary is required. Every stage after it is optional -- switch off what you don't want."
              : "Edit stages that haven't finished yet, or switch off ones you haven't reached. Completed stages are shown for reference."}
          </div>
        </div>

        <div className={styles.content}>
          {props.mode === "new" && (
            <>
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
                  <select
                    className={styles.select}
                    value={selectedYeastId ?? ""}
                    onChange={(e) => {
                      setYeastId(e.target.value);
                      setError(undefined);
                    }}
                  >
                    {yeastOptions.map(([id, preset]) => (
                      <option key={id} value={id}>
                        {preset.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <p className={styles.ogHint}>
                Original gravity is detected automatically from your gravity sensor once fermentation starts --
                there's nothing to enter here.
              </p>
            </>
          )}

          <div className={styles.stageList}>
            {stages.map((s) => (
              <StageEditor
                key={s.key}
                stage={s}
                onChange={(patch) => patchStage(s.key, patch)}
                onToggle={(on) => handleToggleStage(s.key, on)}
              />
            ))}
          </div>
        </div>

        <div className={styles.footer}>
          {error ? (
            <span className={styles.footerError}>{error}</span>
          ) : (
            props.mode === "new" &&
            onCount > 0 && (
              <span className={styles.footerSummary}>
                {onCount} stage{onCount === 1 ? "" : "s"} &middot; {onNames.join(" → ")}
              </span>
            )
          )}
          <div className={styles.footerActions}>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={pending}>
              {pending ? "Saving…" : props.mode === "new" ? "Start fermentation" : "Save changes"}
            </Button>
          </div>
        </div>
      </form>
    </Dialog>
  );
}
