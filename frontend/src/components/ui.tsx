"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { driveFilePreviewUrl, isServiceUnavailableMessage } from "@/lib/api";
import { BackendDisconnectedOverlay } from "@/components/backend-disconnected-overlay";

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("rounded-xl border border-border bg-card p-4 text-card-foreground shadow-sm", className)}>
      {children}
    </div>
  );
}

export function StatCard({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-1 text-3xl font-semibold text-foreground">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </Card>
  );
}

export function Button({
  children,
  onClick,
  variant = "primary",
  disabled,
  className,
  type = "button",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  className?: string;
  type?: "button" | "submit";
}) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110 active:scale-95 shadow-sm",
    secondary: "border border-border bg-secondary text-secondary-foreground hover:bg-accent hover:border-amber-300",
    danger: "bg-destructive text-destructive-foreground hover:brightness-110 active:scale-95",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150 disabled:pointer-events-none disabled:opacity-50",
        variants[variant],
        className
      )}
    >
      {children}
    </button>
  );
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "primary" | "secondary" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 bg-black/50"
        onClick={onCancel}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="relative z-10 w-full max-w-sm rounded-xl border border-border bg-card p-5 text-card-foreground shadow-xl"
      >
        <h2 id="confirm-dialog-title" className="text-base font-semibold">
          {title}
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">{message}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant={variant} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ServiceErrorCard({
  message,
  onRetry,
  onDismiss,
  retryLabel = "Retry",
  retrying = false,
}: {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  retryLabel?: string;
  retrying?: boolean;
}) {
  if (isServiceUnavailableMessage(message)) {
    return (
      <BackendDisconnectedOverlay onRetry={onRetry} onDismiss={onDismiss} retrying={retrying} />
    );
  }

  return (
    <Card className="border-destructive/50 bg-destructive/5">
      <p className="text-sm text-destructive">{message}</p>
      {onRetry && (
        <Button className="mt-3" variant="secondary" onClick={onRetry} disabled={retrying}>
          {retrying ? "Retrying…" : retryLabel}
        </Button>
      )}
    </Card>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={cn(
        "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none ring-offset-background placeholder:text-muted-foreground focus:border-ring focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:opacity-50",
        props.className
      )}
    />
  );
}

export function FaceThumb({ faceId, className }: { faceId: number | null; className?: string }) {
  if (!faceId) {
    return (
      <div className={cn("flex items-center justify-center rounded-md bg-muted text-xs text-muted-foreground", className)}>
        ?
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/faces/${faceId}/thumbnail`}
      alt="face"
      className={cn("rounded-md object-cover", className)}
    />
  );
}

export function FilePreview({
  driveFileId,
  name,
  mimeType,
  className,
  onClick,
}: {
  driveFileId: string;
  name: string;
  mimeType: string;
  className?: string;
  onClick?: () => void;
}) {
  const previewUrl = driveFilePreviewUrl(driveFileId, mimeType);
  const driveViewUrl = `https://drive.google.com/file/d/${driveFileId}/view`;
  const isImage = mimeType.startsWith("image/");
  const isPdf = mimeType === "application/pdf";
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  if (isImage) {
    return (
      <div className={cn("relative h-full w-full bg-black/30", className)}>
        {!loaded && !failed && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
            Loading…
          </div>
        )}
        {failed ? (
          <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center text-xs text-muted-foreground">
            <span>Preview unavailable</span>
            <a href={driveViewUrl} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
              Open in Drive
            </a>
          </div>
        ) : (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={previewUrl}
            alt={name}
            loading="lazy"
            decoding="async"
            onLoad={() => setLoaded(true)}
            onError={() => setFailed(true)}
            onClick={onClick}
            className={cn(
              "h-full w-full object-cover transition-opacity duration-200",
              loaded ? "opacity-100" : "opacity-0",
              onClick && "cursor-pointer"
            )}
          />
        )}
      </div>
    );
  }

  if (isPdf) {
    return (
      <iframe
        src={previewUrl}
        title={name}
        className={cn("h-full w-full border-0 bg-white", className)}
      />
    );
  }

  return (
    <div className={cn("flex h-full w-full flex-col items-center justify-center gap-2 bg-muted/50 p-4 text-center", className)}>
      <span className="text-xs text-muted-foreground">{mimeType || "file"}</span>
      <a
        href={driveViewUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="text-xs text-primary hover:underline"
      >
        Open in Drive
      </a>
    </div>
  );
}
