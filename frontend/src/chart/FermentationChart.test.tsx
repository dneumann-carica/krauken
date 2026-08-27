import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FermentationChart } from "./FermentationChart";
import styles from "./FermentationChart.module.css";
import type { SeriesResponse, StageResponse } from "../api/types";

function stage(overrides: Partial<StageResponse>): StageResponse {
  return {
    id: 1,
    fermentation_id: 1,
    seq: 1,
    name: "Primary",
    temp_mode: "fixed",
    temp_f: 68,
    temp_from_f: null,
    temp_to_f: null,
    ramp_hours: null,
    end_mode: "gravity",
    end_hours: null,
    hold_temp_f: null,
    hold_hours: null,
    gravity_hi: null,
    gravity_stable_hours: null,
    min_hours: null,
    max_hours: null,
    advance_mode: "auto",
    state: "finished",
    started_at: "2026-01-01T00:00:00Z",
    ended_at: "2026-01-04T00:00:00Z",
    criteria_met_at: "2026-01-04T00:00:00Z",
    end_actual_reason: "gravity_stable",
    ...overrides,
  };
}

function series(overrides: Partial<SeriesResponse> = {}): SeriesResponse {
  return {
    fermentation_id: 1,
    point_count: 4,
    ts: [
      "2026-01-01T00:00:00Z",
      "2026-01-01T01:00:00Z",
      "2026-01-02T00:00:00Z",
      "2026-01-04T00:00:00Z",
    ],
    beer_temp_f: [65, 65.2, 66, null],
    chamber_temp_f: [64, 64.5, 65.5, 65.8],
    gravity: [1.05, null, null, 1.012],
    effective_target_f: [66, 66, 66, 66],
    chamber_target_f: [63, 62, 62, 66],
    chamber_mode: ["idle", "cool", "cool", "idle"],
    beer_temp_ok: [true, true, true, false],
    target_source: ["profile", "profile", "profile", "profile"],
    duty: { window_hours: 72, cool_pct: 40, heat_pct: 5, idle_pct: 55 },
    gaps: [{ from: "2026-01-02T00:00:00Z", to: "2026-01-04T00:00:00Z", minutes: 2880 }],
    projection: null,
    ...overrides,
  };
}

describe("FermentationChart", () => {
  it("renders series, stage ribbon, and gravity axis when gravity is present", () => {
    render(<FermentationChart series={series()} stages={[stage({})]} />);
    expect(screen.getByRole("img", { name: /fermentation temperature/i })).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    expect(screen.getByText("Gravity")).toBeInTheDocument();
    // Duty-cycle summary text lives in GettingStartedView now, not here --
    // it used to be shown twice (once inline under the graph, once again
    // in the page's own legend/caption block below), so this component
    // dropped its own copy rather than keep two sources of the same number.
  });

  it("collapses the gravity axis and legend entry when no gravity is mapped", () => {
    render(
      <FermentationChart
        series={series({ gravity: [null, null, null, null] })}
        stages={[stage({})]}
      />,
    );
    expect(screen.queryByText("Gravity")).not.toBeInTheDocument();
  });

  it("renders nothing but the legend for an empty series without crashing", () => {
    render(
      <FermentationChart
        series={series({ point_count: 0, ts: [], beer_temp_f: [], chamber_temp_f: [], gravity: [], effective_target_f: [], chamber_target_f: [], chamber_mode: [], beer_temp_ok: [], target_source: [] })}
        stages={[]}
      />,
    );
    expect(screen.getByRole("img")).toBeInTheDocument();
  });

  it("renders a NOW marker and dashed projection paths when the series carries one", () => {
    const { container } = render(
      <FermentationChart
        series={series({
          projection: {
            ts: ["2026-01-04T06:00:00Z", "2026-01-04T12:00:00Z"],
            beer_temp_f: [65.5, 65.0],
            chamber_temp_f: [64.0, 63.0],
            gravity: [1.012, 1.012],
            effective_target_f: [66, 66],
            chamber_target_f: [62, 62],
          },
        })}
        stages={[stage({})]}
      />,
    );
    expect(screen.getByText("NOW")).toBeInTheDocument();
    // target, chamber, beer -- one projected dashed path per series that's
    // actually safe to extrapolate. Gravity deliberately has no projected
    // path even though it rode along on the wire (contracts/projection.py
    // holds it flat at its last known value) -- a flat dashed line reads as
    // a genuine prediction, not the "we don't know" it actually is. See
    // contracts/projection.py's module docstring.
    expect(container.querySelectorAll("path[stroke-dasharray='2 3']")).toHaveLength(3);
    expect(container.querySelector("path[stroke='var(--kr-gravity)'][stroke-dasharray='2 3']")).not.toBeInTheDocument();
    // Beer target deliberately has no projected continuation either -- it's
    // already exactly what target_temp_f() says the authored stage wants
    // right now (current stage or a future one), so projecting it forward
    // would just re-derive the ribbon's own stage schedule in a different
    // visual form; only the real (left-of-NOW) beer target line is drawn.
    // stroke-width='1.5' narrows this to the beer-target line's own weight
    // -- the real beer_temp_f projection also uses var(--kr-accent) with
    // this same dash pattern, just at stroke-width 2.
    expect(
      container.querySelector("path[stroke='var(--kr-accent)'][stroke-dasharray='2 3'][stroke-width='1.5']"),
    ).not.toBeInTheDocument();
  });

  it("bridges each projected path to start exactly where its solid line ends, no gap", () => {
    // The projection's own first point is t_h_from_now = one
    // PROJECTION_STEP_H into the future (never a t=0 point) -- before this
    // fix, the dashed line started there, leaving a visible gap (in both
    // x and y) at "NOW" between where the solid line stopped and where the
    // dashed one began. The bridge means the two must share one exact
    // coordinate, not just look close.
    const { container } = render(
      <FermentationChart
        series={series({
          gaps: [], // isolates this test from the fixture's default gap-break, which otherwise
          // starts a fresh "M" mid-line and complicates finding the solid path's true last point
          projection: {
            ts: ["2026-01-04T06:00:00Z", "2026-01-04T12:00:00Z"],
            beer_temp_f: [65.5, 65.0],
            chamber_temp_f: [64.0, 63.0],
            gravity: [1.012, 1.012],
            effective_target_f: [66, 66],
            chamber_target_f: [60, 62], // deliberately far from the real last value (66) so a gap would be obvious
          },
        })}
        stages={[stage({})]}
      />,
    );
    const dashedTarget = container.querySelector("path[stroke='var(--kr-plan)'][stroke-dasharray='2 3']");
    const solidTarget = container.querySelector("path[stroke='var(--kr-plan)'][stroke-dasharray='4 3']");
    const firstDashedPoint = dashedTarget?.getAttribute("d")?.match(/^M([\d.]+,[\d.]+)/)?.[1];
    const lastSolidPoint = solidTarget?.getAttribute("d")?.trim().split(" ").pop()?.replace(/^[ML]/, "");
    expect(firstDashedPoint).toBeTruthy();
    expect(firstDashedPoint).toBe(lastSolidPoint);
  });

  it("renders no NOW marker when the series has no projection (a completed batch)", () => {
    render(<FermentationChart series={series()} stages={[stage({})]} />);
    expect(screen.queryByText("NOW")).not.toBeInTheDocument();
  });

  it("shows the Gap legend entry and draws a dotted bridge path when the series has a reported gap", () => {
    const { container } = render(<FermentationChart series={series()} stages={[stage({})]} />);
    expect(screen.getByText("Gap (daemon down)")).toBeInTheDocument();
    // The gap-bridge <path> elements are always present (five, one per
    // series -- beer temp, chamber temp, chamber target (Setpoint), beer
    // target, gravity -- all sharing styles.gapPath -- deliberately one
    // neutral treatment, not per-series color, so a gap is never confused
    // with a projection of the same series); only their `d` is conditional
    // on there being anything to bridge. The fixture's one gap
    // (2026-01-02 -> 2026-01-04) has a non-null value at both ends for at
    // least chamber_temp_f and effective_target_f, so at least one of the
    // five must draw a real M...L... segment (exact pixel placement is
    // geometry.test.ts's job, via buildGapPaths directly -- this just pins
    // that the chart actually wires gaps through to a real path).
    const gapPaths = Array.from(container.getElementsByClassName(styles.gapPath));
    expect(gapPaths).toHaveLength(5);
    expect(gapPaths.some((p) => /^M[\d.]+,[\d.]+ L[\d.]+,[\d.]+$/.test(p.getAttribute("d") ?? ""))).toBe(true);
  });

  it("draws the Setpoint line from chamber_target_f, not the beer target (effective_target_f)", () => {
    // Before this, the Setpoint line plotted effective_target_f (the beer
    // target) -- the same value already shown as the Beer temp tile's
    // "Target" sublabel, just under a different label. Holding
    // effective_target_f fixed while varying only chamber_target_f must
    // still change the rendered line -- proof the line is wired to the
    // right field, not a stale/unrelated one that happens to look right in
    // the default fixture above.
    const targetPathD = (container: HTMLElement) =>
      container.querySelector("path[stroke='var(--kr-plan)'][stroke-dasharray='4 3']")?.getAttribute("d");

    const { container: distinct } = render(
      <FermentationChart
        series={series({ chamber_target_f: [60, 60, 60, 60], effective_target_f: [66, 66, 66, 66] })}
        stages={[stage({})]}
      />,
    );
    const { container: matchingBeerTarget } = render(
      <FermentationChart
        series={series({ chamber_target_f: [66, 66, 66, 66], effective_target_f: [66, 66, 66, 66] })}
        stages={[stage({})]}
      />,
    );
    expect(targetPathD(distinct)).not.toEqual(targetPathD(matchingBeerTarget));
  });

  it("draws a Beer target line from effective_target_f, styled subtly like Setpoint but in the beer color family", () => {
    const beerTargetPathD = (container: HTMLElement) =>
      container.querySelector("path[stroke='var(--kr-accent)'][stroke-dasharray='4 3']")?.getAttribute("d");

    const { container: low } = render(
      <FermentationChart series={series({ effective_target_f: [60, 60, 60, 60] })} stages={[stage({})]} />,
    );
    const { container: high } = render(
      <FermentationChart series={series({ effective_target_f: [70, 70, 70, 70] })} stages={[stage({})]} />,
    );
    expect(beerTargetPathD(low)).toBeTruthy();
    expect(beerTargetPathD(low)).not.toEqual(beerTargetPathD(high));

    // Same dashed rhythm and stroke width as Setpoint (its beer-side
    // equivalent), distinguished by color/opacity, not by a different dash.
    const beerTarget = low.querySelector("path[stroke='var(--kr-accent)'][stroke-dasharray='4 3']");
    expect(beerTarget?.getAttribute("stroke-width")).toBe("1.5");
    expect(beerTarget?.getAttribute("opacity")).toBe("0.45");
    expect(screen.getAllByText("Beer target")[0]).toBeInTheDocument();
  });

  it("draws no gap-bridge path (empty d) when the series has no reported gaps", () => {
    const { container } = render(<FermentationChart series={series({ gaps: [] })} stages={[stage({})]} />);
    expect(screen.queryByText("Gap (daemon down)")).not.toBeInTheDocument();
    const gapPaths = Array.from(container.getElementsByClassName(styles.gapPath));
    expect(gapPaths).toHaveLength(5);
    gapPaths.forEach((p) => expect(p.getAttribute("d")).toBe(""));
  });
});
