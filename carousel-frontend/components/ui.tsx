"use client";

import { useState } from "react";
import { Download, RefreshCw, WifiOff, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatApiError, isServiceUnavailableMessage } from "@/lib/api";
import { downloadFromUrl } from "@/lib/download";
import { LoadingLabel, Spinner } from "@/components/spinner";

export { LoadingLabel, Spinner };

export function DownloadButton({
  url,
  filename,
  label = "Download",
  variant = "primary",
  className,
  iconSize = 14,
  onClick,
}: {
  url: string;
  filename: string;
  label?: string;
  variant?: "primary" | "secondary" | "ghost";
  className?: string;
  iconSize?: number;
  onClick?: (e: React.MouseEvent) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110",
    secondary: "border border-border bg-muted text-foreground hover:bg-accent",
    ghost: "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
  };

  return (
    <button
      type="button"
      disabled={busy || !url}
      title={error || `Download ${filename}`}
      aria-label={error || `Download ${filename}`}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-60",
        variants[variant],
        className
      )}
      onClick={(e) => {
        onClick?.(e);
        e.preventDefault();
        e.stopPropagation();
        if (busy) return;
        setBusy(true);
        setError(null);
        void downloadFromUrl(url, filename)
          .catch((err) => {
            setError(formatApiError(err, "Download failed"));
          })
          .finally(() => setBusy(false));
      }}
    >
      <Download size={iconSize} className="shrink-0" aria-hidden />
      <span>{busy ? "Downloading…" : error ? "Retry download" : label}</span>
    </button>
  );
}

export function ServiceErrorCard({
  message,
  onRetry,
  onDismiss,
  retryLabel = "Try again",
  retrying = false,
}: {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  retryLabel?: string;
  retrying?: boolean;
}) {
  const unavailable = isServiceUnavailableMessage(message);

  return (
    <div
      role="alert"
      className={cn(
        "rounded-xl border p-4 sm:p-5",
        unavailable
          ? "border-[#d6dde8] bg-[#f7f9fc]"
          : "border-destructive/40 bg-destructive/5"
      )}
    >
      <div className="flex items-start gap-3.5">
        {unavailable ? (
          <div
            className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#e8eef6] text-[#3d5a80]"
            aria-hidden
          >
            <WifiOff size={18} strokeWidth={1.75} />
          </div>
        ) : null}
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-foreground">
                {unavailable ? "Can't reach the studio API" : "Something went wrong"}
              </p>
              <p
                className={cn(
                  "mt-1 text-sm leading-relaxed",
                  unavailable ? "text-muted-foreground" : "text-destructive"
                )}
              >
                {unavailable
                  ? "The backend may still be starting, or the connection dropped. You can keep browsing this page — retry when you're ready to load videos."
                  : message}
              </p>
            </div>
            {onDismiss && (
              <button
                type="button"
                onClick={onDismiss}
                className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                aria-label="Dismiss"
              >
                <X size={14} />
              </button>
            )}
          </div>
          {onRetry && (
            <button
              type="button"
              className={cn(
                "mt-3.5 inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition-colors disabled:opacity-50",
                unavailable
                  ? "bg-[#191919] text-white hover:bg-[#191919]/90"
                  : "border border-border bg-muted text-foreground hover:bg-accent"
              )}
              onClick={(e) => {
                e.preventDefault();
                onRetry();
              }}
              disabled={retrying}
            >
              <RefreshCw
                size={14}
                className={cn("shrink-0", retrying && "animate-spin")}
                aria-hidden
              />
              {retrying ? <LoadingLabel>Retrying…</LoadingLabel> : retryLabel}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
