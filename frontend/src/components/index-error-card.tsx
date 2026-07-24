"use client";

import { useState } from "react";
import { Button } from "@/components/ui";
import { humanizeIndexError } from "@/lib/index-errors";

type Props = {
  name: string;
  path: string;
  errorMessage: string | null;
  busy?: boolean;
  onRetry: () => void;
  onDismiss: () => void;
};

export function IndexErrorCard({
  name,
  path,
  errorMessage,
  busy,
  onRetry,
  onDismiss,
}: Props) {
  const [open, setOpen] = useState(false);
  const friendly = humanizeIndexError(errorMessage);
  const hasDetails = Boolean(friendly.details);

  return (
    <div className="rounded-lg border border-border/60 bg-background/50 p-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground" title={name}>
            {name}
          </p>
          {path ? (
            <p className="truncate text-xs text-muted-foreground" title={path}>
              {path}
            </p>
          ) : null}
          <p className="mt-1.5 text-sm leading-snug text-red-700 dark:text-red-300">
            {friendly.summary}
          </p>
          {hasDetails ? (
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            >
              {open ? "Hide details" : "Details"}
            </button>
          ) : null}
          {open && friendly.details ? (
            <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-border/50 bg-muted/40 p-2 text-[11px] leading-relaxed text-muted-foreground whitespace-pre-wrap break-words">
              {friendly.details}
            </pre>
          ) : null}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="secondary" onClick={onRetry} disabled={busy}>
            Retry
          </Button>
          <Button variant="secondary" onClick={onDismiss} disabled={busy}>
            Dismiss
          </Button>
        </div>
      </div>
    </div>
  );
}
