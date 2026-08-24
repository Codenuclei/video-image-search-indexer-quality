"use client";

import type { LucideIcon } from "lucide-react";
import { CircleDashed, GraduationCap, UserRound } from "lucide-react";
import type { PersonRole } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/spinner";

const OPTIONS: { value: PersonRole; label: string; icon: LucideIcon }[] = [
  { value: null, label: "Unset", icon: CircleDashed },
  { value: "student", label: "Student", icon: GraduationCap },
  { value: "non_student", label: "Non-student", icon: UserRound },
];

function roleButtonClass(selected: boolean, value: PersonRole, card: boolean) {
  return cn(
    "rounded-md font-medium transition-all duration-150 disabled:pointer-events-none disabled:opacity-50",
    card
      ? "inline-flex min-w-0 items-center justify-center gap-1 px-1 py-1.5 text-[10px] leading-none whitespace-nowrap"
      : "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap px-2.5 py-1.5 text-[11px]",
    selected
      ? value === "student"
        ? "bg-sky-600 text-white shadow-sm"
        : value === "non_student"
          ? "bg-amber-600 text-white shadow-sm"
          : "bg-background text-foreground shadow-sm ring-1 ring-border/60"
      : "text-muted-foreground hover:text-foreground"
  );
}

/**
 * shadcn-style ToggleGroup: muted track, equal segments, raised selected pill.
 * Use variant="card" on people cards: compact 3-up grid, single-line labels.
 */
export function RoleSelector({
  role,
  disabled,
  onChange,
  className,
  variant = "inline",
}: {
  role: PersonRole;
  disabled?: boolean;
  onChange: (role: PersonRole) => void;
  className?: string;
  variant?: "inline" | "card";
}) {
  const card = variant === "card";

  return (
    <div className={cn("flex w-full min-w-0 items-center gap-2", className)}>
      <div
        role="group"
        aria-label="Role"
        className={cn(
          "w-full min-w-0 rounded-lg border border-border/80 bg-muted/50 p-0.5",
          card ? "grid grid-cols-3 gap-0.5" : "inline-flex max-w-full flex-wrap gap-1"
        )}
      >
        {OPTIONS.map((opt) => {
          const selected = role === opt.value;
          return (
            <button
              key={opt.label}
              type="button"
              disabled={disabled}
              aria-pressed={selected}
              title={opt.label}
              onClick={() => onChange(opt.value)}
              className={roleButtonClass(selected, opt.value, card)}
            >
              <opt.icon size={card ? 10 : 12} aria-hidden className="shrink-0 opacity-90" />
              <span>{opt.label}</span>
            </button>
          );
        })}
      </div>
      {disabled && <Spinner size={12} className="shrink-0 text-muted-foreground" />}
    </div>
  );
}
