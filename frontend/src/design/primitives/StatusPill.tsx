import type { ReactNode } from "react";
import styles from "./StatusPill.module.css";

// Color-based, not domain-based -- this is a generic primitive with no idea
// what "cooling" or "heating" mean; callers map their own domain state onto
// whichever tone reads right (see GettingStartedView.tsx's modeTone).
export type PillTone = "info" | "accent" | "positive" | "warn" | "danger" | "neutral";

interface Props {
  tone: PillTone;
  children: ReactNode;
  pulse?: boolean;
}

export function StatusPill({ tone, children, pulse = false }: Props) {
  const classes = [styles.pill, styles[tone], pulse ? styles.pulse : ""].filter(Boolean).join(" ");
  return (
    <span className={classes}>
      <span className={styles.dot} />
      {children}
    </span>
  );
}
