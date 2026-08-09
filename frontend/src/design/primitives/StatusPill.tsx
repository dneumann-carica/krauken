import type { ReactNode } from "react";
import styles from "./StatusPill.module.css";

export type PillTone = "cool" | "heat" | "idle" | "warn" | "danger" | "neutral";

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
