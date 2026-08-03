"use client";

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export function Spinner({
  className,
  size = 14,
}: {
  className?: string;
  size?: number;
}) {
  return (
    <Loader2
      size={size}
      className={cn("shrink-0 animate-spin", className)}
      aria-hidden
    />
  );
}

export function LoadingLabel({
  children,
  size = 14,
  className,
}: {
  children: React.ReactNode;
  size?: number;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <Spinner size={size} />
      <span>{children}</span>
    </span>
  );
}
