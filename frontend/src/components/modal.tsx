"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";

/**
 * Full-viewport modal shell portaled to document.body so it covers the mobile
 * header/nav and is not clipped by <main overflow-y-auto>.
 */
export function ModalOverlay({
  open,
  onClose,
  children,
  contentClassName,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  contentClassName?: string;
}) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  if (!open || !mounted) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[100] overflow-y-auto bg-black/85 p-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:p-4"
      onClick={onClose}
      role="presentation"
    >
      <div className="flex min-h-[calc(100dvh-1.5rem)] items-center justify-center sm:min-h-[calc(100dvh-2rem)]">
        <div
          className={cn("w-full max-w-[min(92vw,40rem)]", contentClassName)}
          onClick={(e) => e.stopPropagation()}
        >
          {children}
        </div>
      </div>
    </div>,
    document.body
  );
}
