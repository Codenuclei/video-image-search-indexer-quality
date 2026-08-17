"use client";

import { Toaster as Sonner, type ToasterProps } from "sonner";
import "sonner/dist/styles.css";

/** shadcn/ui Sonner toaster — corner popup for API errors. */
export function Toaster({ ...props }: ToasterProps) {
  return (
    <Sonner
      theme="light"
      className="toaster group"
      position="bottom-right"
      richColors
      closeButton
      style={
        {
          "--normal-bg": "var(--card)",
          "--normal-text": "var(--foreground)",
          "--normal-border": "var(--border)",
          "--border-radius": "var(--radius)",
        } as React.CSSProperties
      }
      {...props}
    />
  );
}
