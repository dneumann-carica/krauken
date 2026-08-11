import { describe, expect, it } from "vitest";
import { applyReorder, blankStage, lastTempF, swapAdjacent } from "./FermentationPlanDialog";
import type { StageForm } from "./FermentationPlanDialog";

// Minimal, deliberately loose fixture -- only the fields applyReorder/
// swapAdjacent actually read (key, locked, disabled) are meaningful per
// case; everything else is a fixed placeholder.
function stage(key: string, overrides: Partial<StageForm> = {}): StageForm {
  return {
    key,
    name: key,
    on: true,
    locked: false,
    disabled: false,
    rawState: "pending",
    tempMode: "constant",
    tempF: "68",
    tempFrom: "",
    tempTo: "",
    rampHours: "",
    endMode: "time",
    endHours: "24",
    holdTemp: "",
    holdHours: "",
    gravHi: "",
    gravH: "",
    auto: true,
    ...overrides,
  };
}

describe("blankStage", () => {
  it("continues at the previous stage's temperature", () => {
    expect(blankStage(70).temp_f).toBe(70);
  });

  it("falls back to a sane default when there's no previous stage to continue from", () => {
    expect(blankStage(null).temp_f).toBe(68);
  });

  it("carries no implied brewing semantics -- plain constant/time defaults only", () => {
    const s = blankStage(60);
    expect(s.temp_mode).toBe("constant");
    expect(s.end_mode).toBe("time");
    expect(s.end_hours).toBe(24);
    expect(s.advance_mode).toBe("auto");
  });
});

describe("lastTempF", () => {
  it("reads the constant temp for a constant-mode stage", () => {
    expect(lastTempF(stage("a", { tempMode: "constant", tempF: "70" }))).toBe(70);
  });

  it("reads the arrival temp (tempTo), not the starting one, for a stepped stage", () => {
    expect(lastTempF(stage("a", { tempMode: "stepped", tempFrom: "68", tempTo: "38" }))).toBe(38);
  });

  it("is null when there's no stage at all (an empty plan)", () => {
    expect(lastTempF(undefined)).toBeNull();
  });
});

describe("swapAdjacent", () => {
  it("swaps exactly the two given indices, leaving the rest untouched", () => {
    expect(swapAdjacent(["a", "b", "c", "d"], 1, 2)).toEqual(["a", "c", "b", "d"]);
  });

  it("does not mutate the input array", () => {
    const input = ["a", "b"];
    swapAdjacent(input, 0, 1);
    expect(input).toEqual(["a", "b"]);
  });
});

describe("applyReorder", () => {
  it("permutes only the reorderable stages, leaving a locked stage's position untouched", () => {
    const primary = stage("primary", { locked: true });
    const a = stage("a");
    const b = stage("b");
    const all = [primary, a, b];
    const newOrder = swapAdjacent([a, b], 0, 1); // [b, a]

    const result = applyReorder(all, newOrder);
    expect(result.map((s) => s.key)).toEqual(["primary", "b", "a"]);
  });

  it("leaves a disabled (already-finished) stage's position untouched even mid-list", () => {
    const running = stage("running", { locked: true });
    const finished = stage("finished", { disabled: true });
    const a = stage("a");
    const b = stage("b");
    // A real fermentation never actually interleaves a finished stage
    // between two reorderable ones (finished/running always sort earliest
    // by seq) -- this pins the function's own contract regardless, since
    // nothing about its logic assumes contiguity.
    const all = [running, finished, a, b];
    const newOrder = swapAdjacent([a, b], 0, 1);

    const result = applyReorder(all, newOrder);
    expect(result.map((s) => s.key)).toEqual(["running", "finished", "b", "a"]);
  });

  it("is a no-op when the reorderable subset's order doesn't change", () => {
    const primary = stage("primary", { locked: true });
    const a = stage("a");
    const b = stage("b");
    const all = [primary, a, b];

    const result = applyReorder(all, [a, b]);
    expect(result.map((s) => s.key)).toEqual(["primary", "a", "b"]);
  });
});
