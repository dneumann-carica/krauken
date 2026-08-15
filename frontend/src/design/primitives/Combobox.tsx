import { useEffect, useId, useMemo, useRef, useState } from "react";
import styles from "./Combobox.module.css";

export interface ComboboxOption {
  id: string;
  label: string;
  /** Optional group label -- rendered as a heading, filtered out entirely
   * once none of its options match the query rather than left dangling
   * with an empty body. Omit on every option to get a single flat list. */
  group?: string;
}

interface Props {
  options: ComboboxOption[];
  value: string | undefined;
  onChange: (id: string) => void;
  placeholder?: string;
  /** Fixed group render order (e.g. "most common first", not
   * alphabetical). Groups present in the data but absent here are
   * appended afterward in their first-seen order, so a new group never
   * silently disappears just because this list wasn't updated for it. */
  groupOrder?: string[];
  className?: string;
  id?: string;
}

/** Hand-rolled type-to-filter combobox, not a native <select> -- built
 * specifically for the yeast picker once it grew past 150 options (a
 * flat/grouped <select> has no filtering of its own and OS-native
 * dropdown rendering that varies wildly by browser), but generic: any
 * id/label/group list works. No dependency pulled in for this -- same
 * "plain React, hand-rolled" precedent as Dialog (native <dialog>) and
 * Switch (a styled <button role="switch">) elsewhere in primitives/.
 *
 * Standard ARIA combobox pattern (role="combobox" input +
 * role="listbox" panel + role="option" items, aria-activedescendant
 * tracking keyboard highlight) rather than a plain text input with a
 * homegrown popup -- screen readers already know this pattern.
 *
 * Selection-vs-blur ordering: options use onMouseDown
 * preventDefault so a click registers before the input's blur handler
 * would otherwise close the panel and discard it -- the classic
 * combobox footgun if selection were wired to onClick alone. */
export function Combobox({ options, value, onChange, placeholder, groupOrder, className, id }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlighted, setHighlighted] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  const selected = options.find((o) => o.id === value);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.label.toLowerCase().includes(q));
  }, [options, query]);

  const groups = useMemo(() => {
    const present = [...new Set(filtered.map((o) => o.group ?? ""))];
    const order = groupOrder
      ? [...groupOrder.filter((g) => present.includes(g)), ...present.filter((g) => !groupOrder.includes(g))]
      : present;
    return order.map((group) => ({ group, items: filtered.filter((o) => (o.group ?? "") === group) }));
  }, [filtered, groupOrder]);

  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  // Outside click closes without discarding the current selection --
  // only Enter/click-on-an-option ever calls onChange. A bare click
  // elsewhere is a "never mind," not an implicit choice.
  useEffect(() => {
    if (!open) return;
    function onDocMouseDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  function openPanel() {
    setOpen(true);
    setQuery("");
    const idx = flat.findIndex((o) => o.id === value);
    setHighlighted(idx >= 0 ? idx : 0);
  }

  function choose(optId: string) {
    onChange(optId);
    setOpen(false);
    setQuery("");
    inputRef.current?.blur();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter") {
        e.preventDefault();
        openPanel();
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlighted((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlighted((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const opt = flat[highlighted];
      if (opt) choose(opt.id);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      setQuery("");
      inputRef.current?.blur();
    }
  }

  const displayValue = open ? query : (selected?.label ?? "");
  const activeOptionId = open && flat[highlighted] ? `${listboxId}-${flat[highlighted].id}` : undefined;

  return (
    <div ref={rootRef} className={[styles.root, className].filter(Boolean).join(" ")}>
      <input
        id={id}
        ref={inputRef}
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-activedescendant={activeOptionId}
        className={styles.input}
        value={displayValue}
        placeholder={placeholder}
        onFocus={openPanel}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
          setHighlighted(0);
        }}
        onKeyDown={onKeyDown}
      />
      {open && (
        <ul id={listboxId} role="listbox" className={styles.panel}>
          {flat.length === 0 && <li className={styles.empty}>No matches</li>}
          {groups.map(
            ({ group, items }) =>
              items.length > 0 && (
                <li key={group || "_"} role="presentation">
                  {group && <div className={styles.groupLabel}>{group}</div>}
                  <ul role="presentation" className={styles.groupList}>
                    {items.map((o) => {
                      const idx = flat.indexOf(o);
                      return (
                        <li
                          key={o.id}
                          id={`${listboxId}-${o.id}`}
                          role="option"
                          aria-selected={o.id === value}
                          className={[styles.option, idx === highlighted ? styles.optionHighlighted : ""].join(" ")}
                          onMouseDown={(e) => e.preventDefault()}
                          onMouseEnter={() => setHighlighted(idx)}
                          onClick={() => choose(o.id)}
                        >
                          {o.label}
                        </li>
                      );
                    })}
                  </ul>
                </li>
              ),
          )}
        </ul>
      )}
    </div>
  );
}
