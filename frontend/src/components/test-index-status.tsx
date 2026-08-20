"use client";

import { useEffect, useState } from "react";
import { apiClient } from "@/lib/api";
import { formatCount } from "@/lib/index-errors";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { LoadingLabel } from "@/components/ui";

export function TestIndexStatus({ compact = false }: { compact?: boolean }) {
  const { status, error } = useIndexStatusStore();
  const [namedCount, setNamedCount] = useState<number | null>(null);

  useEffect(() => {
    void apiClient
      .personsRevision()
      .then((meta) => setNamedCount(meta.count))
      .catch(() => setNamedCount(null));
  }, [status?.revision]);

  if (error) {
    return (
      <div className="rounded-2xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
        Backend unreachable — indexing status unavailable.
      </div>
    );
  }

  if (!status) {
    return (
      <div className="rounded-2xl border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
        <LoadingLabel>Loading indexing status…</LoadingLabel>
      </div>
    );
  }

  const processed = status.counts_by_status.processed ?? 0;
  const pending = status.counts_by_status.pending ?? 0;
  const processing = status.counts_by_status.processing ?? 0;
  const total = processed + pending + processing;
  const processedPct = total === 0 ? 0 : Math.min(100, Math.round((processed / total) * 100));

  return (
    <div className="rounded-2xl border border-border bg-card p-4 shadow-sm">
      <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
        AI Indexing Status
      </p>
      <div className="mt-3 space-y-3">
        <div>
          <div className="flex items-center justify-between text-sm">
            <span>Indexed files</span>
            <span className="font-semibold tabular-nums">{formatCount(processed)}</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-blue-600" style={{ width: `${processedPct}%` }} />
          </div>
        </div>
        <div>
          <div className="flex items-center justify-between text-sm">
            <span>Named identities</span>
            <span className="font-semibold tabular-nums text-emerald-700 dark:text-emerald-400">
              {namedCount == null ? "—" : formatCount(namedCount)}
            </span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-emerald-500"
              style={{ width: namedCount ? `${Math.min(100, namedCount)}%` : "0%" }}
            />
          </div>
        </div>
        {!compact && (
          <p className="text-xs text-muted-foreground">
            {status.is_running ? "Indexing active" : "Indexer idle"}
            {pending ? ` · ${formatCount(pending)} pending` : ""}
            {processing ? ` · ${formatCount(processing)} in flight` : ""}
          </p>
        )}
      </div>
    </div>
  );
}
