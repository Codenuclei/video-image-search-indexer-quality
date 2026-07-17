"use client";

import Image from "next/image";
import { useRouter } from "next/navigation";
import { PlugZap } from "lucide-react";
import { cn } from "@/lib/utils";
import { supportMailto } from "@/lib/support";
import { LoadingLabel } from "@/components/spinner";

function OverlayButton({
  children,
  onClick,
  variant = "primary",
  disabled,
  className,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary";
  disabled?: boolean;
  className?: string;
}) {
  const variants = {
    primary: "bg-primary text-primary-foreground hover:brightness-110 shadow-sm",
    secondary: "border border-border bg-secondary text-secondary-foreground hover:bg-accent",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-lg px-3 py-2 text-sm font-medium transition-all disabled:pointer-events-none disabled:opacity-50",
        variants[variant],
        className
      )}
    >
      {children}
    </button>
  );
}

export function BackendDisconnectedOverlay({
  onRetry,
  onDismiss,
  retrying = false,
}: {
  onRetry?: () => void;
  onDismiss?: () => void;
  retrying?: boolean;
}) {
  const router = useRouter();

  function goDashboard() {
    onDismiss?.();
    router.push("/");
  }

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-background/90 backdrop-blur-sm" aria-hidden />
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="backend-disconnected-title"
        className="relative z-10 w-full max-w-md overflow-hidden rounded-2xl border border-border bg-card shadow-2xl"
      >
        <div className="relative h-40 w-full overflow-hidden bg-muted">
          <Image
            src="/backend-disconnected.png"
            alt="Server connection lost"
            fill
            priority
            quality={72}
            sizes="(max-width: 448px) 100vw, 448px"
            className="object-cover object-center"
          />
          <div className="absolute inset-0 bg-gradient-to-t from-card via-card/20 to-transparent" />
          <div className="absolute bottom-3 left-4 flex items-center gap-2 rounded-full bg-destructive/90 px-3 py-1 text-xs font-medium text-destructive-foreground shadow-sm">
            <PlugZap size={14} />
            Socket disconnected
          </div>
        </div>

        <div className="space-y-4 p-5">
          <div>
            <h2 id="backend-disconnected-title" className="text-lg font-semibold text-foreground">
              Lost connection to DFI
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              The app cannot reach the backend right now. This usually happens during a deploy or a brief
              network interruption.
            </p>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row">
            <OverlayButton className="flex-1" onClick={onRetry} disabled={!onRetry || retrying}>
              {retrying ? <LoadingLabel>Retrying…</LoadingLabel> : "Retry"}
            </OverlayButton>
            <OverlayButton className="flex-1" variant="secondary" onClick={goDashboard}>
              Go back to Dashboard
            </OverlayButton>
          </div>

          <p className="text-center text-xs text-muted-foreground">
            Still stuck?{" "}
            <a href={supportMailto("DFI backend connection issue")} className="font-medium text-primary hover:underline">
              Contact support
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
