"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { apiClient, type IndexStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

export function IndexStatusBanner() {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    async function poll() {
      try {
        setStatus(await apiClient.indexStatus());
        setError(false);
      } catch {
        setError(true);
      }
    }
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, []);

  if (error) {
    return (
      <div className="mb-4 rounded-md border border-destructive bg-destructive/10 px-3 py-2 text-xs text-destructive">
        Backend unreachable — is port 8002 running?
      </div>
    );
  }

  if (!status) return null;

  const pending = status.counts_by_status.pending ?? 0;
  const processing = status.counts_by_status.processing ?? 0;
  const processed = status.counts_by_status.processed ?? 0;

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
        {status.is_running && <Loader2 size={12} className="animate-spin" />}
        <span>{status.is_running ? "Indexing…" : "Indexer idle"}</span>
        {status.auto_index_enabled && !status.is_running && (
          <span className="opacity-60">· auto on</span>
        )}
      </div>
      {status.is_running && status.current_file && (
        <p className="mt-1 truncate opacity-80">Processing: {status.current_file}</p>
      )}
      <p className="mt-1">
        {processed} done · {pending} pending
        {processing > 0 && ` · ${processing} active`}
      </p>
      {status.last_run && !status.is_running && (
        <p className="mt-1 opacity-60">
          Last run: {status.last_run.processed} processed, {status.last_run.skipped} skipped
        </p>
      )}
    </div>
  );
}
