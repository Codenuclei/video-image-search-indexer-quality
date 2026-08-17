"use client";

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
  const friendly = humanizeIndexError(errorMessage);

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
