"use client";

import { useState } from "react";
import { API_BASE } from "@/lib/api";
import { useIndexStatusStore } from "@/lib/index-status-store";
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

/** App-health only — not indexing telemetry. Mounted on all routes including Dashboard. */
export function BackendConnectivityIndicator() {
  const { error, refresh } = useIndexStatusStore();
  const [retrying, setRetrying] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  if (!error) return null;

  async function handleRetry() {
    setRetrying(true);
    try {
      await refresh();
    } finally {
      setRetrying(false);
    }
  }

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
