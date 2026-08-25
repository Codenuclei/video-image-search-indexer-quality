"use client";

import type { IndexStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LoadingLabel } from "@/components/spinner";

function formatLaneFiles(files: string[], limit = 2): string {
  if (!files.length) return "idle";
  const shown = files.slice(0, limit).join(" · ");
  const extra = files.length > limit ? ` (+${files.length - limit})` : "";
  return `${shown}${extra}`;
}

export function isLiveIndexingActive(status: IndexStatus | null | undefined): boolean {
  if (!status) return false;
  const imageActive = status.image_slots?.active ?? status.active_image_jobs ?? 0;
  const videoActive = status.video_slots?.active ?? status.active_video_jobs ?? 0;
  return status.is_running || imageActive > 0 || videoActive > 0;
}

function LaneRow({
  label,
  active,
  max,
  files,
}: {
  label: string;
  active: number;
  max: number;
  files: string[];
}) {
  const cap = max > 0 ? String(max) : "—";
  return (
    <p className="mt-0.5 truncate text-xs opacity-90">
      <span className="font-medium opacity-100">
        {label} {active}/{cap}
      </span>
      {" · "}
      <span title={files.join(" · ") || undefined}>{formatLaneFiles(files)}</span>
    </p>
  );
}

/** Unique live signals only — no Indexed/Pending totals (StatCards own those). */
export function LiveIndexingLanes({
  status,
  className,
  showHeader = true,
  lanesOnly = false,
}: {
  status: IndexStatus;
  className?: string;
  showHeader?: boolean;
  /** Image + video lanes only (no Go / in-flight chrome). */
  lanesOnly?: boolean;
}) {
  const processing = status.counts_by_status.processing ?? 0;
  const imageActive = status.image_slots?.active ?? status.active_image_jobs ?? 0;
  const imageMax = status.image_slots?.max ?? 0;
  const videoActive = status.video_slots?.active ?? status.active_video_jobs ?? 0;
  const videoMax = status.video_slots?.max ?? 0;
  const imageFiles = status.current_image_files ?? [];
  const videoFiles = status.current_video_files ?? [];

  return (
    <div className={cn("text-xs", className)}>
      {showHeader && !lanesOnly && (
        <div className="flex flex-wrap items-center gap-2 font-medium">
          {status.is_running ? (
            <LoadingLabel size={12}>Indexing…</LoadingLabel>
          ) : (
            <span>Live slots</span>
          )}
          {status.go_indexer_enabled && (
            <span className={status.go_indexer_alive ? "text-emerald-600 dark:text-emerald-400" : "opacity-60"}>
              · Go {status.go_indexer_alive ? "active" : "idle"}
              {status.go_files_per_sec != null && status.go_files_per_sec > 0
                ? ` ${status.go_files_per_sec.toFixed(2)}/s`
                : ""}
            </span>
          )}
        </div>
      )}
      <div className={showHeader && !lanesOnly ? "mt-1" : undefined}>
        <LaneRow label="Images" active={imageActive} max={imageMax} files={imageFiles} />
        <LaneRow label="Videos" active={videoActive} max={videoMax} files={videoFiles} />
      </div>
      {!lanesOnly && processing > 0 && (
        <p className="mt-1 tabular-nums text-muted-foreground">{processing} in flight</p>
      )}
    </div>
  );
}
