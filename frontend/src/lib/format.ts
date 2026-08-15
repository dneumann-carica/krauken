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
