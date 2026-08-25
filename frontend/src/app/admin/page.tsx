"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  apiClient,
  type CacheCleanupResult,
  type ControlReaderStatus,
  type IndexStatus,
  type IndexTatStats,
  type SkipStats,
} from "@/lib/api";
import { useAuthSession } from "@/components/auth-gate";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { formatCount, skipReasonMeta } from "@/lib/index-errors";
import { Button, Card, ConfirmDialog, LoadingLabel, StatCard } from "@/components/ui";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatTatMs(ms: number): string {
  if (!ms || ms <= 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)}s`;
  const min = sec / 60;
  if (min < 60) return `${min < 10 ? min.toFixed(1) : Math.round(min)}m`;
  const hr = min / 60;
  return `${hr < 10 ? hr.toFixed(1) : Math.round(hr)}h`;
}

export default function AdminPage() {
  const router = useRouter();
  const { isAdmin: allowed } = useAuthSession();
  const { status, refresh } = useIndexStatusStore();
  const [skipStats, setSkipStats] = useState<SkipStats | null>(null);
  const [tatStats, setTatStats] = useState<IndexTatStats | null>(null);
  const [busy, setBusy] = useState(false);
  const [skipBusy, setSkipBusy] = useState(false);
  const [retryingReason, setRetryingReason] = useState<string | null>(null);
  const [controlStatus, setControlStatus] = useState<ControlReaderStatus | null>(null);
  const [controlBusy, setControlBusy] = useState<"pause" | "resume" | "reader" | null>(null);
  const [confirmControl, setConfirmControl] = useState<"pause" | "resume" | null>(null);
  const [confirmRetry, setConfirmRetry] = useState<{
    reason: string;
    count: number;
    label: string;
  } | null>(null);
  const [cachePreview, setCachePreview] = useState<CacheCleanupResult | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [confirmCacheClean, setConfirmCacheClean] = useState(false);
  const [cacheResult, setCacheResult] = useState<CacheCleanupResult | null>(null);
  const [tatResetBusy, setTatResetBusy] = useState(false);
  const [confirmTatReset, setConfirmTatReset] = useState(false);

  useEffect(() => {
    if (!allowed) router.replace("/");
  }, [allowed, router]);

  const loadSecondary = useCallback(async () => {
    const [skips, control, tat] = await Promise.all([
      apiClient.skipStats().catch(() => null),
      apiClient.controlReaderStatus().catch(() => null),
      apiClient.indexTatStats().catch(() => null),
    ]);
    setSkipStats(skips);
    setControlStatus(control);
    setTatStats(tat);
  }, []);

  useEffect(() => {
    void loadSecondary();
    const t = setInterval(() => void loadSecondary(), 30000);
    return () => clearInterval(t);
  }, [loadSecondary]);

  async function runIndex(reindex: boolean) {
    setBusy(true);
    try {
      await (reindex ? apiClient.triggerReindex() : apiClient.triggerIndex());
      await refresh();
      await loadSecondary();
    } catch {
      /* toasted */
    } finally {
      setBusy(false);
    }
  }

  async function skipCorrupt() {
    setSkipBusy(true);
    try {
      await apiClient.skipCorruptFiles();
      await refresh();
      await loadSecondary();
    } catch {
      /* toasted */
    } finally {
      setSkipBusy(false);
    }
  }

  async function requestRetryAll(reason: string) {
    setRetryingReason(reason);
    try {
      await apiClient.retrySkippedByReason(reason);
      await refresh();
      await loadSecondary();
    } catch {
      /* toasted */
    } finally {
      setRetryingReason(null);
      setConfirmRetry(null);
    }
  }

  async function runControl(action: "pause" | "resume") {
    setControlBusy(action);
    try {
      await (action === "pause"
        ? apiClient.pauseAllIndexing()
        : apiClient.resumeAllIndexing());
      for (let attempt = 0; attempt < 12; attempt += 1) {
        const next = await apiClient.controlReaderStatus();
        setControlStatus(next);
        if (
          action === "resume" ||
          (next.paused &&
            (next.active_image_jobs ?? 0) === 0 &&
            (next.active_video_jobs ?? 0) === 0)
        ) {
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      await refresh();
    } catch {
      /* toasted */
    } finally {
      setControlBusy(null);
      setConfirmControl(null);
    }
  }

  async function resetTatStats() {
    setTatResetBusy(true);
    try {
      const result = await apiClient.resetIndexTatStats();
      setTatStats(result.stats);
      await loadSecondary();
    } catch {
      /* toasted */
    } finally {
      setTatResetBusy(false);
      setConfirmTatReset(false);
    }
  }

  async function restartReader() {
    setControlBusy("reader");
    try {
      await apiClient.restartLibraryReader();
      setControlStatus(await apiClient.controlReaderStatus());
    } catch {
      /* toasted */
    } finally {
      setControlBusy(null);
    }
  }

  async function previewCacheCleanup() {
    setCacheBusy(true);
    setCacheResult(null);
    try {
      setCachePreview(await apiClient.cacheCleanupDryRun());
    } catch {
      setCachePreview(null);
    } finally {
      setCacheBusy(false);
    }
  }

  async function applyCacheCleanup() {
    setCacheBusy(true);
    try {
      const res = await apiClient.cacheCleanupApply();
      setCacheResult(res);
      setCachePreview(res);
    } catch {
      /* toasted */
    } finally {
      setCacheBusy(false);
      setConfirmCacheClean(false);
    }
  }

  const topSkipReasons = useMemo(
    () => (skipStats?.by_reason ?? []).slice(0, 12),
    [skipStats]
  );
  const maxSkipCount = Math.max(1, ...topSkipReasons.map((r) => r.count));

  if (!allowed) {
    return (
      <p className="text-sm text-muted-foreground">
        <LoadingLabel>Redirecting…</LoadingLabel>
      </p>
    );
  }

  const s: IndexStatus | null = status;
  const pending = s?.counts_by_status?.pending ?? 0;
  const processed = s?.counts_by_status?.processed ?? 0;
  const errors = s?.counts_by_status?.error ?? 0;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Admin — Indexing</h2>
        <p className="text-sm text-muted-foreground">
          Start index, backfill, and skip-reason retries. Already-indexed content (by hash) is skipped.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <StatCard label="Indexed" value={processed} />
        <StatCard label="Pending" value={pending} />
        <StatCard label="Errors" value={errors} />
      </div>

      <Card className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="font-medium">Index TAT</h3>
            <p className="text-sm text-muted-foreground">
              Claim → done (PROCESSED/captioned/error)
              {tatStats?.sample_window ? ` · ${tatStats.sample_window}` : ""}.
              {tatStats && (tatStats.image?.count ?? 0) + (tatStats.video?.count ?? 0) === 0
                ? " Waiting for completed samples…"
                : ""}
            </p>
          </div>
          <Button
            variant="secondary"
            disabled={tatResetBusy}
            onClick={() => setConfirmTatReset(true)}
          >
            {tatResetBusy ? <LoadingLabel>Resetting…</LoadingLabel> : "Reset TAT"}
          </Button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 text-sm">
          {(["image", "video"] as const).map((kind) => {
            const bucket = tatStats?.[kind];
            const count = bucket?.count ?? 0;
            return (
              <div key={kind} className="rounded-md border border-border/60 px-3 py-2">
                <p className="font-medium capitalize">{kind}</p>
                {count === 0 ? (
                  <p className="mt-1 text-muted-foreground">No samples yet</p>
                ) : (
                  <p className="mt-1 text-muted-foreground">
                    <span className="text-foreground">{formatTatMs(bucket!.min_ms)}</span>
                    {" · "}
                    <span className="font-medium text-foreground">{formatTatMs(bucket!.avg_ms)}</span>
                    {" · "}
                    <span className="text-foreground">{formatTatMs(bucket!.max_ms)}</span>
                    <span className="ml-2">min · avg · max</span>
                    <span className="ml-2">({formatCount(count)})</span>
                  </p>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card className="flex flex-wrap gap-3">
        <Button onClick={() => void runIndex(false)} disabled={busy || !!s?.is_running}>
          {s?.is_running || busy ? <LoadingLabel>Indexing…</LoadingLabel> : "Start Index"}
        </Button>
        <Button variant="secondary" onClick={() => void runIndex(true)} disabled={busy || !!s?.is_running}>
          Backfill missing
        </Button>
        <Button variant="secondary" onClick={() => void skipCorrupt()} disabled={skipBusy}>
          {skipBusy ? <LoadingLabel>Skipping…</LoadingLabel> : "Skip corrupt files"}
        </Button>
        <Button variant="secondary" onClick={() => void refresh()}>
          Refresh status
        </Button>
      </Card>

      <Card className="space-y-3">
        <div>
          <h3 className="font-medium">Isolated controls</h3>
          <p className="text-sm text-muted-foreground">
            Constant-time controls and Library reads use a dedicated thread and never delete or
            rewrite indexed files.
          </p>
        </div>
        <div className="grid gap-2 text-sm sm:grid-cols-3">
          <p>
            Indexing:{" "}
            <span className="font-medium">{controlStatus?.paused ? "Paused" : "Available"}</span>
          </p>
          <p>
            Control watcher:{" "}
            <span className="font-medium">
              {controlStatus?.watcher_alive ? "Online" : "Waiting"}
            </span>
          </p>
          <p>
            Library reader:{" "}
            <span className="font-medium">
              {controlStatus?.reader.thread_alive ? "Online" : "Unavailable"}
            </span>
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button
            variant="secondary"
            disabled={!!controlBusy || !!controlStatus?.paused}
            onClick={() => setConfirmControl("pause")}
          >
            {controlBusy === "pause" ? <LoadingLabel>Pausing…</LoadingLabel> : "Pause all indexing"}
          </Button>
          <Button
            variant="secondary"
            disabled={!!controlBusy || !controlStatus?.paused}
            onClick={() => setConfirmControl("resume")}
          >
            {controlBusy === "resume" ? <LoadingLabel>Resuming…</LoadingLabel> : "Resume indexing"}
          </Button>
          <Button
            variant="secondary"
            disabled={!!controlBusy}
            onClick={() => void restartReader()}
          >
            {controlBusy === "reader" ? (
              <LoadingLabel>Restarting…</LoadingLabel>
            ) : (
              "Restart Library reader"
            )}
          </Button>
        </div>
      </Card>

      <Card className="space-y-3">
        <div>
          <h3 className="font-medium">Clean Drive media cache</h3>
          <p className="text-sm text-muted-foreground">
            Deletes temporary Drive downloads under media_cache/videos only when the file is
            PROCESSED and already has a durable Media row. Never deletes Postgres Media, Qdrant,
            thumbnails, YouTube, or upload sources. Index/auto-index will not re-pull those files.
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button variant="secondary" disabled={cacheBusy} onClick={() => void previewCacheCleanup()}>
            {cacheBusy && !confirmCacheClean ? (
              <LoadingLabel>Scanning…</LoadingLabel>
            ) : (
              "Dry-run scan"
            )}
          </Button>
          <Button
            variant="secondary"
            disabled={cacheBusy || !cachePreview || cachePreview.deletable_count === 0}
            onClick={() => setConfirmCacheClean(true)}
          >
            Clean safe cache…
          </Button>
        </div>
        {cachePreview && (
          <div className="rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm">
            <p>
              Safe to delete:{" "}
              <span className="font-medium tabular-nums">
                {formatCount(cachePreview.deletable_count)}
              </span>{" "}
              files ·{" "}
              <span className="font-medium tabular-nums">
                {formatBytes(cachePreview.deletable_bytes)}
              </span>
            </p>
            <p className="text-xs text-muted-foreground">
              Scanned {formatCount(cachePreview.total_files)} cache files (
              {formatBytes(cachePreview.total_bytes)}) across media_cache + videos.
            </p>
            {cacheResult && !cacheResult.dry_run && (
              <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-400">
                Deleted {formatCount(cacheResult.deleted_count)} files ·{" "}
                {formatBytes(cacheResult.deleted_bytes)}
              </p>
            )}
          </div>
        )}
      </Card>

      <Card>
        <h3 className="mb-3 font-medium">Skip reasons</h3>
        {topSkipReasons.length === 0 ? (
          <p className="text-sm text-muted-foreground">No skipped media yet.</p>
        ) : (
          <ul className="space-y-3">
            {topSkipReasons.map((row) => {
              const meta = skipReasonMeta(row.reason);
              return (
                <li key={row.reason} className="space-y-1">
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span>
                      {meta.label}{" "}
                      <span className="text-muted-foreground">({formatCount(row.count)})</span>
                    </span>
                    {meta.retryable && (
                      <Button
                        variant="secondary"
                        disabled={retryingReason === row.reason}
                        onClick={() =>
                          setConfirmRetry({
                            reason: row.reason,
                            count: row.count,
                            label: meta.label,
                          })
                        }
                      >
                        {retryingReason === row.reason ? (
                          <LoadingLabel>Retrying…</LoadingLabel>
                        ) : (
                          meta.retryLabel
                        )}
                      </Button>
                    )}
                  </div>
                  <div className="h-1.5 overflow-hidden rounded bg-muted">
                    <div
                      className="h-full rounded bg-primary/70"
                      style={{ width: `${Math.max(4, (100 * row.count) / maxSkipCount)}%` }}
                    />
                  </div>
                  <p className="text-xs text-muted-foreground">{meta.hint}</p>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <ConfirmDialog
        open={confirmTatReset}
        title="Reset Index TAT?"
        message="Clears min / avg / max sample stamps only. Indexed files, captions, embeddings, and media are not changed. New completions refill the card."
        confirmLabel="Reset TAT"
        onConfirm={() => void resetTatStats()}
        onCancel={() => setConfirmTatReset(false)}
      />

      <ConfirmDialog
        open={!!confirmControl}
        title={confirmControl === "pause" ? "Pause all indexing?" : "Resume indexing?"}
        message={
          confirmControl === "pause"
            ? "Stops new claims and cancels in-flight jobs. Indexed files, captions, embeddings, and media are not changed or deleted."
            : "Removes only the global pause flag and re-enables automatic indexing."
        }
        confirmLabel={confirmControl === "pause" ? "Pause indexing" : "Resume indexing"}
        onConfirm={() => confirmControl && void runControl(confirmControl)}
        onCancel={() => setConfirmControl(null)}
      />

      <ConfirmDialog
        open={confirmCacheClean}
        title="Clean Drive media cache?"
        message={
          cachePreview
            ? `Delete ${formatCount(cachePreview.deletable_count)} temporary cache file(s) (${formatBytes(cachePreview.deletable_bytes)}). Only PROCESSED Drive files that already have Media. Indexed library data is not changed.`
            : ""
        }
        confirmLabel="Delete safe cache"
        onConfirm={() => void applyCacheCleanup()}
        onCancel={() => setConfirmCacheClean(false)}
      />

      <ConfirmDialog
        open={!!confirmRetry}
        title={confirmRetry ? `Retry ${confirmRetry.label}?` : ""}
        message={
          confirmRetry
            ? `Re-queue ${formatCount(confirmRetry.count)} skipped file(s) with reason “${confirmRetry.reason}”.`
            : ""
        }
        confirmLabel="Retry"
        onConfirm={() => confirmRetry && void requestRetryAll(confirmRetry.reason)}
        onCancel={() => setConfirmRetry(null)}
      />
    </div>
  );
}
