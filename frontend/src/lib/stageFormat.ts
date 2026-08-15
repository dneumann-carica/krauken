/** Fields common to a stage's end-criteria regardless of source shape --
 * StageResponse's wire numbers directly, or FermentationPlanDialog's
 * StageForm strings once run through its own `num()` parser. */
export interface StageEndCriteriaFields {
  end_mode: string;
  end_hours: number | null;
  hold_temp_f: number | null;
  hold_hours: number | null;
  gravity_hi: number | null;
  gravity_stable_hours: number | null;
}

export interface StageEndCriteriaFormat {
  /** Formats an hours quantity (end_hours/hold_hours/gravity_stable_hours),
   * including its own trailing unit -- callers differ on whether that's a
   * real elapsed-time rollup (e.g. "4d 3h") or a plain typed number still
   * being edited ("12h", or "?h" if not filled in yet). */
  hours: (h: number | null) => string;
  /** Formats a Fahrenheit temperature, including "°F". */
  tempF: (t: number | null) => string;
  /** Formats a specific-gravity value, no unit (SG is unitless). */
  gravity: (g: number | null) => string;
}

/** The stage-end-criteria clause -- "4d elapse", "beer holds 65.0°F for
 * 12h", "gravity flattens at or below 1.010 for 48h", "gravity drops below
 * 1.010 and stays there for 12h" -- shared between the live batch view's
 * stage-end projection (GettingStartedView.tsx's criteriaDescription/
 * stageEndSentence) and the plan editor's per-stage summary
 * (FermentationPlanDialog.tsx's summaryLine). Used to be two independent
 * copies whose wording had already drifted apart, one of them
 * (criteriaDescription) missing a gravity_below branch entirely and
 * silently falling through to the wrong ("gravity flattens...") text for
 * it -- a real bug this extraction fixes by construction, since there's
 * now only one branch list to get right. */
export function describeStageEndCriteria(s: StageEndCriteriaFields, fmt: StageEndCriteriaFormat): string {
  if (s.end_mode === "time") return `${fmt.hours(s.end_hours)} elapse`;
  if (s.end_mode === "temp_hold") return `beer holds ${fmt.tempF(s.hold_temp_f)} for ${fmt.hours(s.hold_hours)}`;
  if (s.end_mode === "gravity_below") {
    return `gravity drops below ${fmt.gravity(s.gravity_hi)} and stays there for ${fmt.hours(s.hold_hours)}`;
  }
  return `gravity flattens at or below ${fmt.gravity(s.gravity_hi)} for ${fmt.hours(s.gravity_stable_hours)}`;
}
