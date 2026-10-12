"use client";

import { useEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import { Download, ExternalLink, FileVideo, Image as ImageIcon, Play, User } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  API_BASE,
  driveFilePreviewUrl,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  driveVideoFrameUrl,
  formatApiError,
  isServiceUnavailableMessage,
} from "@/lib/api";
import { downloadFromUrl } from "@/lib/download";
import { BackendDisconnectedOverlay } from "@/components/backend-disconnected-overlay";
import { LoadingLabel, Spinner } from "@/components/spinner";

export { LoadingLabel, Spinner };

export function IconLink({
  href,
  icon: Icon,
  label,
  variant = "ghost",
  className,
  iconSize = 14,
  ...props
}: {
  href: string;
  icon: LucideIcon;
  label: string;
  variant?: "primary" | "secondary" | "ghost";
  className?: string;
  iconSize?: number;
} & Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "className">) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110",
    secondary: "border border-border bg-muted text-foreground hover:bg-accent",
    ghost: "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
  };
  return (
    <a
      href={href}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
        variants[variant],
        className
      )}
      {...props}
    >
      <Icon size={iconSize} className="shrink-0" aria-hidden />
      <span>{label}</span>
    </a>
  );
}

/** Cross-origin-safe download control (fetch → blob → save). */
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

export function IconButton({
  icon: Icon,
  label,
  variant = "ghost",
  className,
  iconSize = 14,
  ...props
}: {
  icon: LucideIcon;
  label: string;
  variant?: "primary" | "secondary" | "ghost";
  className?: string;
  iconSize?: number;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110",
    secondary: "border border-border bg-muted text-foreground hover:bg-accent",
    ghost: "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
  };
  return (
    <button
      type="button"
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:pointer-events-none disabled:opacity-50",
        variants[variant],
        className
      )}
      {...props}
    >
      <Icon size={iconSize} className="shrink-0" aria-hidden />
      <span>{label}</span>
    </button>
  );
}

export function PersonTags({
  names,
  className,
  links,
}: {
  names: string[];
  className?: string;
  /** Optional person_name → LinkedIn URL map; matched names become profile links. */
  links?: Record<string, string>;
}) {
  if (!names.length) return null;
  return (
    <div className={cn("flex min-h-6 flex-wrap items-center gap-1.5", className)}>
      <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-foreground/70">
        People
      </span>
      {names.map((name) => {
        const linkedinUrl = links?.[name];
        const chipClass =
          "inline-flex max-w-full items-center gap-1 truncate rounded-full border border-primary/25 bg-primary/10 px-2 py-0.5 text-xs font-medium leading-none text-foreground";
        if (linkedinUrl) {
          return (
            <a
              key={name}
              href={linkedinUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(chipClass, "border-sky-500/40 bg-sky-500/10 transition-colors hover:bg-sky-500/20")}
              title={`${name} — LinkedIn profile`}
            >
              {name}
              <ExternalLink size={10} aria-hidden className="shrink-0 text-sky-600 dark:text-sky-400" />
            </a>
          );
        }
        return (
          <span key={name} className={chipClass} title={name}>
            {name}
          </span>
        );
      })}
    </div>
  );
}

export function Card({
  className,
  children,
  ...rest
}: {
  className?: string;
  children: React.ReactNode;
} & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-xl border border-border bg-card p-4 text-card-foreground shadow-sm", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <Card>
      <p className="text-sm text-muted-foreground">{label}</p>
      <div className="mt-1 text-3xl font-semibold text-foreground">{value}</div>
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
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  className?: string;
  type?: "button" | "submit";
  title?: string;
}) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110 active:scale-95 shadow-sm",
    secondary: "border border-border bg-secondary text-secondary-foreground hover:bg-accent",
    danger: "bg-destructive text-destructive-foreground hover:brightness-110 active:scale-95",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150 disabled:pointer-events-none disabled:opacity-50",
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
  confirmLabel?: React.ReactNode;
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
          {retrying ? <LoadingLabel>Retrying…</LoadingLabel> : retryLabel}
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
  const [broken, setBroken] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const autoRetriedFor = useRef<number | null>(null);
  const autoRetryTimer = useRef<number | null>(null);
  useEffect(() => {
    setBroken(false);
    setRetryToken(0);
    autoRetriedFor.current = null;
    if (autoRetryTimer.current != null) {
      window.clearTimeout(autoRetryTimer.current);
      autoRetryTimer.current = null;
    }
    return () => {
      if (autoRetryTimer.current != null) {
        window.clearTimeout(autoRetryTimer.current);
        autoRetryTimer.current = null;
      }
    };
  }, [faceId]);
  if (!faceId) {
    return (
      <div
        className={cn(
          "flex items-center justify-center rounded-md bg-muted text-muted-foreground",
          className
        )}
        aria-hidden
      >
        <User size={18} />
      </div>
    );
  }
  if (broken) {
    return (
      <button
        type="button"
        title="Retry thumbnail"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setBroken(false);
          setRetryToken((n) => n + 1);
        }}
        className={cn(
          "flex items-center justify-center rounded-md bg-muted text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
          className
        )}
      >
        <User size={18} />
      </button>
    );
  }
  const src = `${API_BASE}/faces/${faceId}/thumbnail${retryToken ? `?retry=${retryToken}` : ""}`;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt="face"
      loading="lazy"
      decoding="async"
      className={cn("rounded-md object-cover", className)}
      onError={() => {
        if (autoRetriedFor.current !== faceId) {
          autoRetriedFor.current = faceId;
          autoRetryTimer.current = window.setTimeout(() => {
            autoRetryTimer.current = null;
            setRetryToken((n) => n + 1);
          }, 2000);
          return;
        }
        setBroken(true);
      }}
    />
  );
}

export function DriveMediaThumb({
  driveFileId,
  name,
  isVideo,
  frameTimestamp,
  className,
}: {
  driveFileId: string;
  name: string;
  isVideo?: boolean;
  frameTimestamp?: number | null;
  className?: string;
}) {
  const frameSrc = isVideo ? driveVideoFrameUrl(driveFileId, frameTimestamp) : null;
  const thumbSrc = driveFileThumbnailUrl(driveFileId);
  const [retryToken, setRetryToken] = useState(0);
  const [src, setSrc] = useState(frameSrc ?? thumbSrc);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setSrc(frameSrc ?? thumbSrc);
    setFailed(false);
    setRetryToken(0);
  }, [frameSrc, thumbSrc]);

  if (failed) {
    return (
      <button
        type="button"
        title="Retry thumbnail"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          const next = retryToken + 1;
          setRetryToken(next);
          const bust = (url: string) => `${url}${url.includes("?") ? "&" : "?"}retry=${next}`;
          setSrc(bust(frameSrc ?? thumbSrc));
          setFailed(false);
        }}
        className={cn(
          "flex h-full w-full items-center justify-center bg-muted text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
          className
        )}
      >
        {isVideo ? <FileVideo size={18} /> : <ImageIcon size={18} />}
      </button>
    );
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={name}
      loading="lazy"
      decoding="async"
      onError={() => {
        // Video frame endpoints can 404 for sparse indexes — fall back to Drive thumb once.
        if (isVideo && frameSrc && src.startsWith(frameSrc.split("?")[0]!) && thumbSrc !== frameSrc) {
          const busted =
            retryToken > 0
              ? `${thumbSrc}${thumbSrc.includes("?") ? "&" : "?"}retry=${retryToken}`
              : thumbSrc;
          setSrc(busted);
          return;
        }
        setFailed(true);
      }}
      className={cn("h-full w-full object-cover", className)}
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
  // Images: compressed KB JPEG thumb only — never stream the original.
  const baseThumbUrl = driveFileThumbnailUrl(driveFileId);
  const previewUrl = driveFilePreviewUrl(driveFileId, mimeType);
  const driveViewUrl = driveGoogleViewUrl(driveFileId);
  const isImage = mimeType.startsWith("image/");
  const isVideo = mimeType.startsWith("video/");
  const isPdf = mimeType === "application/pdf";
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const thumbUrl =
    retryToken > 0
      ? `${baseThumbUrl}${baseThumbUrl.includes("?") ? "&" : "?"}retry=${retryToken}`
      : baseThumbUrl;

  useEffect(() => {
    setLoaded(false);
    setFailed(false);
    setRetryToken(0);
  }, [driveFileId]);

  if (isImage) {
    return (
      <div className={cn("relative h-full w-full bg-black/30", className)}>
        {!loaded && !failed && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
            <LoadingLabel size={16}>Loading…</LoadingLabel>
          </div>
        )}
        {failed ? (
          <button
            type="button"
            onClick={() => {
              setFailed(false);
              setLoaded(false);
              setRetryToken((n) => n + 1);
            }}
            className="flex h-full w-full cursor-pointer flex-col items-center justify-center gap-2 p-4 text-center text-xs text-muted-foreground hover:bg-black/10"
          >
            <span>Image unavailable</span>
            <span>Click to retry</span>
          </button>
        ) : (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={thumbUrl}
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
          </>
        )}
      </div>
    );
  }

  if (isVideo) {
    const inner = (
      <>
        <DriveMediaThumb driveFileId={driveFileId} name={name} isVideo />
        <span className="absolute inset-0 flex items-center justify-center bg-black/15">
          <span className="rounded-full bg-black/70 p-2 text-white">
            <Play size={22} fill="currentColor" aria-hidden />
          </span>
        </span>
      </>
    );
    if (onClick) {
      return (
        <button
          type="button"
          onClick={onClick}
          className={cn("relative h-full w-full overflow-hidden bg-black/40", className)}
        >
          {inner}
        </button>
      );
    }
    return (
      <div className={cn("relative h-full w-full overflow-hidden bg-black/40", className)}>
        {inner}
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
      <IconLink
        href={driveViewUrl}
        icon={ExternalLink}
        label="Open in Drive"
        target="_blank"
        rel="noopener noreferrer"
      />
    </div>
  );
}
