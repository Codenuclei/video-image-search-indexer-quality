"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type FilterDropdownOption = {
  value: string;
  label: string;
  hint?: string;
};

export function FilterDropdown({
  icon: Icon,
  value,
  options,
  onChange,
  disabled,
  title,
  iconOnly = false,
  active,
  className,
}: {
  icon?: LucideIcon;
  value: string;
  options: FilterDropdownOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  title?: string;
  /** Render as a round icon button (header style) instead of a labeled pill. */
  iconOnly?: boolean;
  /** Force the active tint in iconOnly mode (defaults to non-default value). */
  active?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const selected = options.find((o) => o.value === value);
  const isActive = active ?? (iconOnly ? value !== options[0]?.value : false);

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        title={title ?? selected?.label}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          iconOnly
            ? cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
                isActive
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                disabled && "opacity-40"
              )
            : cn(
                "flex h-9 items-center gap-1.5 rounded-full border border-border bg-card pl-3.5 pr-3 text-xs font-medium text-foreground shadow-sm outline-none transition-colors hover:border-muted-foreground/30 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20 disabled:cursor-not-allowed disabled:opacity-60",
                open && "border-ring ring-2 ring-ring/20"
              )
        )}
      >
        {Icon && <Icon size={iconOnly ? 15 : 14} className={iconOnly ? undefined : "shrink-0 text-muted-foreground"} />}
        {!iconOnly && (
          <>
            <span className="max-w-[10rem] truncate">{selected?.label ?? "Select"}</span>
            <ChevronDown
              size={13}
              className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
            />
          </>
        )}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-40 mt-1.5 w-52 max-w-[70vw] overflow-hidden rounded-xl border border-border bg-card py-1 shadow-lg">
          <ul role="listbox" aria-label={title} className="max-h-64 overflow-y-auto">
            {options.map((o) => {
              const isSel = o.value === value;
              return (
                <li key={o.value === "" ? "__default__" : o.value}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSel}
                    title={o.hint}
                    onClick={() => {
                      onChange(o.value);
                      setOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors",
                      isSel
                        ? "bg-primary/10 font-medium text-primary"
                        : "text-foreground hover:bg-accent"
                    )}
                  >
                    <span className="min-w-0 flex-1 truncate">{o.label}</span>
                    {isSel && <Check size={13} className="shrink-0" aria-hidden />}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
