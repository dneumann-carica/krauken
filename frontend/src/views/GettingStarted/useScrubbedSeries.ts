import type { SeriesResponse } from "../../api/types";

function lastDefined<T>(arr: (T | null)[]): T | null {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] !== null) return arr[i];
  }
  return null;
}

export interface ScrubbedSeries {
  /** The scrubbed sample's own timestamp, or null while not scrubbing. */
  ts: string | null;
  beer: number | null;
  chamber: number | null;
  gravity: number | null;
  target: number | null;
  mode: string;
  /** Whether this batch has EVER reported a gravity reading -- independent
   * of the current scrub position, unlike `gravity` above. Whether the
   * Gravity tile is shown at all is a structural fact about the batch (is a
   * gravity source mapped for it), not something that should flicker off
   * just because the sample under the scrub cursor happens to be null. */
  hasGravity: boolean;
}

/** Picks, for each of a fermentation's headline series, either the
 * scrubbed sample's value (while the chart is being scrubbed) or the
 * batch's latest-known one -- the same "scrubIndex present -> that sample,
 * else last non-null" choice the stat tiles all make, used to be five
 * near-identical ternaries repeated for beer/chamber/gravity/target/mode.
 * Named like a hook (called from a component body, one per render) even
 * though it's a plain derived-value function with no state/effects of its
 * own -- there's nothing here that needs React's hook machinery. */
export function useScrubbedSeries(series: SeriesResponse | undefined, scrubIndex: number | null): ScrubbedSeries {
  const scrubTs = scrubIndex !== null && series ? series.ts[scrubIndex] : null;
  const scrubbing = scrubTs !== null && series !== undefined;
  const lastGravity = series ? lastDefined(series.gravity) : null;
  const lastMode = series?.chamber_mode[series.chamber_mode.length - 1] ?? "idle";
  return {
    ts: scrubTs,
    beer: scrubbing ? series!.beer_temp_f[scrubIndex!] : series ? lastDefined(series.beer_temp_f) : null,
    chamber: scrubbing ? series!.chamber_temp_f[scrubIndex!] : series ? lastDefined(series.chamber_temp_f) : null,
    gravity: scrubbing ? series!.gravity[scrubIndex!] : lastGravity,
    target: scrubbing ? series!.effective_target_f[scrubIndex!] : series ? lastDefined(series.effective_target_f) : null,
    mode: scrubbing ? series!.chamber_mode[scrubIndex!] : lastMode,
    hasGravity: lastGravity !== null,
  };
}
