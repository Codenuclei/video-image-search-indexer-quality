"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown, type LucideIcon } from "lucide-react";
import { FaceThumb } from "@/components/ui";
import { cn } from "@/lib/utils";

export type FilterDropdownOption = {
  value: string;
  label: string;
  hint?: string;
  faceId?: number | null;
};

type MenuPlacement = {
  openUp: boolean;
  maxHeight: number;
  /** Fixed positioning when the trigger is near a scroll edge. */
  fixed: boolean;
  top: number;
  left: number;
  width: number;
};

const MENU_GAP = 6;
const MENU_MIN = 120;
const MENU_IDEAL = 256;

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
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  function measurePlacement(): MenuPlacement | null {
    const trigger = rootRef.current;
    if (!trigger) return null;
    const rect = trigger.getBoundingClientRect();
    const edge = 8;
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const spaceBelow = vh - rect.bottom - MENU_GAP - edge;
    const spaceAbove = rect.top - MENU_GAP - edge;
    const openUp = spaceBelow < MENU_MIN && spaceAbove > spaceBelow;
    const available = openUp ? spaceAbove : spaceBelow;
    // Prefer ideal height, but never exceed remaining viewport space (incl. bottom edge).
    const maxHeight = Math.min(MENU_IDEAL, Math.max(48, available));
    let top = openUp ? rect.top - MENU_GAP - maxHeight : rect.bottom + MENU_GAP;
    top = Math.max(edge, Math.min(top, vh - edge - maxHeight));
    const width = Math.max(rect.width, 224);
    const cappedWidth = Math.min(width, vw * 0.7, 18 * 16);
    const left = Math.max(edge, Math.min(rect.left, vw - cappedWidth - edge));
    return {
      openUp,
      maxHeight,
      fixed: true,
      top,
      left,
      width,
    };
  }

  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    setPlacement(measurePlacement());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node) && !menuRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    function onReposition() {
      setPlacement(measurePlacement());
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onReposition);
    window.addEventListener("scroll", onReposition, true);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
  }, [open]);

  const selected = options.find((o) => o.value === value);
  const isActive = active ?? (iconOnly ? value !== options[0]?.value : false);
  const selectedFaceId = selected?.faceId ?? null;

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
                "flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-full transition-colors",
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
        {iconOnly && selectedFaceId ? (
          <FaceThumb faceId={selectedFaceId} className="h-7 w-7 rounded-full object-cover" />
        ) : Icon ? (
          <Icon size={iconOnly ? 15 : 14} className={iconOnly ? undefined : "shrink-0 text-muted-foreground"} />
        ) : null}
        {!iconOnly && (
          <>
            {!Icon && selectedFaceId ? (
              <FaceThumb faceId={selectedFaceId} className="h-5 w-5 shrink-0 rounded-full object-cover" />
            ) : Icon ? (
              <Icon size={14} className="shrink-0 text-muted-foreground" />
            ) : null}
            <span className="max-w-[10rem] truncate">{selected?.label ?? "Select"}</span>
            <ChevronDown
              size={13}
              className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
            />
          </>
        )}
      </button>
      {open && placement && (
        <div
          ref={menuRef}
          className="z-[60] overflow-hidden rounded-xl border border-border bg-card py-1 shadow-lg"
          style={{
            position: "fixed",
            top: placement.top,
            left: placement.left,
            width: placement.width,
            maxWidth: "min(70vw, 18rem)",
          }}
        >
          <ul
            role="listbox"
            aria-label={title}
            className="overflow-y-auto overscroll-contain"
            style={{ maxHeight: placement.maxHeight }}
          >
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
                      "flex w-full min-w-0 items-center gap-2 overflow-hidden px-3 py-2 text-left text-xs transition-colors",
                      isSel
                        ? "bg-primary/10 font-medium text-primary"
                        : "text-foreground hover:bg-accent"
                    )}
                  >
                    {o.faceId ? (
                      <FaceThumb
                        faceId={o.faceId}
                        className="h-6 w-6 shrink-0 rounded-full object-cover ring-1 ring-border/60"
                      />
                    ) : o.value === "" ? (
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground">
                        ···
                      </span>
                    ) : null}
                    <span className="min-w-0 flex-1 truncate">{o.label}</span>
                    <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden>
                      {isSel ? <Check size={13} /> : null}
                    </span>
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
