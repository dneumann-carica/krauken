import { useId } from "react";
import styles from "./Switch.module.css";

interface Props {
  checked: boolean;
  onChange?: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
}

export function Switch({ checked, onChange, disabled = false, label }: Props) {
  const id = useId();
  const control = (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      disabled={disabled}
      onClick={() => !disabled && onChange?.(!checked)}
      className={[styles.track, checked ? styles.trackOn : ""].join(" ")}
    >
      <span className={[styles.knob, checked ? styles.knobOn : ""].join(" ")} />
    </button>
  );
  if (!label) return control;
  return (
    <label htmlFor={id} className={styles.label}>
      {control}
      <span className={styles.labelText}>{label}</span>
    </label>
  );
}
