/** "Aug 9, 3:42 PM" -- the date+time label shared by the chart's scrub
 * crosshair (chart/geometry.ts's formatScrubMoment) and the live-bar's
 * stage-end projection (GettingStartedView.tsx's stageEndSentence). Both
 * used to spell out the same toLocaleDateString/toLocaleTimeString pair
 * independently -- a real wording change (e.g. adding a weekday) meant
 * remembering to touch both. */
export function formatDateTime(d: Date): string {
  const dateLabel = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const timeLabel = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${dateLabel}, ${timeLabel}`;
}

/** "1.042" -- gravity readings only ever mean anything to 3 decimal
 * places (a hydrometer/Tilt's own native resolution); applying toFixed(3)
 * at every display site keeps a smoothed/averaged reading -- which now
 * carries far more binary-floating-point digits than a raw single Tilt
 * decode ever did, see platforms/tilt/scanner.py's own
 * GRAVITY_SMOOTHING_WINDOW_S -- from leaking that extra, meaningless
 * precision into the UI. Was previously re-implemented ad hoc at each
 * display site (stageFormat.ts/LiveStageBar.tsx's own gravity formatters,
 * FermentationChart.tsx's axis ticks, HardwareSetupView.tsx,
 * TiltCalibrationDialog.tsx, GettingStartedView.tsx) -- centralized here
 * for the same reason formatDateTime is. Takes only a real number --
 * callers keep their own null-handling/fallback text, since that
 * genuinely differs by context ("—", "?", "--"); this only centralizes
 * the rounding itself. */
export function formatGravity(g: number): string {
  return g.toFixed(3);
}
