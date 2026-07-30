"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, apiClient, type IndexStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { BackendDisconnectedOverlay } from "@/components/backend-disconnected-overlay";
import { LoadingLabel } from "@/components/spinner";

function apiReachabilityHint(): string {
  try {
    const u = new URL(API_BASE);
    const port = u.port || (u.protocol === "https:" ? "443" : "80");
    return `Backend unreachable — is ${u.hostname}:${port} running?`;
  } catch {
    return `Backend unreachable — is ${API_BASE} running?`;
  }
}

function formatLaneFiles(files: string[], limit = 2): string {
  if (!files.length) return "idle";
  const shown = files.slice(0, limit).join(" · ");
  const extra = files.length > limit ? ` (+${files.length - limit})` : "";
  return `${shown}${extra}`;
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
    <p className="mt-0.5 truncate opacity-80">
      <span className="font-medium opacity-100">
        {label} {active}/{cap}
      </span>
      {" · "}
      {formatLaneFiles(files)}
    </p>
  );
}

export function IndexStatusBanner() {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const failStreakRef = useRef(0);

  const poll = useCallback(async () => {
    try {
      setStatus(await apiClient.indexStatus());
      failStreakRef.current = 0;
      setError(false);
      setDismissed(false);
      return true;
    } catch {
      // Require consecutive failures so long carousel AI calls don't flash
      // "Backend unreachable" on a single blip / contention.
      failStreakRef.current += 1;
      if (failStreakRef.current >= 3) {
        setError(true);
      }
      return false;
    }
  }, []);

  useEffect(() => {
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, [poll]);

  // Browser offline also surfaces the same retry banner.
  useEffect(() => {
    function onOffline() {
      failStreakRef.current = 3;
      setError(true);
    }
    function onOnline() {
      failStreakRef.current = 0;
      void poll();
    }
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return () => {
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, [poll]);

  async function handleRetry() {
    setRetrying(true);
    try {
      await poll();
    } finally {
      setRetrying(false);
    }
  }

  if (error) {
    return (
      <>
        <div className="mb-4 rounded-md border border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <LoadingLabel size={12}>{apiReachabilityHint()}</LoadingLabel>
        </div>
        {!dismissed && (
          <BackendDisconnectedOverlay
            onRetry={handleRetry}
            onDismiss={() => setDismissed(true)}
            retrying={retrying}
          />
        )}
      </>
    );
  }

  if (!status) return null;

  const pending = status.counts_by_status.pending ?? 0;
  const processing = status.counts_by_status.processing ?? 0;
  const processed = status.counts_by_status.processed ?? 0;
  const imageActive = status.image_slots?.active ?? status.active_image_jobs ?? 0;
  const imageMax = status.image_slots?.max ?? 0;
  const videoActive = status.video_slots?.active ?? status.active_video_jobs ?? 0;
  const videoMax = status.video_slots?.max ?? 0;
  const imageFiles = status.current_image_files ?? [];
  const videoFiles = status.current_video_files ?? [];
  const showLanes = status.is_running || imageActive > 0 || videoActive > 0;

  return (
    <div
      className={cn(
        "mb-4 rounded-md border px-3 py-2 text-xs",
        status.is_running
          ? "border-primary bg-primary/10 text-primary"
          : "border-border bg-muted/40 text-muted-foreground"
      )}
    >
      <div className="flex items-center gap-2 font-medium">
        {status.is_running ? (
          <LoadingLabel size={12}>Indexing…</LoadingLabel>
        ) : (
          <span>Indexer idle</span>
        )}
        {status.auto_index_enabled && !status.is_running && (
          <span className="opacity-60">· auto on</span>
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
      {showLanes && (
        <div className="mt-1">
          <LaneRow label="Images" active={imageActive} max={imageMax} files={imageFiles} />
          <LaneRow label="Videos" active={videoActive} max={videoMax} files={videoFiles} />        </div>
      )}
      <p className="mt-1">
        {processed} done · {pending} pending · {processing} in flight
      </p>
      {status.last_run && !status.is_running && (
        <p className="mt-1 opacity-60">
          Last run: {status.last_run.processed} processed, {status.last_run.skipped} skipped
        </p>
      )}
    </div>
  );
}
