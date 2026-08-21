"use client";

import { formatCount } from "@/lib/index-errors";
import { useIndexStatusStore } from "@/lib/index-status-store";
import { LoadingLabel } from "@/components/ui";

/** Slim idle/pending strip only — Indexed files / Named identities removed. */
export function TestIndexStatus({ compact = false }: { compact?: boolean }) {
  const { status, error } = useIndexStatusStore();

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

  const pending = status.counts_by_status.pending ?? 0;
  const processing = status.counts_by_status.processing ?? 0;

  if (compact) {
    return (
      <p className="text-xs text-muted-foreground">
        {status.is_running ? "Indexing active" : "Indexer idle"}
        {pending ? ` · ${formatCount(pending)} pending` : ""}
        {processing ? ` · ${formatCount(processing)} in flight` : ""}
      </p>
    );
  }

  return (
    <div className="rounded-2xl border border-border bg-card px-4 py-3 shadow-sm">
      <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
        AI Indexing Status
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        {status.is_running ? "Indexing active" : "Indexer idle"}
        {pending ? ` · ${formatCount(pending)} pending` : ""}
        {processing ? ` · ${formatCount(processing)} in flight` : ""}
      </p>
    </div>
  );
}
