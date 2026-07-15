"use client";

import type { PersonRole } from "@/lib/api";
import { cn } from "@/lib/utils";

const OPTIONS: { value: PersonRole; label: string }[] = [
  { value: null, label: "Unset" },
  { value: "student", label: "Student" },
  { value: "non_student", label: "Non-student" },
];

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
    <div className={cn("flex flex-wrap gap-2.5", className)}>
      {OPTIONS.map((opt) => (
        <button
          key={opt.label}
          type="button"
          disabled={disabled}
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
            role === opt.value
              ? opt.value === "student"
                ? "bg-blue-600 text-white"
                : opt.value === "non_student"
                  ? "bg-amber-600 text-white"
                  : "bg-muted text-foreground"
              : "bg-muted/60 text-muted-foreground hover:bg-muted"
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
