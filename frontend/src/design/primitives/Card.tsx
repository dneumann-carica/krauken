import type { HTMLAttributes, ReactNode } from "react";
import styles from "./Card.module.css";

interface Props extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  padding?: "none" | "sm" | "md" | "lg";
  variant?: "default" | "muted";
}

const PAD_CLASS = { none: styles.padNone, sm: styles.padSm, md: styles.padMd, lg: styles.padLg };

export function Card({ title, subtitle, actions, padding = "md", variant = "default", className, children, ...rest }: Props) {
  const classes = [styles.card, PAD_CLASS[padding], variant === "muted" ? styles.muted : "", className]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={classes} {...rest}>
      {(title || actions) && (
        <div className={styles.header}>
          <div>
            {title && <h4 className={styles.title}>{title}</h4>}
            {subtitle && <div className={styles.subtitle}>{subtitle}</div>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </div>
  );
}
