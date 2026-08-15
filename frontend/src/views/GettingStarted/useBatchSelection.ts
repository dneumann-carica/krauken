import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { useAlerts, useAppState, useFermentation, useFermentations, useSeries } from "../../api/queries";

/** Resolves which batch this page is viewing, whether it's the live one,
 * and the detail/series/alerts queries for it -- plus the fetch/refetch
 * choreography a live view needs around that.
 *
 * The viewed batch lives in the URL, not plain component state -- a
 * refresh must keep showing whatever you were looking at, not silently
 * fall back to the default. Only in the absence of that (a fresh load
 * with no ?batch= at all) does it pick a default, and that default prefers
 * whatever's actively running over merely "most recently started" --
 * fermentations_list sorts by started_at, which a SimulatorClock-driven
 * batch can't be trusted to place correctly relative to wall-clock-timed
 * ones. */
export function useBatchSelection() {
  const state = useAppState();
  const fermentations = useFermentations();
  const [searchParams, setSearchParams] = useSearchParams();

  const viewedIdParam = searchParams.get("batch");
  const parsedViewedId = viewedIdParam !== null ? Number(viewedIdParam) : NaN;
  const defaultBatchId = state.data?.active_fermentation_id ?? fermentations.data?.[0]?.id;
  const batchId = Number.isFinite(parsedViewedId) ? parsedViewedId : defaultBatchId;
  const isLive = state.data?.active_fermentation_id !== undefined && state.data?.active_fermentation_id === batchId;

  const detail = useFermentation(batchId, { live: isLive });
  const series = useSeries(batchId, { live: isLive });
  const alerts = useAlerts(isLive ? batchId : undefined, { live: isLive });

  // Independent polling intervals (state every 5s, detail/series on their
  // own slower cadences while live) mean isLive can flip false the moment a
  // batch completes, stopping detail/series polling, BEFORE their own next
  // scheduled poll would have picked up the truly-final state -- the
  // observed symptom was the chart freezing mid-stage with a stale NOW
  // marker and projection that never cleared. One extra fetch right on the
  // true->false transition closes that gap; nothing needs to keep polling
  // after that, since the daemon's completion writes (final sample, status,
  // stage state) all land in the same control tick, not staggered later.
  // fermentations gets the identical treatment for the identical reason --
  // it has no polling of its own at all (see useFermentations), so without
  // this its cached list keeps whatever status the batch had the last time
  // this hook mounted/a mutation invalidated it. The observed symptom there
  // was BatchTitleMenu's dropdown still labeling a just-completed batch
  // "Active" -- the page's own header (reading `detail`, not this list) had
  // already updated to "Batch complete."
  // Deliberately NOT alerts: unlike detail/series (always queried on
  // batchId, gated only on *polling* via live:isLive), alerts is gated on
  // *identity* too (isLive ? batchId : undefined) since it's never shown
  // for a non-live batch -- calling .refetch() on it here bypasses its own
  // enabled:false and fires with fermentationId already undefined.
  const wasLiveRef = useRef(isLive);
  useEffect(() => {
    if (wasLiveRef.current && !isLive) {
      detail.refetch();
      series.refetch();
      fermentations.refetch();
    }
    wasLiveRef.current = isLive;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLive]);

  return { state, fermentations, batchId, isLive, detail, series, alerts, setSearchParams };
}
