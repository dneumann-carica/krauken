import type { ReactNode } from "react";
import styles from "./MetricStat.module.css";

type Accent = "orange" | "navy" | "blue" | "green" | "plan";
const ACCENT_CLASS: Record<Accent, string> = {
  orange: styles.accentOrange,
  navy: styles.accentNavy,
  blue: styles.accentBlue,
  green: styles.accentGreen,
  plan: styles.accentPlan,
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
