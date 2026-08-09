import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import styles from "./Dialog.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}

export function Dialog({ open, onClose, children, className }: Props) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handleCancel = (e: Event) => {
      e.preventDefault();
      onClose();
    };
    const handleClick = (e: MouseEvent) => {
      if (e.target === el) onClose();
    };
    el.addEventListener("cancel", handleCancel);
    el.addEventListener("click", handleClick);
    return () => {
      el.removeEventListener("cancel", handleCancel);
      el.removeEventListener("click", handleClick);
    };
  }, [onClose]);

  return (
    <dialog ref={ref} className={[styles.dialog, className].filter(Boolean).join(" ")}>
      {open && children}
    </dialog>
  );
}
