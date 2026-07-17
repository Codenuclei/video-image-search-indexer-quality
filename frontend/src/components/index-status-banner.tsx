"use client";

import { useCallback, useEffect, useState } from "react";
import { apiClient, type IndexStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { BackendDisconnectedOverlay } from "@/components/backend-disconnected-overlay";
import { LoadingLabel } from "@/components/spinner";

export function IndexStatusBanner() {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  const poll = useCallback(async () => {
    try {
      setStatus(await apiClient.indexStatus());
      setError(false);
      setDismissed(false);
      return true;
    } catch {
      setError(true);
      return false;
    }
  }, []);

  useEffect(() => {
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, [poll]);

  // Browser offline also surfaces the same retry banner.
  useEffect(() => {
    function onOffline() {
      setError(true);
    }
    function onOnline() {
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
          <LoadingLabel size={12}>Backend unreachable — is port 8002 running?</LoadingLabel>
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
