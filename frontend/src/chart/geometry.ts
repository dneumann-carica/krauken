// Pure functions only -- no React, no DOM. These are the genuinely
// bug-prone part of the chart (scales, bucketing, path-building with gap
// breaks), kept testable in isolation; the React components are thin
// renderers over what these return.

export interface PlotInsets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

// 1.000 is the lowest physically possible SG (pure water) -- the floor
// used to sit at 1.008, which clipped any reading at or below the
// simulator's terminal gravity (1.005, see plant.py's GravityParams).
export const GRAVITY_MIN = 1.0;
export const GRAVITY_MAX = 1.056;

/** Linear time scale: maps an ISO timestamp to an x pixel position. */
export function timeScale(tsList: string[], plotWidth: number): (ts: string) => number {
  if (tsList.length === 0) return () => 0;
  const times = tsList.map((t) => new Date(t).getTime());
  const min = times[0];
  const max = times[times.length - 1];
  const span = max - min || 1;
  return (ts: string) => ((new Date(ts).getTime() - min) / span) * plotWidth;
}

/** The classic "nice numbers" step ladder (1/2/5 x a power of 10), scaled
 * to keep roughly 4-8 ticks regardless of span -- unlike a ladder that
 * caps at a fixed max step, this keeps working for a span of any
 * magnitude, so a degenerate upstream value (e.g. a numerically unstable
 * projection) can never force the tick loop into a near-unbounded number
 * of iterations. See FermentationChart.tsx's tempTicks for the loop this
 * protects, and plant.py's advance_physics for the actual bug (an Euler
 * step that diverged to ~1e200) this was hardening against. */
function niceStep(rawStep: number): number {
  if (!Number.isFinite(rawStep) || rawStep <= 0) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const fraction = rawStep / magnitude;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return niceFraction * magnitude;
}

/** Auto-scaled temp axis: data range padded by a couple of degrees, y
 * inverted (SVG y grows downward). */
export function tempScale(
  values: (number | null)[],
  plotHeight: number,
  padding = 2,
): { toY: (v: number) => number; min: number; max: number; tickStep: number } {
  const present = values.filter((v): v is number => v !== null && Number.isFinite(v));
  const dataMin = present.length ? Math.min(...present) : 60;
  const dataMax = present.length ? Math.max(...present) : 70;
  const min = Math.floor(dataMin - padding);
  const max = Math.ceil(dataMax + padding);
  const span = max - min || 1;
  const toY = (v: number) => plotHeight - ((v - min) / span) * plotHeight;
  const tickStep = niceStep(span / 6);
  return { toY, min, max, tickStep };
}

/** Gravity axis is fixed (1.008-1.056), not auto-scaled -- per the design,
 * so the axis doesn't jump around across different batches. */
export function gravityScale(plotHeight: number): (g: number) => number {
  const span = GRAVITY_MAX - GRAVITY_MIN;
  return (g: number) => plotHeight - ((g - GRAVITY_MIN) / span) * plotHeight;
}

/** X-axis tick spacing ladder, in hours, tuned so a chart never shows an
 * unreadable number of ticks regardless of the batch's total span. */
export function xTickStep(spanHours: number, maxTicks = 10): number {
  if (spanHours <= 26) return spanHours <= 13 ? 4 : 6;
  if (spanHours <= 80) return 12;
  return 24 * Math.ceil(spanHours / 24 / maxTicks);
}

/** Formats a sample timestamp for the scrub crosshair's on-chart label --
 * the same spot the "NOW" label uses, so it reads as "this is what point
 * in time the tiles above are currently showing," not a second, separate
 * caption competing for space. */
export function formatScrubMoment(ts: string): string {
  const d = new Date(ts);
  const dateLabel = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const timeLabel = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${dateLabel}, ${timeLabel}`;
}

/** Binary search for the index of the sample whose x position (from an
 * xs array already run through timeScale) is closest to a given pixel
 * position -- the scrub/crosshair feature's hit-testing. xs must be
 * sorted ascending (true of anything built from timeScale over
 * chronological timestamps). Assumes xs.length > 0. */
export function nearestIndex(xs: number[], x: number): number {
  if (xs.length === 1) return 0;
  let lo = 0;
  let hi = xs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  // lo is the first index with xs[lo] >= x -- compare it against its
  // immediate predecessor to find whichever is genuinely closer.
  if (lo > 0 && Math.abs(xs[lo - 1] - x) <= Math.abs(xs[lo] - x)) return lo - 1;
  return lo;
}

export interface SeriesPoint {
  x: number;
  y: number;
}

/** Builds an SVG path string from parallel x/value arrays, breaking the
 * path (starting a new "M" segment) at any null value or across a
 * reported gap -- a real data gap or a missing reading must never be
 * silently interpolated across as a straight line. */
export function buildLinePath(
  xs: number[],
  values: (number | null)[],
  toY: (v: number) => number,
  breakAt: Set<number> = new Set(),
): string {
  let d = "";
  let drawing = false;
  for (let i = 0; i < xs.length; i++) {
    const v = values[i];
    if (v === null || breakAt.has(i)) {
      drawing = false;
      continue;
    }
    const cmd = drawing ? "L" : "M";
    d += `${cmd}${xs[i].toFixed(1)},${toY(v).toFixed(1)} `;
    drawing = true;
  }
  return d.trim();
}

/** One dotted bridge segment per reported gap, connecting the last sample
 * before it to the first sample after -- deliberately a SEPARATE path from
 * buildLinePath's, not a fill-in for the break it makes. buildLinePath's
 * gap must stay a real break (see its docstring: never silently
 * interpolated as if it were an ordinary line) -- this is a visibly
 * distinct "we don't know what happened here" bridge across it, styled
 * with its own dash pattern so it can never be mistaken for either real
 * data or the forward projection (contracts/projection.py's "preview, not
 * prediction" dashing). Skips a gap if either endpoint is missing from
 * tsList or its value is null -- nothing to bridge from/to. */
export function buildGapPaths(
  xs: number[],
  values: (number | null)[],
  toY: (v: number) => number,
  tsList: string[],
  gaps: { from: string; to: string }[],
): string {
  let d = "";
  for (const gap of gaps) {
    const i0 = tsList.indexOf(gap.from);
    const i1 = tsList.indexOf(gap.to);
    if (i0 < 0 || i1 < 0) continue;
    const v0 = values[i0];
    const v1 = values[i1];
    if (v0 === null || v1 === null) continue;
    d += `M${xs[i0].toFixed(1)},${toY(v0).toFixed(1)} L${xs[i1].toFixed(1)},${toY(v1).toFixed(1)} `;
  }
  return d.trim();
}

/** Indices where the gap array (server-computed, from/to timestamps
 * matching entries in tsList) falls -- used by buildLinePath to break the
 * line exactly where the sampler reports the daemon actually stopped. */
export function gapBreakIndices(tsList: string[], gaps: { from: string; to: string }[]): Set<number> {
  const breaks = new Set<number>();
  for (const gap of gaps) {
    const idx = tsList.indexOf(gap.to);
    if (idx > 0) breaks.add(idx);
  }
  return breaks;
}

export interface DutyColumn {
  x: number;
  width: number;
  coolFrac: number;
  heatFrac: number;
  idleFrac: number;
  hasData: boolean;
}

/** Buckets the duty cycle into fixed-width pixel columns for the wash
 * effect -- this is what makes the shading read correctly at any zoom
 * level, since a fixed number of buckets (not one rectangle per sample)
 * means dense and sparse sampling regions look the same visually.
 *
 * Each sample-to-sample interval's duration is distributed PROPORTIONALLY
 * across every bucket it overlaps, not just the bucket containing its
 * start point -- with on-change sampling, a single interval (e.g. a long
 * idle stretch overnight) routinely spans many buckets, so crediting only
 * the first one would leave every other bucket it passes through reading
 * as "no data" even though the mode is known for that whole span. */
export function dutyColumns(
  xs: number[],
  modes: string[],
  plotWidth: number,
  bucketPx = 3,
): DutyColumn[] {
  if (xs.length === 0) return [];
  const bucketCount = Math.max(1, Math.ceil(plotWidth / bucketPx));
  const counts = Array.from({ length: bucketCount }, () => ({ cool: 0, heat: 0, idle: 0, total: 0 }));

  for (let i = 0; i < xs.length - 1; i++) {
    const x0 = xs[i];
    const x1 = xs[i + 1];
    if (x1 <= x0) continue;
    const mode = modes[i];
    const startBucket = Math.max(0, Math.floor(x0 / bucketPx));
    const endBucket = Math.min(bucketCount - 1, Math.floor((x1 - 1e-6) / bucketPx));
    for (let b = startBucket; b <= endBucket; b++) {
      const bucketStart = b * bucketPx;
      const bucketEnd = bucketStart + bucketPx;
      const overlap = Math.min(x1, bucketEnd) - Math.max(x0, bucketStart);
      if (overlap <= 0) continue;
      const bucket = counts[b];
      if (mode === "cool") bucket.cool += overlap;
      else if (mode === "heat") bucket.heat += overlap;
      else bucket.idle += overlap;
      bucket.total += overlap;
    }
  }

  return counts.map((c, i) => ({
    x: i * bucketPx,
    width: bucketPx,
    coolFrac: c.total ? c.cool / c.total : 0,
    heatFrac: c.total ? c.heat / c.total : 0,
    idleFrac: c.total ? c.idle / c.total : 0,
    hasData: c.total > 0,
  }));
}

export interface RibbonSegment {
  x: number;
  width: number;
  label: string;
  /** Short duration readout for this segment, e.g. "18h" or "4d". */
  span: string;
  stageId: number;
  /** Currently running. */
  active: boolean;
  /** Reached a terminal state (finished/skipped), not currently running. */
  done: boolean;
  /** The last segment of a batch that is no longer active -- gets its own
   * "wrapped up" tone distinct from an ordinary done-and-passed stage. */
  ended: boolean;
}

function formatSpan(hours: number): string {
  return hours >= 48 ? `${Math.round(hours / 24)}d` : `${Math.round(hours)}h`;
}

interface RawScheduledStage {
  id: number;
  name: string;
  state?: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface ScheduledStage extends RawScheduledStage {
  /** Whether `ended_at` (once set) represents a real, tellable duration.
   * False for a projected/synthetic end on a stage gated on live
   * conditions (gravity, temp-hold) or on manual advancement -- in both
   * cases the real end depends on something other than elapsed time, so
   * showing a number would claim a certainty that doesn't exist. */
  durationKnown: boolean;
}

interface DurationFields {
  seq: number;
  stage_type: string;
  end_mode: string;
  advance_mode: string;
  max_hours: number | null;
  end_hours: number | null;
  hold_hours: number | null;
  gravity_stable_hours: number | null;
}

// Sizing-only fallback for stages whose real duration can't be known ahead
// of time (gravity-gated, no cap set) -- used purely to give the ribbon a
// proportional width to draw, never surfaced as displayed text. Matches the
// reference design's own default-recipe total (456h across 5 stages).
const STAGE_TYPE_FALLBACK_HOURS: Record<string, number> = {
  primary: 96,
  free_rise: 24,
  diacetyl_rest: 72,
  conditioning: 168,
  cold_crash: 96,
};

function plannedDurationHours(s: DurationFields): number {
  if (s.max_hours != null) return s.max_hours;
  if (s.end_mode === "time") return s.end_hours ?? STAGE_TYPE_FALLBACK_HOURS[s.stage_type] ?? 24;
  if (s.end_mode === "temp_hold") return s.hold_hours ?? STAGE_TYPE_FALLBACK_HOURS[s.stage_type] ?? 24;
  return STAGE_TYPE_FALLBACK_HOURS[s.stage_type] ?? 24;
}

/** A stage's real end is only tellable ahead of time when it's on a fixed
 * clock (`time` end mode) AND nothing is holding it open past that point
 * for a human to advance manually. */
function isDurationDisplayable(s: { end_mode: string; advance_mode: string }): boolean {
  return s.end_mode === "time" && s.advance_mode !== "manual";
}

/** Projects the full planned stage schedule, not just stages that have
 * already started -- every stage now gets a sizing duration (real if it's
 * finished, a per-stage-type fallback estimate otherwise), so the ribbon
 * can always show the whole plan (like the design's ribbon does) instead
 * of only the part that's already happened, and the last stage's segment
 * always reaches the same right edge the rest of the chart's x-domain
 * does. Whether that duration is safe to *display* as text is a separate
 * question -- see `durationKnown` on the returned stages and `isDurationDisplayable`. */
export function projectStageSchedule<T extends DurationFields & RawScheduledStage>(stages: T[]): ScheduledStage[] {
  const ordered = [...stages].sort((a, b) => a.seq - b.seq);
  const out: ScheduledStage[] = [];
  let cursor: string | null = null;
  for (const s of ordered) {
    const start: string | null = s.started_at ?? cursor;
    if (start == null) break;
    const terminal = s.state === "finished" || s.state === "skipped";
    let end = s.ended_at;
    let durationKnown = terminal;
    if (end == null) {
      const durH = plannedDurationHours(s);
      end = new Date(new Date(start).getTime() + durH * 3_600_000).toISOString();
      durationKnown = isDurationDisplayable(s);
    }
    out.push({ id: s.id, name: s.name, state: s.state, started_at: start, ended_at: end, durationKnown });
    cursor = end;
  }
  return out;
}

/** Stage-ribbon segments, pixel-aligned to the same x scale as the plot
 * below it -- so the ribbon's boundaries always line up with the actual
 * data, not an independently-computed layout. */
export function ribbonSegments(
  stages: ScheduledStage[],
  toX: (ts: string) => number,
  plotWidth: number,
  batchComplete = false,
): RibbonSegment[] {
  const finished = stages.filter((s) => s.started_at);
  return finished.map((s, i) => {
    const x = toX(s.started_at!);
    const endTs = s.ended_at ?? (i + 1 < finished.length ? finished[i + 1].started_at : null);
    const endX = endTs ? toX(endTs) : plotWidth;
    const hours = endTs ? (new Date(endTs).getTime() - new Date(s.started_at!).getTime()) / 3_600_000 : null;
    const isLast = i === finished.length - 1;
    const showSpan = s.durationKnown && hours != null;
    return {
      x,
      width: Math.max(0, endX - x),
      label: s.name,
      span: showSpan ? formatSpan(hours!) : "",
      stageId: s.id,
      active: s.state === "running",
      done: s.state === "finished" || s.state === "skipped",
      ended: batchComplete && isLast,
    };
  });
}

/** X positions for the dashed stage-boundary lines drawn on the plot itself
 * -- one per stage start after the first (the first stage's start coincides
 * with the plot's own left edge, so a line there would be redundant). */
export function stageLineXs(stages: { started_at: string | null }[], toX: (ts: string) => number): number[] {
  const started = stages.filter((s) => s.started_at);
  return started.slice(1).map((s) => toX(s.started_at!));
}

/** Insets collapse the right-side gravity axis entirely when no gravity
 * source is mapped for this batch -- not just hidden, actually not
 * reserving the space, so the temp plot uses the full width. */
export function plotInsets(hasGravity: boolean): PlotInsets {
  return { top: 16, right: hasGravity ? 48 : 12, bottom: 28, left: 44 };
}
