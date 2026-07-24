"use client";

import type { CarouselPresetItem } from "@/lib/api";
import { cn } from "@/lib/utils";

export function PresetChipMultiSelect({
  title,
  hint,
  items,
  selected,
  onToggle,
  onExpand,
  expanding,
  reveal,
}: {
  title: string;
  hint: string;
  items: CarouselPresetItem[];
  selected: string[];
  onToggle: (id: string) => void;
  onExpand?: () => void;
  expanding?: boolean;
  /** Staggered entrance when hooks/topics auto-surface */
  reveal?: boolean;
}) {
  return (
    <div className={cn("space-y-3", reveal && "studio-hooks-reveal")}>
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className="studio-section-label">{title}</p>
          <p className="mt-1 text-sm font-medium text-muted-foreground">{hint}</p>
        </div>
        {onExpand && (
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            onClick={onExpand}
            disabled={expanding}
          >
            {expanding ? "Expanding…" : "Expand more"}
          </button>
        )}
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item, i) => {
          const on = selected.includes(item.id);
          return (
            <button
              key={item.id}
              type="button"
              title={item.blurb}
              onClick={() => onToggle(item.id)}
              data-on={on ? "true" : "false"}
              className={cn(
                "studio-preset",
                reveal && "studio-fade-in"
              )}
              style={reveal ? { animationDelay: `${Math.min(i, 7) * 40}ms` } : undefined}
            >
              <span className="studio-preset-label">{item.label}</span>
              <span className="studio-preset-blurb line-clamp-2">{item.blurb}</span>
            </button>
          );
        })}
      </div>
      {selected.length > 0 && (
        <p className="text-xs font-medium text-muted-foreground">
          {selected.length} selected
          {selected.length >= 5 && selected.length <= 8 ? " · good range for a carousel" : ""}
        </p>
      )}
    </div>
  );
}
