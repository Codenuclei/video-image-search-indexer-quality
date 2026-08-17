"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  apiClient,
  type FileIndexConflict,
  type IndexStatus,
  type IndexedFolder,
  type SkipStats,
} from "@/lib/api";
import { formatCount, skipReasonMeta } from "@/lib/index-errors";
import { Button, Card, LoadingLabel, StatCard } from "@/components/ui";

const STATUS_ORDER = ["processed", "pending", "processing", "error", "skipped"] as const;

const STATUS_COLORS: Record<string, string> = {
  processed: "#22c55e",
  pending: "#eab308",
  processing: "#3b82f6",
  error: "#ef4444",
  skipped: "#71717a",
};

const STATUS_LABELS: Record<string, string> = {
  processed: "Processed",
  pending: "Pending",
  processing: "Processing",
  error: "Error",
  skipped: "Skipped",
};

function conflictKindLabel(kind: string): string {
  switch (kind) {
    case "same_content":
      return "Identical content";
    case "same_content_diff_name":
      return "Same content, different name";
    case "same_name_diff_content":
      return "Same name, different content";
    default:
      return kind.replace(/_/g, " ");
  }
}

export default function DashboardPage() {
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [skipStats, setSkipStats] = useState<SkipStats | null>(null);
  const [folders, setFolders] = useState<IndexedFolder[]>([]);
  const [conflicts, setConflicts] = useState<FileIndexConflict[]>([]);
  const [retryingReason, setRetryingReason] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      // Hot path: status only. Folders/conflicts are slower-changing — avoid
      // spamming Postgres on every poll tick when a few tabs are open.
      const status = await apiClient.indexStatus();
      setIndexStatus(status);
    } catch {
      /* silent poll; IndexStatusBanner surfaces unreachable */
    }
  }, []);

  const loadSecondary = useCallback(async () => {
    try {
      const [skips, folderRes, conflictRes] = await Promise.all([
        apiClient.skipStats().catch(() => null),
        apiClient.indexedFolders().catch(() => ({ folders: [], total: 0 })),
        apiClient.indexConflicts("pending").catch(() => ({ items: [], total: 0, offset: 0, limit: 50 })),
      ]);
      setSkipStats(skips);
      setFolders(folderRes.folders ?? []);
      setConflicts(conflictRes.items ?? []);
    } catch {
      /* secondary — ignore */
    }
  }, []);

  useEffect(() => {
    void load();
    void loadSecondary();
    const hot = setInterval(() => void load(), 10000);
    const cold = setInterval(() => void loadSecondary(), 30000);
    return () => {
      clearInterval(hot);
      clearInterval(cold);
    };
  }, [load, loadSecondary]);

  async function requestRetryAll(reason: string) {
    setRetryingReason(reason);
    try {
      await apiClient.retrySkippedByReason(reason);
      await load();
      await loadSecondary();
    } catch {
      /* api() already toasted */
    } finally {
      setRetryingReason(null);
    }
  }

  async function resolveConflict(id: number, action: "skip" | "replace" | "merge") {
    setResolvingId(id);
    try {
      await apiClient.resolveIndexConflict(id, action);
      await load();
      await loadSecondary();
    } catch {
      /* api() already toasted */
    } finally {
      setResolvingId(null);
    }
  }

  const chartData = useMemo(() => {
    const counts = indexStatus?.counts_by_status ?? {};
    const rows = Object.entries(counts).map(([status, count]) => ({
      status,
      label: STATUS_LABELS[status] ?? status,
      count,
      fill: STATUS_COLORS[status] ?? "#a855f7",
    }));
    return rows.sort(
      (a, b) =>
        (STATUS_ORDER.indexOf(a.status as (typeof STATUS_ORDER)[number]) + 1 || 99) -
        (STATUS_ORDER.indexOf(b.status as (typeof STATUS_ORDER)[number]) + 1 || 99)
    );
  }, [indexStatus]);

  const topSkipReasons = useMemo(
    () => (skipStats?.by_reason ?? []).slice(0, 8),
    [skipStats]
  );
  const maxSkipCount = Math.max(1, ...topSkipReasons.map((r) => r.count));

  const processed = indexStatus?.counts_by_status?.processed ?? 0;
  const pending = indexStatus?.counts_by_status?.pending ?? 0;
  const errors = indexStatus?.counts_by_status?.error ?? 0;
  const skipped = indexStatus?.counts_by_status?.skipped ?? skipStats?.total_skipped ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold">Dashboard</h2>
        <p className="text-sm text-muted-foreground">
          Gemini Embedding 2 video search · Gemini image search · InsightFace detection
        </p>
      </div>

      {!indexStatus && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading dashboard…</LoadingLabel>
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard label="Indexed" value={processed} />
        <StatCard label="Pending" value={pending} />
        <StatCard label="Errors" value={errors} />
        <StatCard label="Skipped" value={skipped} />
        <StatCard
          label="Indexer"
          value={
            !indexStatus ? (
              "…"
            ) : indexStatus.is_running ? (
              <LoadingLabel size={18}>Running</LoadingLabel>
            ) : (
              "Idle"
            )
          }
          hint={
            indexStatus?.last_run
              ? `Last: ${indexStatus.last_run.processed} processed, ${indexStatus.last_run.errored} errors`
              : undefined
          }
        />
      </div>

      <Card>
        <h3 className="mb-3 font-medium">Drive file status</h3>
        {!indexStatus ? (
          <p className="text-sm text-muted-foreground">
            <LoadingLabel size={14}>Loading chart…</LoadingLabel>
          </p>
        ) : chartData.length === 0 ? (
          <p className="text-sm text-muted-foreground">No files synced yet. Go to Folders to start indexing.</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
              {chartData.map((row) => (
                <span key={row.status} className="inline-flex items-center gap-1.5">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ backgroundColor: row.fill }}
                  />
                  {row.label}
                </span>
              ))}
            </div>
            <div className="h-44 max-w-md">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartData}
                  margin={{ top: 8, right: 4, left: -8, bottom: 0 }}
                  barCategoryGap="12%"
                >
                  <XAxis
                    dataKey="label"
                    stroke="var(--muted-foreground)"
                    fontSize={11}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    stroke="var(--muted-foreground)"
                    fontSize={11}
                    allowDecimals={false}
                    tickLine={false}
                    axisLine={false}
                    width={32}
                  />
                  <Tooltip
                    cursor={{ fill: "transparent" }}
                    contentStyle={{
                      background: "#0f172a",
                      border: "1px solid #475569",
                      borderRadius: 8,
                      color: "#f8fafc",
                      fontSize: 12,
                      boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
                      padding: "8px 12px",
                    }}
                    itemStyle={{ color: "#f8fafc", fontWeight: 500 }}
                    labelStyle={{ color: "#f8fafc", fontWeight: 700, marginBottom: 4 }}
                    formatter={(value: number) => [String(value), "Files"]}
                    labelFormatter={(label) => String(label)}
                  />
                  <Bar dataKey="count" radius={[4, 4, 0, 0]} barSize={40} maxBarSize={48}>
                    {chartData.map((row) => (
                      <Cell key={row.status} fill={row.fill} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </Card>

      <Card>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h3 className="font-medium">Skip reasons</h3>
            <p className="text-xs text-muted-foreground">
              Why files were skipped
              {skipStats?.total_skipped != null
                ? ` · ${formatCount(skipStats.total_skipped)} total`
                : ""}
            </p>
          </div>
        </div>
        {topSkipReasons.length === 0 ? (
          <p className="text-sm text-muted-foreground">No skipped media yet.</p>
        ) : (
          <ul className="divide-y divide-border/50 overflow-hidden rounded-lg border border-border/60 bg-muted/10">
            {topSkipReasons.map((r) => {
              const meta = skipReasonMeta(r.reason);
              const pct = Math.max(6, Math.round((r.count / maxSkipCount) * 100));
              const rowBusy = retryingReason === r.reason;
              return (
                <li key={r.reason} className="relative px-3 py-2.5 transition-colors hover:bg-muted/25">
                  <div
                    className="pointer-events-none absolute inset-y-0 left-0 bg-muted-foreground/10"
                    style={{ width: `${pct}%` }}
                    aria-hidden
                  />
                  <div className="relative flex flex-wrap items-center gap-2 sm:gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <p className="truncate text-sm font-medium text-foreground">{meta.label}</p>
                        <span className="shrink-0 rounded-md bg-background/80 px-1.5 py-0.5 text-xs font-semibold tabular-nums text-foreground ring-1 ring-border/60">
                          {formatCount(r.count)}
                        </span>
                      </div>
                      <p className="mt-0.5 truncate text-xs text-muted-foreground">{meta.hint}</p>
                    </div>
                    {meta.retryable ? (
                      <Button
                        variant="secondary"
                        className="shrink-0 px-2.5 py-1.5 text-xs"
                        disabled={retryingReason != null}
                        onClick={() => void requestRetryAll(r.reason)}
                      >
                        {rowBusy ? <LoadingLabel>{meta.retryLabel}…</LoadingLabel> : meta.retryLabel}
                      </Button>
                    ) : (
                      <span className="shrink-0 rounded-md px-2 py-1 text-[11px] text-muted-foreground">
                        {meta.retryLabel}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card>
        <div className="mb-3">
          <h3 className="font-medium">Indexed folders</h3>
          <p className="text-xs text-muted-foreground">
            Historical Drive folders with persistent links (kept after disconnect)
          </p>
        </div>
        {folders.length === 0 ? (
          <p className="text-sm text-muted-foreground">No folders indexed yet. Choose one on Folders.</p>
        ) : (
          <ul className="divide-y divide-border/50 overflow-hidden rounded-lg border border-border/60">
            {folders.map((f) => (
              <li
                key={f.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 hover:bg-muted/20"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-sm font-medium">{f.name}</p>
                    {f.is_active && (
                      <span className="rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
                        Active
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {f.last_file_count != null ? `${formatCount(f.last_file_count)} files · ` : ""}
                    {f.drive_user_email ? `${f.drive_user_email} · ` : ""}
                    {f.last_indexed_at
                      ? `last synced ${new Date(f.last_indexed_at).toLocaleString()}`
                      : null}
                  </p>
                </div>
                <a
                  href={f.drive_url}
                  target="_blank"
                  rel="noreferrer"
                  className="shrink-0 text-xs font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
                >
                  Open in Drive
                </a>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <div className="mb-3">
          <h3 className="font-medium">File conflicts</h3>
          <p className="text-xs text-muted-foreground">
            Same content autoskips; same name with different content needs Replace or Skip. Same
            content with different names can Merge.
          </p>
        </div>
        {conflicts.length === 0 ? (
          <p className="text-sm text-muted-foreground">No open conflicts.</p>
        ) : (
          <ul className="divide-y divide-border/50 overflow-hidden rounded-lg border border-border/60">
            {conflicts.map((c) => {
              const busy = resolvingId === c.id;
              return (
                <li key={c.id} className="space-y-2 px-3 py-3">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <p className="text-sm font-medium">{conflictKindLabel(c.conflict_kind)}</p>
                    <span className="text-xs text-muted-foreground">{c.status}</span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Incoming <span className="font-medium text-foreground">{c.incoming_name}</span>
                    {" vs "}
                    existing <span className="font-medium text-foreground">{c.existing_name}</span>
                  </p>
                  {c.message && (
                    <p className="truncate text-[11px] text-muted-foreground">{c.message}</p>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {c.can_replace && (
                      <Button
                        variant="secondary"
                        className="px-2.5 py-1.5 text-xs"
                        disabled={resolvingId != null}
                        onClick={() => void resolveConflict(c.id, "replace")}
                      >
                        {busy ? <LoadingLabel>Replace…</LoadingLabel> : "Replace"}
                      </Button>
                    )}
                    {c.can_merge && (
                      <Button
                        variant="secondary"
                        className="px-2.5 py-1.5 text-xs"
                        disabled={resolvingId != null}
                        onClick={() => void resolveConflict(c.id, "merge")}
                      >
                        {busy ? <LoadingLabel>Merge…</LoadingLabel> : "Merge"}
                      </Button>
                    )}
                    {c.can_skip && (
                      <Button
                        variant="secondary"
                        className="px-2.5 py-1.5 text-xs"
                        disabled={resolvingId != null}
                        onClick={() => void resolveConflict(c.id, "skip")}
                      >
                        Skip
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}
