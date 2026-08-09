import type { StageInput } from "../api/types";

// Mirrors krauken/db/seed.py's DEMO_STAGES skeleton -- the same 5-stage
// shape (primary/free_rise/diacetyl_rest/conditioning/cold_crash) and the
// same authoring defaults (gravity threshold, ramp/hold hours), just built
// from whichever yeast preset the user picked instead of being hardcoded
// to US-05. A user can still edit any of this afterward via the running
// profile's edit mechanism (see FermentationPlanDialog).
export function buildDefaultStages(stageDefaults: Record<string, Record<string, number>>): StageInput[] {
  const primary = stageDefaults.primary ?? {};
  const freeRise = stageDefaults.free_rise ?? {};
  const diacetyl = stageDefaults.diacetyl_rest ?? {};
  const conditioning = stageDefaults.conditioning ?? {};
  const coldCrash = stageDefaults.cold_crash ?? {};

  return [
    {
      stage_type: "primary",
      name: "Primary fermentation",
      temp_mode: "constant",
      temp_f: primary.temp_f ?? 66,
      end_mode: "gravity",
      gravity_hi: 1.016,
      gravity_stable_hours: 24,
      max_hours: 240,
      advance_mode: "auto",
    },
    {
      stage_type: "free_rise",
      name: "Free rise",
      temp_mode: "stepped",
      temp_from_f: freeRise.temp_from_f ?? 66,
      temp_to_f: freeRise.temp_to_f ?? 70,
      ramp_hours: 24,
      end_mode: "time",
      end_hours: 24,
      advance_mode: "auto",
    },
    {
      stage_type: "diacetyl_rest",
      name: "Diacetyl rest",
      temp_mode: "constant",
      temp_f: diacetyl.temp_f ?? 70,
      end_mode: "time",
      end_hours: 48,
      advance_mode: "auto",
    },
    {
      stage_type: "conditioning",
      name: "Conditioning",
      temp_mode: "constant",
      temp_f: conditioning.temp_f ?? 68,
      end_mode: "time",
      end_hours: 168,
      advance_mode: "auto",
    },
    {
      stage_type: "cold_crash",
      name: "Cold crash",
      temp_mode: "stepped",
      temp_from_f: coldCrash.temp_from_f ?? 68,
      temp_to_f: coldCrash.temp_to_f ?? 38,
      ramp_hours: 96,
      end_mode: "time",
      end_hours: 96,
      advance_mode: "auto",
    },
  ];
}
