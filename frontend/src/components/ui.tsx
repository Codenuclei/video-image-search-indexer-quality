"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { driveFilePreviewUrl } from "@/lib/api";

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
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  className?: string;
}) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110 active:scale-95 shadow-sm",
    secondary: "border border-border bg-secondary text-secondary-foreground hover:bg-accent hover:border-amber-300",
    danger: "bg-destructive text-destructive-foreground hover:brightness-110 active:scale-95",
  };
  return (
    <button
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
