import type { ReactNode } from "react";
import styles from "./MetricStat.module.css";

// Color-based, not domain-based -- this is a generic primitive with no idea
// what a "setpoint" is; callers map their own domain meaning onto whichever
// accent reads right (see GettingStartedView.tsx's Setpoint tile, which
// picks "gray" to match the chart's own plan-line color, --kr-plan).
type Accent = "orange" | "navy" | "blue" | "green" | "gray";
const ACCENT_CLASS: Record<Accent, string> = {
  orange: styles.accentOrange,
  navy: styles.accentNavy,
  blue: styles.accentBlue,
  green: styles.accentGreen,
  gray: styles.accentGray,
};
const ALIGN_CLASS = { left: "", center: styles.alignCenter, right: styles.alignRight };

interface Props {
  value: ReactNode;
  label: ReactNode;
  sublabel?: ReactNode;
  align?: "left" | "center" | "right";
  accent?: Accent;
}

export function MetricStat({ value, label, sublabel, align = "left", accent = "orange" }: Props) {
  return (
    <div className={[styles.root, ALIGN_CLASS[align]].filter(Boolean).join(" ")}>
      <div className={[styles.value, ACCENT_CLASS[accent]].join(" ")}>{value}</div>
      <div className={styles.label}>{label}</div>
      {sublabel && <div className={styles.sublabel}>{sublabel}</div>}
    </div>
  );
}
