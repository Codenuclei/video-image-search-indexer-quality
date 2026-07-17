"use client";

import { cn } from "@/lib/utils";

/**
 * Trash2-style dustbin whose lid pops open on hover (via `group/trash` on a
 * parent) and wiggles while `animating` (delete in flight). Keyframes live in
 * globals.css (`bin-lid-open`, `bin-wiggle`).
 */
export function AnimatedTrash({
  size = 14,
  animating = false,
  className,
}: {
  size?: number;
  animating?: boolean;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("shrink-0", animating && "trash-animating", className)}
      aria-hidden
    >
      <g className="trash-lid">
        <path d="M3 6h18" />
        <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      </g>
      <g className="trash-body">
        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
        <line x1="10" y1="11" x2="10" y2="17" />
        <line x1="14" y1="11" x2="14" y2="17" />
      </g>
    </svg>
  );
}
