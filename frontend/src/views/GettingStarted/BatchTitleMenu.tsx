import { useEffect, useRef } from "react";
import type { FermentationSummary } from "../../api/types";
import { Tag } from "../../design/primitives";
import styles from "./GettingStartedView.module.css";

function fmtMenuDate(iso: string, includeYear: boolean): string {
  return new Date(iso).toLocaleDateString(
    undefined,
    includeYear ? { month: "short", day: "numeric", year: "numeric" } : { month: "short", day: "numeric" },
  );
}

// The title-menu batch list's secondary line -- "Active" for the one
// currently running (a date range would just show "started, still going",
// which is what "Active" already says more plainly), a real start–end
// range for anything finished. The end date gets its year appended once
// it's no longer this year -- otherwise last December's batch and one
// from three weeks ago both just read "Dec 30", indistinguishable at a
// glance. The start date never does; a range implies "same era as its own
// end date" well enough on its own.
function fmtMenuDateRange(f: FermentationSummary): string {
  if (f.status === "active") return "Active";
  if (!f.ended_at) return fmtMenuDate(f.started_at, false);
  // Not just "ended before this year" -- an accelerated SimulatorClock
  // batch can just as easily race PAST the current year into next year
  // (started from a real-now anchor, then ticked through weeks of
  // simulated time in seconds). Either direction is equally ambiguous
  // without a year shown, and equally capable of producing a menu order
  // that looks wrong at a glance if you assume everything's "this year."
  const differentYear = new Date(f.ended_at).getFullYear() !== new Date().getFullYear();
  return `${fmtMenuDate(f.started_at, false)} – ${fmtMenuDate(f.ended_at, differentYear)}`;
}

interface Props {
  batchName: string;
  isDemo: boolean;
  canStartNew: boolean;
  cannotStartReason: string;
  fermentations: FermentationSummary[] | undefined;
  currentBatchId: number | undefined;
  onStartNew: () => void;
  onSelectBatch: (id: number) => void;
}

/** The page title's own dropdown -- "Start a new fermentation" plus every
 * past/active batch to switch to. Self-contained click-outside-to-close
 * behavior (native <details> has no such thing on its own -- only
 * reopening the <summary> toggles it back off) since nothing outside this
 * component needs to know or control whether the menu is open. */
export function BatchTitleMenu({
  batchName,
  isDemo,
  canStartNew,
  cannotStartReason,
  fermentations,
  currentBatchId,
  onStartNew,
  onSelectBatch,
}: Props) {
  const menuRef = useRef<HTMLDetailsElement>(null);

  // A capturing pointerdown listener on the whole document, closing
  // whenever the click lands outside the element, is the standard way to
  // bolt "click outside to close" onto a native <details>. Attached
  // unconditionally (not just while open) since it's cheap and the element
  // itself is stable for the component's whole lifetime -- no need to
  // add/remove it every open/close cycle.
  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      const el = menuRef.current;
      if (el && el.open && e.target instanceof Node && !el.contains(e.target)) {
        el.removeAttribute("open");
      }
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, []);

  function closeMenu() {
    menuRef.current?.removeAttribute("open");
  }

  return (
    <div className={styles.headerTitleCol}>
      <div className={styles.eyebrow}>The Krauken · Release the Krausen</div>
      <details ref={menuRef} className={styles.titleMenu}>
        <summary className={styles.titleMenuButton}>
          <span className={styles.titleRow}>
            <h1 className={styles.title}>{batchName}</h1>
            {isDemo && (
              <Tag tone="gray" size="sm">
                Demo
              </Tag>
            )}
          </span>
          <span className={styles.chevronCircle}>
            {/* An inline SVG, not the Unicode ⌄ glyph -- that character's
                ink sits visibly above center in most fonts (glyph metrics,
                not a flex-centering bug), so no amount of align-items:
                center on .chevronCircle could fix it. A hand-drawn path
                centers by construction instead of by font luck. */}
            <svg className={styles.chevron} viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        </summary>
        <div className={styles.titleMenuPanel}>
          <button
            type="button"
            className={styles.titleMenuAction}
            disabled={!canStartNew}
            onClick={() => {
              if (!canStartNew) return;
              closeMenu();
              onStartNew();
            }}
          >
            <span>Start a new fermentation</span>
            {!canStartNew && <span className={styles.titleMenuNote}>{cannotStartReason}</span>}
          </button>
          <div className={styles.titleMenuDivider} />
          <div className={styles.titleMenuLabel}>Fermentations</div>
          {fermentations && fermentations.length > 0 ? (
            fermentations.map((f) => (
              <button
                key={f.id}
                type="button"
                className={`${styles.titleMenuItem} ${f.id === currentBatchId ? styles.titleMenuItemActive : ""}`}
                onClick={() => {
                  onSelectBatch(f.id);
                  closeMenu();
                }}
              >
                <span>{f.name}</span>
                <span className={styles.titleMenuNote}>{fmtMenuDateRange(f)}</span>
              </button>
            ))
          ) : (
            <div className={styles.titleMenuNote}>Your own batches appear here once you start one.</div>
          )}
        </div>
      </details>
    </div>
  );
}
