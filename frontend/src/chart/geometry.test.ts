import { describe, expect, it } from "vitest";
import {
  buildGapPaths,
  buildLinePath,
  dutyColumns,
  gapBreakIndices,
  gravityScale,
  nearestIndex,
  projectStageSchedule,
  ribbonSegments,
  tempScale,
  timeScale,
  xTickStep,
} from "./geometry";

describe("timeScale", () => {
  it("maps the first timestamp to 0 and the last to plotWidth", () => {
    const ts = ["2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z", "2026-01-02T00:00:00Z"];
    const scale = timeScale(ts, 100);
    expect(scale(ts[0])).toBeCloseTo(0);
    expect(scale(ts[2])).toBeCloseTo(100);
    expect(scale(ts[1])).toBeCloseTo(50);
  });

  it("returns 0 for an empty series rather than throwing", () => {
    const scale = timeScale([], 100);
    expect(scale("2026-01-01T00:00:00Z")).toBe(0);
  });
});

describe("tempScale", () => {
  it("pads the data range and inverts y (higher temp -> smaller y)", () => {
    const { toY, min, max } = tempScale([60, 70], 200, 2);
    expect(min).toBe(58);
    expect(max).toBe(72);
    expect(toY(70)).toBeLessThan(toY(60));
  });

  it("falls back to a sane default range when every value is null", () => {
    const { min, max } = tempScale([null, null], 200);
    expect(max).toBeGreaterThan(min);
  });

  it("handles a very long-running fermentation's sample count without blowing the call stack", () => {
    // Math.min(...values)/Math.max(...values) throws "Maximum call stack
    // size exceeded" somewhere in the tens of thousands of spread
    // arguments -- a real fermentation that's been sampling on change for
    // months (a stage stuck well past its predicted duration, say) can
    // genuinely reach six figures of points.
    const values = Array.from({ length: 150_000 }, (_, i) => 60 + (i % 20));
    expect(() => tempScale(values, 200)).not.toThrow();
    const { min, max } = tempScale(values, 200);
    expect(min).toBe(58);
    expect(max).toBe(81);
  });
});

describe("gravityScale", () => {
  it("is fixed at 1.000-1.056 regardless of input", () => {
    const toY = gravityScale(100);
    expect(toY(1.0)).toBeCloseTo(100);
    expect(toY(1.056)).toBeCloseTo(0);
  });
});

describe("nearestIndex", () => {
  it("finds the exact match when the pixel lands on a sample", () => {
    const xs = [0, 10, 20, 30, 40];
    expect(nearestIndex(xs, 20)).toBe(2);
  });

  it("rounds to whichever neighbor is genuinely closer", () => {
    const xs = [0, 10, 20, 30, 40];
    expect(nearestIndex(xs, 24)).toBe(2);
    expect(nearestIndex(xs, 26)).toBe(3);
  });

  it("clamps to the first/last sample for out-of-range positions", () => {
    const xs = [10, 20, 30];
    expect(nearestIndex(xs, -50)).toBe(0);
    expect(nearestIndex(xs, 500)).toBe(2);
  });

  it("handles a single-sample series without dividing by zero", () => {
    expect(nearestIndex([15], 999)).toBe(0);
  });
});

describe("xTickStep", () => {
  it("uses a tighter step for short spans and a looser one for long spans", () => {
    expect(xTickStep(12)).toBe(4);
    expect(xTickStep(24)).toBe(6);
    expect(xTickStep(60)).toBe(12);
    expect(xTickStep(240)).toBeGreaterThanOrEqual(24);
  });
});

describe("buildLinePath", () => {
  it("draws a continuous path when there are no nulls or breaks", () => {
    const path = buildLinePath([0, 10, 20], [60, 65, 70], (v) => 100 - v);
    expect(path).toBe("M0.0,40.0 L10.0,35.0 L20.0,30.0");
  });

  it("starts a new segment (M, not L) after a null value", () => {
    const path = buildLinePath([0, 10, 20], [60, null, 70], (v) => 100 - v);
    expect(path).toBe("M0.0,40.0 M20.0,30.0");
    expect(path).not.toContain("L20.0");
  });

  it("breaks at explicitly flagged indices even when the value is present", () => {
    const path = buildLinePath([0, 10, 20], [60, 65, 70], (v) => 100 - v, new Set([1]));
    expect(path).toBe("M0.0,40.0 M20.0,30.0");
  });
});

describe("gapBreakIndices", () => {
  it("finds the index of each gap's end timestamp", () => {
    const ts = ["a", "b", "c", "d"];
    const idx = gapBreakIndices(ts, [{ from: "b", to: "c" }]);
    expect(idx.has(2)).toBe(true);
    expect(idx.size).toBe(1);
  });
});

describe("buildGapPaths", () => {
  const ts = ["a", "b", "c", "d"];
  const xs = [0, 10, 20, 30];

  it("draws a bridge segment connecting a gap's from/to samples", () => {
    const path = buildGapPaths(xs, [60, 65, 70, 75], (v) => 100 - v, ts, [{ from: "b", to: "c" }]);
    expect(path).toBe("M10.0,35.0 L20.0,30.0");
  });

  it("draws one independent bridge per gap, even for back-to-back gaps", () => {
    const path = buildGapPaths(xs, [60, 65, 70, 75], (v) => 100 - v, ts, [
      { from: "a", to: "b" },
      { from: "b", to: "c" },
    ]);
    expect(path).toBe("M0.0,40.0 L10.0,35.0 M10.0,35.0 L20.0,30.0");
  });

  it("skips a gap whose endpoint timestamp isn't in tsList", () => {
    const path = buildGapPaths(xs, [60, 65, 70, 75], (v) => 100 - v, ts, [{ from: "b", to: "not-in-list" }]);
    expect(path).toBe("");
  });

  it("skips a gap whose endpoint value is null -- nothing to bridge from/to", () => {
    const path = buildGapPaths(xs, [60, null, 70, 75], (v) => 100 - v, ts, [{ from: "b", to: "c" }]);
    expect(path).toBe("");
  });

  it("returns an empty string when there are no gaps", () => {
    expect(buildGapPaths(xs, [60, 65, 70, 75], (v) => 100 - v, ts, [])).toBe("");
  });
});

describe("dutyColumns", () => {
  it("assigns 100% of a bucket's duration to the mode that held it", () => {
    // Two samples, 30px apart, entirely in 'cool' mode -- with a 3px
    // bucket width that's 10 buckets, every one should read 100% cool.
    const cols = dutyColumns([0, 30], ["cool", "cool"], 30, 3);
    expect(cols.length).toBe(10);
    expect(cols.every((c) => c.coolFrac === 1 && c.heatFrac === 0)).toBe(true);
  });

  it("returns an empty array for an empty series", () => {
    expect(dutyColumns([], [], 100)).toEqual([]);
  });
});

describe("projectStageSchedule", () => {
  function stage(overrides: Record<string, unknown>) {
    return {
      id: 1,
      name: "Stage",
      state: "pending",
      started_at: null,
      ended_at: null,
      seq: 1,
      end_mode: "time",
      advance_mode: "auto",
      max_hours: null,
      end_hours: 24,
      hold_hours: null,
      gravity_stable_hours: null,
      ...overrides,
    };
  }

  it("lays out every stage, including ones that haven't started", () => {
    const stages = [
      stage({ id: 1, name: "Primary", seq: 1, state: "running", started_at: "2026-01-01T00:00:00Z", advance_mode: "auto" }),
      stage({ id: 2, name: "Free rise", seq: 2, state: "pending" }),
      stage({ id: 3, name: "Cold crash", seq: 3, state: "pending" }),
    ];
    const out = projectStageSchedule(stages);
    expect(out.map((s) => s.name)).toEqual(["Primary", "Free rise", "Cold crash"]);
  });

  it("clamps a RUNNING stage's segment to end exactly at `now` once its guessed end is already behind it, shifting everything after it later instead of dropping it", () => {
    // The real bug this pins: a manual-advance stage that's already met its
    // own criteria (so its own guessed 24h end has long since passed) used
    // to either get subsequent stages laid out right at that stale,
    // already-past position (reading as "already past cold crash"), get
    // dropped from the ribbon entirely (an earlier, overcorrected fix), or
    // stretch a fresh guess PAST now (another earlier fix -- looked like
    // "there's more work left in this stage" when the honest answer is "we
    // don't know, it could end any moment"). The right behavior: end this
    // stage's segment exactly at now, and lay out the rest of the plan
    // starting right there.
    const stages = [
      stage({ id: 1, name: "Primary", seq: 1, state: "running", started_at: "2026-01-01T00:00:00Z", advance_mode: "manual" }),
      stage({ id: 2, name: "Free rise", seq: 2, state: "pending" }),
      stage({ id: 3, name: "Cold crash", seq: 3, state: "pending" }),
    ];
    // 96h after Primary started -- 72h past its own 24h guess.
    const now = "2026-01-05T00:00:00Z";
    const out = projectStageSchedule(stages, now);
    expect(out.map((s) => s.name)).toEqual(["Primary", "Free rise", "Cold crash"]);
    const primary = out[0];
    expect(primary.ended_at).toBe(new Date(now).toISOString());
    // Free rise starts exactly where Primary was clamped to, not its
    // original (already-passed) guessed end.
    expect(out[1].started_at).toBe(primary.ended_at);
  });

  it("does not stretch a stage whose guessed end is still ahead of `now`", () => {
    const stages = [stage({ id: 1, name: "Primary", seq: 1, state: "running", started_at: "2026-01-01T00:00:00Z" })];
    const now = "2026-01-01T06:00:00Z"; // 6h in -- well inside the 24h guess
    const out = projectStageSchedule(stages, now);
    expect(out[0].ended_at).toBe("2026-01-02T00:00:00.000Z");
  });

  it("never stretches a finished/skipped stage even if its real end is in the past relative to `now`", () => {
    const stages = [
      stage({
        id: 1, name: "Primary", seq: 1, state: "finished",
        started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T12:00:00Z",
      }),
    ];
    const out = projectStageSchedule(stages, "2026-02-01T00:00:00Z");
    expect(out[0].ended_at).toBe("2026-01-01T12:00:00Z");
  });
});

describe("ribbonSegments", () => {
  it("uses the next stage's start as this stage's end when ended_at is null", () => {
    const stages = [
      { id: 1, name: "Primary", started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-02T00:00:00Z", durationKnown: true },
      { id: 2, name: "Conditioning", started_at: "2026-01-02T00:00:00Z", ended_at: null, durationKnown: true },
    ];
    const toX = timeScale(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"], 100);
    const segs = ribbonSegments(stages, toX, 100);
    expect(segs).toHaveLength(2);
    expect(segs[1].width).toBeGreaterThan(0);
  });
});
