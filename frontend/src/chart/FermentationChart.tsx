import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { SeriesResponse, StageResponse } from "../api/types";
import { formatDateTime } from "../lib/format";
import { useMeasuredWidth } from "../lib/useMeasuredWidth";
import {
  buildGapPaths,
  buildLinePath,
  dutyColumns,
  gapBreakIndices,
  gravityScale,
  nearestIndex,
  plotInsets,
  projectStageSchedule,
  ribbonSegments,
  stageLineXs,
  tempScale,
  timeScale,
  xTickStep,
} from "./geometry";
import styles from "./FermentationChart.module.css";

interface Props {
  series: SeriesResponse;
  stages: StageResponse[];
  /** Batch has finished or been terminated -- the ribbon's last segment gets
   * its own "wrapped up" tone distinct from an ordinary passed stage. */
  complete?: boolean;
  /** Fires with the sample index nearest the pointer while the user is
   * pressing or dragging on the chart (mouse or touch, unified via the
   * Pointer Events API), and with `null` the instant they release -- lets
   * the parent temporarily swap its stat tiles over to "what was true at
   * this point in time" without this component knowing anything about
   * tiles itself. Index is into the same `series` arrays passed in above. */
  onScrub?: (index: number | null) => void;
}

const PLOT_HEIGHT = 260;

// Zoom stretches the SVG itself wider than its container (up to 10x) and
// leans on .plotWrap's existing overflow-x:auto as the pan mechanism --
// no separate "visible time window" concept to keep in sync with the
// model's own scales, and no conflict with the horizontal-scroll-bridge
// styling that already exists for a wide/long chart.
const MIN_ZOOM = 1;
const MAX_ZOOM = 10;
// Exponential, not linear, so each wheel "tick" feels like the same
// relative zoom step regardless of the current zoom level (multiplying,
// not adding) -- tuned to feel similar to Google Maps/Figma's ctrl+wheel.
const WHEEL_ZOOM_SENSITIVITY = 0.004;

export function FermentationChart({ series, stages, complete = false, onScrub }: Props) {
  const [wrapRef, measuredWidth] = useMeasuredWidth<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const baseWidth = Math.max(320, measuredWidth || 640);
  const [zoom, setZoom] = useState(1);
  const outerWidth = baseWidth * zoom;
  const [scrubIndex, setScrubIndex] = useState<number | null>(null);
  // A ref, not state -- pointermove fires at high frequency and only ever
  // needs to know "are we currently pressed," which pointer capture already
  // guarantees keeps targeting this element regardless of where the
  // pointer physically wanders. No re-render needed just to track that.
  const scrubbingRef = useRef(false);
  // Pointer tracking for pinch-to-zoom -- keyed by pointerId, since Pointer
  // Events deliver one independent stream per finger; a pinch is recognized
  // here as "exactly two pointers currently down," not as any single
  // browser-native gesture type.
  const activePointersRef = useRef(new Map<number, { x: number; y: number }>());
  const pinchStartRef = useRef<{ distance: number; zoom: number } | null>(null);
  // Two-finger pan -- the midpoint's own horizontal movement scrolls
  // .plotWrap directly (its scrollLeft, same mechanism a desktop trackpad's
  // plain wheel scroll already uses via the browser's native overflow-x
  // handling). Tracked as "the last seen midpoint," not the gesture's
  // start position, so it composes cleanly with a simultaneous pinch --
  // each move applies its own incremental delta rather than one absolute
  // offset computed from a possibly-stale start point.
  const panLastMidpointXRef = useRef<number | null>(null);
  // Set by a zoom gesture (wheel or pinch) right before calling setZoom,
  // read back and cleared by the layout effect below once the new
  // outerWidth has actually committed to the DOM -- keeps the gesture's
  // focal point (cursor position, or pinch midpoint) visually still
  // instead of every zoom just growing away from the left edge.
  const pendingZoomAnchorRef = useRef<{ clientX: number; fraction: number } | null>(null);

  // A different batch shouldn't inherit whatever zoom/scroll position was
  // left over from the last one being viewed.
  useEffect(() => {
    setZoom(1);
    if (wrapRef.current) wrapRef.current.scrollLeft = 0;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [series.fermentation_id]);

  useLayoutEffect(() => {
    const anchor = pendingZoomAnchorRef.current;
    const wrap = wrapRef.current;
    if (!anchor || !wrap) return;
    pendingZoomAnchorRef.current = null;
    const containerLeft = wrap.getBoundingClientRect().left;
    wrap.scrollLeft = anchor.fraction * outerWidth - (anchor.clientX - containerLeft);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outerWidth]);

  // Native (non-passive) wheel listener -- React attaches its own
  // onWheel prop as a PASSIVE listener by default (matching Chrome's own
  // recommendation for scroll perf), which silently ignores
  // preventDefault() calls. Suppressing the browser's native ctrl+wheel
  // page-zoom requires a real, non-passive DOM listener.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    function onWheel(e: WheelEvent) {
      if (!e.ctrlKey) return; // plain scroll/trackpad-pan passes through untouched
      e.preventDefault();
      const factor = Math.exp(-e.deltaY * WHEEL_ZOOM_SENSITIVITY);
      requestZoom(zoom * factor, e.clientX);
    }
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outerWidth]);

  function requestZoom(targetZoom: number, clientX: number) {
    const wrap = wrapRef.current;
    const clamped = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, targetZoom));
    if (!wrap || clamped === zoom) return;
    const containerLeft = wrap.getBoundingClientRect().left;
    const contentXBefore = wrap.scrollLeft + (clientX - containerLeft);
    const fraction = outerWidth > 0 ? contentXBefore / outerWidth : 0;
    pendingZoomAnchorRef.current = { clientX, fraction };
    setZoom(clamped);
  }

  function pinchDistance(): number | null {
    const pts = [...activePointersRef.current.values()];
    if (pts.length !== 2) return null;
    return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  }

  function pinchMidpointX(): number {
    const pts = [...activePointersRef.current.values()];
    return (pts[0].x + pts[1].x) / 2;
  }

  const hasGravity = series.gravity.some((g) => g !== null);
  const hasGaps = series.gaps.length > 0;
  const proj = series.projection;
  const hasProjection = !!proj && proj.ts.length > 0;
  const insets = plotInsets(hasGravity);
  const plotWidth = Math.max(1, outerWidth - insets.left - insets.right);

  const model = useMemo(() => {
    if (series.point_count === 0) return null;
    // The ribbon shows the *whole planned schedule*, not just stages that
    // have already started -- so the x-scale's domain has to extend far
    // enough to place stages that haven't happened yet, whenever their
    // planned duration is actually knowable (a cap or a time-based end).
    const scheduled = projectStageSchedule(stages, series.ts[series.ts.length - 1]);
    const scheduleEndTs = scheduled.length ? scheduled[scheduled.length - 1].ended_at : null;
    const allTs = hasProjection ? [...series.ts, ...proj!.ts] : [...series.ts];
    if (scheduleEndTs && new Date(scheduleEndTs).getTime() > new Date(allTs[allTs.length - 1]).getTime()) {
      allTs.push(scheduleEndTs);
    }
    const toX = timeScale(allTs, plotWidth);
    const xs = series.ts.map(toX);
    const allTemps = [...series.beer_temp_f, ...series.chamber_temp_f, ...series.chamber_target_f];
    if (hasProjection) allTemps.push(...proj!.beer_temp_f, ...proj!.chamber_temp_f, ...proj!.chamber_target_f);
    const temp = tempScale(allTemps, PLOT_HEIGHT);
    const toGravityY = gravityScale(PLOT_HEIGHT);
    const breaks = gapBreakIndices(series.ts, series.gaps);

    const beerPath = buildLinePath(xs, series.beer_temp_f, temp.toY, breaks);
    const chamberPath = buildLinePath(xs, series.chamber_temp_f, temp.toY, breaks);
    const targetPath = buildLinePath(xs, series.chamber_target_f, temp.toY, breaks);
    const gravityPath = hasGravity ? buildLinePath(xs, series.gravity, toGravityY, breaks) : "";

    // Dotted bridges across the same gaps that just broke the lines above --
    // a distinct dash pattern from the forward projection's, so a "we don't
    // know what happened here" gap can never be mistaken for either real
    // data or the projected preview.
    const beerGapPath = buildGapPaths(xs, series.beer_temp_f, temp.toY, series.ts, series.gaps);
    const chamberGapPath = buildGapPaths(xs, series.chamber_temp_f, temp.toY, series.ts, series.gaps);
    const targetGapPath = buildGapPaths(xs, series.chamber_target_f, temp.toY, series.ts, series.gaps);
    const gravityGapPath = hasGravity ? buildGapPaths(xs, series.gravity, toGravityY, series.ts, series.gaps) : "";

    let beerProjPath = "";
    let chamberProjPath = "";
    let targetProjPath = "";
    let nowX = 0;
    if (hasProjection) {
      const projXs = proj!.ts.map(toX);
      beerProjPath = buildLinePath(projXs, proj!.beer_temp_f, temp.toY);
      chamberProjPath = buildLinePath(projXs, proj!.chamber_temp_f, temp.toY);
      targetProjPath = buildLinePath(projXs, proj!.chamber_target_f, temp.toY);
      // Gravity is deliberately NOT projected forward -- contracts/
      // projection.py holds it flat at its last known value (there's no
      // honest way to extrapolate a real fermentation's actual curve), and
      // a flat dashed line reads as a genuine "gravity is about to stop
      // moving" prediction rather than the "we simply don't know" it
      // actually is. See that module's docstring.
      nowX = toX(series.ts[series.ts.length - 1]);
    }

    const duty = dutyColumns(xs, series.chamber_mode, plotWidth);
    const ribbon = ribbonSegments(scheduled, toX, plotWidth, complete);
    const stageLines = stageLineXs(scheduled, toX);

    const lastTs = allTs[allTs.length - 1];
    const spanMs = new Date(lastTs).getTime() - new Date(series.ts[0]).getTime();
    const spanHours = spanMs / 3_600_000;
    const tickStepH = xTickStep(spanHours);
    const startMs = new Date(series.ts[0]).getTime();
    const xTicks: { x: number; label: string }[] = [];
    for (let h = 0; h <= spanHours; h += tickStepH) {
      const ts = new Date(startMs + h * 3_600_000).toISOString();
      xTicks.push({ x: toX(ts), label: formatTick(h) });
    }

    return {
      xs, temp, toGravityY, beerPath, chamberPath, targetPath, gravityPath, duty, ribbon, stageLines, xTicks,
      beerGapPath, chamberGapPath, targetGapPath, gravityGapPath,
      beerProjPath, chamberProjPath, targetProjPath, nowX,
    };
  }, [series, stages, plotWidth, hasGravity, hasProjection, proj, complete]);

  // Pixel -> nearest-real-sample-index hit-testing, shared by pointer-down
  // and pointer-move. Real samples only (series.xs, not the projection) --
  // scrubbing into the projected/future region just clamps to the last
  // known real value, which is the honest answer to "what was true then."
  function scrubIndexFromClientX(svg: SVGSVGElement, clientX: number): number | null {
    if (!model || model.xs.length === 0) return null;
    const rect = svg.getBoundingClientRect();
    const xInPlot = clientX - rect.left - insets.left;
    const clamped = Math.max(0, Math.min(plotWidth, xInPlot));
    return nearestIndex(model.xs, clamped);
  }

  function handlePointerDown(e: ReactPointerEvent<SVGSVGElement>) {
    if (!model) return;
    // Belt-and-suspenders alongside .root's user-select:none (CSS module)
    // -- without this, a mousedown-and-drag gesture on some browsers still
    // starts a native text selection over the legend/axis labels/ribbon
    // before the CSS rule alone gets a chance to suppress it.
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    activePointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (activePointersRef.current.size === 2) {
      // Second finger down -- this is a pinch, not a scrub. Drop any
      // in-progress single-finger scrub cleanly rather than leaving the
      // tiles/scrub-line stuck on wherever the first finger last landed.
      scrubbingRef.current = false;
      setScrubIndex(null);
      onScrub?.(null);
      const distance = pinchDistance();
      if (distance !== null) pinchStartRef.current = { distance, zoom };
      panLastMidpointXRef.current = pinchMidpointX();
      return;
    }
    if (activePointersRef.current.size > 2) return; // a stray 3rd+ finger -- ignore

    scrubbingRef.current = true;
    const idx = scrubIndexFromClientX(e.currentTarget, e.clientX);
    setScrubIndex(idx);
    onScrub?.(idx);
  }

  function handlePointerMove(e: ReactPointerEvent<SVGSVGElement>) {
    if (!activePointersRef.current.has(e.pointerId)) return;
    activePointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (activePointersRef.current.size === 2) {
      if (pinchStartRef.current) {
        const distance = pinchDistance();
        if (distance !== null && distance > 0) {
          const target = pinchStartRef.current.zoom * (distance / pinchStartRef.current.distance);
          requestZoom(target, pinchMidpointX());
        }
      }
      // Pan alongside the zoom, not instead of it -- a real two-finger
      // touch gesture routinely does both at once (fingers spreading AND
      // drifting sideways), and there's no reason to force the user to
      // pick one motion at a time.
      const midpoint = pinchMidpointX();
      if (panLastMidpointXRef.current !== null && wrapRef.current) {
        wrapRef.current.scrollLeft -= midpoint - panLastMidpointXRef.current;
      }
      panLastMidpointXRef.current = midpoint;
      return;
    }
    if (!scrubbingRef.current || !model || activePointersRef.current.size !== 1) return;
    const idx = scrubIndexFromClientX(e.currentTarget, e.clientX);
    setScrubIndex(idx);
    onScrub?.(idx);
  }

  function endScrub() {
    if (!scrubbingRef.current) return;
    scrubbingRef.current = false;
    setScrubIndex(null);
    onScrub?.(null);
  }

  function endPointer(e: ReactPointerEvent<SVGSVGElement>) {
    activePointersRef.current.delete(e.pointerId);
    if (activePointersRef.current.size < 2) {
      pinchStartRef.current = null;
      panLastMidpointXRef.current = null;
    }
    if (activePointersRef.current.size === 0) endScrub();
  }

  return (
    <div className={styles.root}>
      <Legend hasGravity={hasGravity} hasGaps={hasGaps} />
      <div className={styles.plotWrap} ref={wrapRef}>
        {model && (
          // Inside .plotWrap (not a sibling above it) so the ribbon and the
          // plot scroll as ONE horizontally-scrolling unit -- it already
          // zoomed correctly (its width is plotWidth, which scales with
          // zoom same as the SVG), but as a sibling outside .plotWrap it
          // never moved when the zoomed plot got scrolled/panned, so the
          // stage boundaries drifted out of alignment with the data.
          <div className={styles.ribbon} style={{ marginLeft: insets.left, width: plotWidth }}>
            {model.ribbon.map((seg) => (
              <div
                key={seg.stageId}
                className={[
                  styles.ribbonSeg,
                  seg.active ? styles.ribbonActive : seg.ended ? styles.ribbonEnded : seg.done ? styles.ribbonDone : styles.ribbonPending,
                ].join(" ")}
                style={{ left: seg.x + 1, width: Math.max(0, seg.width - 2) }}
              >
                <div className={styles.ribbonName}>{seg.label}</div>
                <div className={styles.ribbonSpan}>{seg.span}</div>
              </div>
            ))}
          </div>
        )}
        <svg
          ref={svgRef}
          className={styles.plot}
          width={outerWidth}
          height={insets.top + PLOT_HEIGHT + insets.bottom}
          role="img"
          aria-label="Fermentation temperature and gravity over time"
          // "pan-y", not "none" -- the scrub gesture only ever reads
          // clientX (horizontal position along the timeline), so a
          // near-vertical swipe should still scroll the page natively.
          // "none" would claim the WHOLE gesture the instant it starts on
          // the chart -- touch-action is decided once at gesture start and
          // then governs it for its full duration, even after the finger
          // moves well outside the chart's bounds -- which is exactly why
          // a swipe that merely started on/near a chart occupying a third
          // of a phone screen could freeze page-scroll entirely, even
          // though the drag was clearly headed elsewhere.
          style={{ touchAction: "pan-y" }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={endPointer}
          onPointerCancel={endPointer}
          // A touch-and-hold is indistinguishable from the start of a
          // scrub until it's already underway, so the browser's native
          // long-press context menu (Chrome/Android) needs suppressing
          // directly -- touch-action alone doesn't cover it, that's a
          // separate default action tied to the 'contextmenu' event.
          onContextMenu={(e) => e.preventDefault()}
        >
          {model && (
            <g transform={`translate(${insets.left},${insets.top})`}>
              {/* future region -- shaded so the projected half of the plot reads as "preview" */}
              {hasProjection && (
                <rect x={model.nowX} y={0} width={plotWidth - model.nowX} height={PLOT_HEIGHT} className={styles.futureRegion} />
              )}

              {/* duty-cycle wash */}
              {model.duty.map((c, i) =>
                c.hasData ? (
                  <rect
                    key={i}
                    x={c.x}
                    y={0}
                    width={c.width}
                    height={PLOT_HEIGHT}
                    fill={
                      // rgba(var(--triple), alpha) mixes the space-separated
                      // custom-property expansion with a comma before alpha,
                      // which isn't valid CSS color syntax -- the browser
                      // rejects the whole value and SVG's fill falls back to
                      // its initial black, painting over the whole plot.
                      // rgb(var(--triple) / alpha) is the modern form that
                      // actually accepts a space-separated triple.
                      c.coolFrac >= c.heatFrac
                        ? `rgb(var(--kr-wash-cool-rgb) / ${(c.coolFrac * 0.24).toFixed(3)})`
                        : `rgb(var(--kr-wash-heat-rgb) / ${(c.heatFrac * 0.24).toFixed(3)})`
                    }
                  />
                ) : null,
              )}

              {/* y gridlines + labels (temp axis) */}
              {tempTicks(model.temp.min, model.temp.max, model.temp.tickStep).map((t) => (
                <g key={t}>
                  <line x1={0} x2={plotWidth} y1={model.temp.toY(t)} y2={model.temp.toY(t)} className={styles.gridLine} />
                  <text x={-8} y={model.temp.toY(t)} dy="0.32em" textAnchor="end" className={styles.axisLabel}>
                    {t}°
                  </text>
                </g>
              ))}

              {/* gravity axis labels, right side */}
              {hasGravity &&
                gravityTicks().map((g) => (
                  <text key={g} x={plotWidth + 8} y={model.toGravityY(g)} dy="0.32em" className={styles.axisLabel}>
                    {g.toFixed(3)}
                  </text>
                ))}

              {/* x axis ticks */}
              {model.xTicks.map((t, i) => (
                <text key={i} x={t.x} y={PLOT_HEIGHT + 18} textAnchor="middle" className={styles.axisLabel}>
                  {t.label}
                </text>
              ))}
              <line x1={0} x2={plotWidth} y1={PLOT_HEIGHT} y2={PLOT_HEIGHT} className={styles.axisLine} />

              {/* dashed stage-boundary lines, pixel-aligned to the ribbon above */}
              {model.stageLines.map((x, i) => (
                <line key={i} x1={x} x2={x} y1={0} y2={PLOT_HEIGHT} className={styles.stageLine} />
              ))}

              {/* series */}
              <path d={model.targetPath} fill="none" stroke="var(--kr-plan)" strokeWidth={1.5} strokeDasharray="4 3" />
              {hasGravity && <path d={model.gravityPath} fill="none" stroke="var(--kr-gravity)" strokeWidth={1.5} strokeDasharray="3 2" />}
              <path d={model.chamberPath} fill="none" stroke="var(--kr-cool)" strokeWidth={1.75} />
              <path d={model.beerPath} fill="none" stroke="var(--kr-accent)" strokeWidth={2} />

              {/* gap bridges -- deliberately NOT drawn in each series' own color, unlike the
                  projection below. A projection is a preview of the same series continuing
                  (color/identity carries forward, just faded+dashed); a gap is the opposite --
                  we don't know what this series actually did here, so it gets no identity at
                  all: one neutral ink, large sparse round dots, never a per-series hue. That
                  keeps it from ever reading as "a paler version of the real or projected line"
                  even at a glance. Drawn over the breaks buildLinePath just made, so a gap
                  never reads as simply missing/blank either. */}
              <path d={model.targetGapPath} fill="none" className={styles.gapPath} />
              {hasGravity && <path d={model.gravityGapPath} fill="none" className={styles.gapPath} />}
              <path d={model.chamberGapPath} fill="none" className={styles.gapPath} />
              <path d={model.beerGapPath} fill="none" className={styles.gapPath} />

              {/* forward projection -- dashed continuation, a preview not a prediction (see contracts/projection.py) */}
              {hasProjection && (
                <>
                  <path d={model.targetProjPath} fill="none" stroke="var(--kr-plan)" strokeWidth={1.5} strokeDasharray="2 3" opacity={0.6} />
                  <path d={model.chamberProjPath} fill="none" stroke="var(--kr-cool)" strokeWidth={1.75} strokeDasharray="2 3" opacity={0.6} />
                  <path d={model.beerProjPath} fill="none" stroke="var(--kr-accent)" strokeWidth={2} strokeDasharray="2 3" opacity={0.6} />
                  <line x1={model.nowX} x2={model.nowX} y1={0} y2={PLOT_HEIGHT} className={styles.nowLine} />
                  {/* Suppressed while scrubbing -- the scrub label below sits in this exact
                      spot, and with the now-line usually close to the scrubbed point the two
                      texts would overlap. The line itself stays put as an "as of now" anchor. */}
                  {scrubIndex === null && (
                    <text x={model.nowX} y={-4} textAnchor="middle" className={styles.nowLabel}>
                      NOW
                    </text>
                  )}
                </>
              )}

              {/* scrub crosshair -- mouse-down/touch-down-and-drag preview, see onScrub above.
                  Label sits in the exact same spot the "NOW" label uses (so there's one
                  consistent "this is what moment in time you're looking at" convention, not a
                  second competing indicator), with an adaptive anchor near either edge so a
                  longer date/time string never runs off the plot the way a centered-only anchor
                  would. */}
              {scrubIndex !== null && (
                <>
                  <line
                    x1={model.xs[scrubIndex]}
                    x2={model.xs[scrubIndex]}
                    y1={0}
                    y2={PLOT_HEIGHT}
                    className={styles.scrubLine}
                  />
                  <text
                    x={model.xs[scrubIndex]}
                    y={-4}
                    textAnchor={model.xs[scrubIndex] < 60 ? "start" : model.xs[scrubIndex] > plotWidth - 60 ? "end" : "middle"}
                    className={styles.scrubLabel}
                  >
                    {formatDateTime(new Date(series.ts[scrubIndex]))}
                  </text>
                </>
              )}
            </g>
          )}
        </svg>
      </div>
    </div>
  );
}

function Legend({ hasGravity, hasGaps }: { hasGravity: boolean; hasGaps: boolean }) {
  return (
    <div className={styles.legend}>
      <LegendItem color="var(--kr-accent)" label="Beer temp" />
      <LegendItem color="var(--kr-cool)" label="Chamber temp" />
      {hasGravity && <LegendItem color="var(--kr-gravity)" label="Gravity" dashed />}
      <LegendItem color="var(--kr-plan)" label="Setpoint" dashed />
      {hasGaps && <LegendItem color="var(--kr-ink-muted)" label="Gap (daemon down)" dotted />}
    </div>
  );
}

function LegendItem({
  color,
  label,
  dashed = false,
  dotted = false,
}: {
  color: string;
  label: string;
  dashed?: boolean;
  dotted?: boolean;
}) {
  const swatchClass = dotted ? styles.swatchDotted : dashed ? styles.swatchDashed : styles.swatch;
  return (
    <span className={styles.legendItem}>
      <span className={swatchClass} style={{ background: dashed || dotted ? undefined : color, color }} />
      {label}
    </span>
  );
}

// Hard backstop, independent of tempScale's own niceStep safeguard --
// this loop should never produce more than a couple dozen ticks in
// practice, so 500 is "something has gone very wrong upstream," not a
// real chart. Cheap insurance against a whole-page crash (the actual
// incident this guards: a numerically unstable projection once produced
// a ~1e200F span, and an uncapped loop here ran until it blew the array
// past JS's max length).
const MAX_TEMP_TICKS = 500;

function tempTicks(min: number, max: number, step: number): number[] {
  const ticks: number[] = [];
  if (!Number.isFinite(min) || !Number.isFinite(max) || !Number.isFinite(step) || step <= 0) return ticks;
  for (let t = Math.ceil(min / step) * step; t <= max && ticks.length < MAX_TEMP_TICKS; t += step) ticks.push(t);
  return ticks;
}

function gravityTicks(): number[] {
  return [1.010, 1.020, 1.030, 1.040, 1.050];
}

function formatTick(hours: number): string {
  const days = Math.floor(hours / 24);
  if (hours < 24) return `${hours}h`;
  return `d${days}`;
}
