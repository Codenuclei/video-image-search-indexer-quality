"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

/**
 * Full-viewport modal shell portaled to document.body so it covers the mobile
 * header/nav and is not clipped by <main overflow-y-auto>.
 */
export function ModalOverlay({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
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
      className="fixed inset-0 z-[100] overflow-y-auto bg-black/85 p-4 pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))]"
      onClick={onClose}
      role="presentation"
    >
      <div className="flex min-h-[calc(100dvh-2rem)] items-center justify-center">
        <div className="w-full max-w-[min(92vw,40rem)]" onClick={(e) => e.stopPropagation()}>
          {children}
        </div>
      </div>
    </div>,
    document.body
  );
}
