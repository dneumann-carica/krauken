import type { HTMLAttributes, ReactNode } from "react";
import styles from "./Tag.module.css";

type Tone = "gray" | "orange" | "blue" | "navy" | "green" | "amber" | "red";

interface Props extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  size?: "sm" | "md";
  icon?: ReactNode;
}

export function Tag({ tone = "gray", size = "md", icon, className, children, ...rest }: Props) {
  const classes = [styles.tag, styles[tone], size === "sm" ? styles.sm : "", className].filter(Boolean).join(" ");
  return (
    <span className={classes} {...rest}>
      {icon}
      {children}
    </span>
  );
}
