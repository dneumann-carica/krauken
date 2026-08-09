import type { ButtonHTMLAttributes, ReactNode } from "react";
import styles from "./Button.module.css";

type Variant = "primary" | "secondary" | "outline" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  fullWidth?: boolean;
  children?: ReactNode;
}

export function Button({ variant = "primary", size = "md", fullWidth = false, className, ...rest }: Props) {
  const classes = [styles.button, styles[variant], styles[size], fullWidth ? styles.fullWidth : "", className]
    .filter(Boolean)
    .join(" ");
  return <button type="button" className={classes} {...rest} />;
}
