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

/**
 * shadcn-style ToggleGroup: muted track, equal segments, raised selected pill.
 */
export function RoleSelector({
  role,
  disabled,
  onChange,
  className,
}: {
  role: PersonRole;
  disabled?: boolean;
  onChange: (role: PersonRole) => void;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div
        role="group"
        aria-label="Role"
        className="inline-flex w-full rounded-lg border border-border/80 bg-muted/50 p-0.5"
      >
        {OPTIONS.map((opt) => {
          const selected = role === opt.value;
          return (
            <button
              key={opt.label}
              type="button"
              disabled={disabled}
              aria-pressed={selected}
              onClick={() => onChange(opt.value)}
              className={cn(
                "inline-flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] font-medium transition-all duration-150 disabled:pointer-events-none disabled:opacity-50",
                selected
                  ? opt.value === "student"
                    ? "bg-sky-600 text-white shadow-sm"
                    : opt.value === "non_student"
                      ? "bg-amber-600 text-white shadow-sm"
                      : "bg-background text-foreground shadow-sm ring-1 ring-border/60"
                  : "text-muted-foreground hover:text-foreground"
              )}
            >
              <opt.icon size={12} aria-hidden className="shrink-0 opacity-90" />
              <span className="truncate">{opt.label}</span>
            </button>
          );
        })}
      </div>
      {disabled && <Spinner size={12} className="shrink-0 text-muted-foreground" />}
    </div>
  );
}
