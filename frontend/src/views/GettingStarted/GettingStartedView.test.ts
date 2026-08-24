import { describe, expect, it } from "vitest";
import { projectedHoursToTarget, timeToTargetLabel } from "./GettingStartedView";
import type { SeriesResponse } from "../../api/types";

// Minimal SeriesResponse builder -- only ts/beer_temp_f/effective_target_f/
// projection matter for these functions; everything else gets a plain,
// unused-by-this-logic default so the object type-checks.
function series(overrides: Partial<SeriesResponse> = {}): SeriesResponse {
  return {
    fermentation_id: 1,
    point_count: 1,
    ts: ["2026-01-01T00:00:00Z"],
    beer_temp_f: [64.0],
    chamber_temp_f: [60.0],
    gravity: [1.02],
    effective_target_f: [66.0],
    chamber_target_f: [55.0],
    chamber_mode: ["cool"],
    beer_temp_ok: [true],
    target_source: ["profile"],
    duty: { window_hours: 1, cool_pct: 100, heat_pct: 0, idle_pct: 0 },
    gaps: [],
    projection: null,
    ...overrides,
  };
}

function projection(ts: string[], beer_temp_f: (number | null)[], effective_target_f: (number | null)[]) {
  return { ts, beer_temp_f, effective_target_f, chamber_temp_f: beer_temp_f, gravity: beer_temp_f, chamber_target_f: beer_temp_f };
}

describe("projectedHoursToTarget", () => {
  it("finds the crossing point when beer is projected to reach a held target", () => {
    const s = series({
      ts: ["2026-01-01T00:00:00Z"],
      beer_temp_f: [64.0],
      effective_target_f: [66.0], // last real: diff = -2.0
      projection: projection(
        ["2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z", "2026-01-01T02:00:00Z"],
        [64.5, 65.0, 65.5, 66.5],
        [66.0, 66.0, 66.0, 66.0], // held -- diffs: -1.5, -1.0, -0.5, +0.5 (crosses at the last point)
      ),
    });
    expect(projectedHoursToTarget(s)).toBeCloseTo(2.0);
  });

  it("returns a sub-1h crossing when the projection's very first step already crosses", () => {
    const s = series({
      ts: ["2026-01-01T00:00:00Z"],
      beer_temp_f: [65.8],
      effective_target_f: [66.0], // last real: diff = -0.2
      projection: projection(["2026-01-01T00:30:00Z"], [66.2], [66.0]), // diff = +0.2 -- crosses immediately
    });
    expect(projectedHoursToTarget(s)).toBeCloseTo(0.5);
  });

  it("returns the empty-string sentinel when beer never catches a moving (ramping) target", () => {
    const s = series({
      ts: ["2026-01-01T00:00:00Z"],
      beer_temp_f: [64.0],
      effective_target_f: [66.0], // diff = -2.0
      projection: projection(
        ["2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z"],
        [64.3, 64.5, 64.6],
        [66.5, 67.0, 67.5], // ramping up faster than beer can follow -- diff stays negative, widening
      ),
    });
    expect(projectedHoursToTarget(s)).toBe("");
  });

  it("returns null when there is no projection at all (a completed batch)", () => {
    expect(projectedHoursToTarget(series({ projection: null }))).toBeNull();
  });

  it("returns null when the series itself has no real samples yet", () => {
    expect(
      projectedHoursToTarget(
        series({ ts: [], beer_temp_f: [], effective_target_f: [], projection: projection(["x"], [64], [66]) }),
      ),
    ).toBeNull();
  });
});

describe("timeToTargetLabel", () => {
  it("formats a multi-hour crossing", () => {
    const s = series({
      beer_temp_f: [64.0],
      effective_target_f: [66.0],
      projection: projection(
        ["2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z", "2026-01-01T02:00:00Z"],
        [64.5, 65.0, 65.5, 66.5],
        [66.0, 66.0, 66.0, 66.0],
      ),
    });
    expect(timeToTargetLabel(s)).toBe("2h to target");
  });

  it("uses the <1h phrasing instead of rounding down to 0h", () => {
    const s = series({
      beer_temp_f: [65.8],
      effective_target_f: [66.0],
      projection: projection(["2026-01-01T00:30:00Z"], [66.2], [66.0]),
    });
    expect(timeToTargetLabel(s)).toBe("<1h to target");
  });

  it("says so plainly when the projection never crosses", () => {
    const s = series({
      beer_temp_f: [64.0],
      effective_target_f: [66.0],
      projection: projection(["2026-01-01T00:30:00Z"], [64.3], [66.5]),
    });
    expect(timeToTargetLabel(s)).toBe("Won't reach target");
  });

  it("returns null (render nothing / fall back) when there is no projection", () => {
    expect(timeToTargetLabel(series({ projection: null }))).toBeNull();
  });
});
