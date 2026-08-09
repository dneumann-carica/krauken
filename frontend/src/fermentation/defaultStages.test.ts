import { describe, expect, it } from "vitest";
import { buildDefaultStages } from "./defaultStages";

describe("buildDefaultStages", () => {
  it("produces the same 5-stage skeleton as the backend's demo profile", () => {
    const stages = buildDefaultStages({
      primary: { temp_f: 66 },
      free_rise: { temp_from_f: 66, temp_to_f: 70 },
      diacetyl_rest: { temp_f: 70 },
      conditioning: { temp_f: 68 },
      cold_crash: { temp_from_f: 68, temp_to_f: 38 },
    });
    expect(stages.map((s) => s.stage_type)).toEqual([
      "primary",
      "free_rise",
      "diacetyl_rest",
      "conditioning",
      "cold_crash",
    ]);
    expect(stages[0].end_mode).toBe("gravity");
    expect(stages[0].temp_f).toBe(66);
    expect(stages.slice(1).every((s) => s.end_mode === "time")).toBe(true);
  });

  it("falls back to sane defaults when a preset omits a stage entirely (e.g. the custom preset)", () => {
    const stages = buildDefaultStages({});
    expect(stages).toHaveLength(5);
    expect(stages[0].temp_f).toBe(66);
    expect(stages[4].temp_to_f).toBe(38);
  });
});
