"use client";

import { Toaster as Sonner, type ToasterProps } from "sonner";
import "sonner/dist/styles.css";

/** shadcn/ui Sonner toaster — corner popup for API errors. */
export function Toaster({ ...props }: ToasterProps) {
  return (
    <Sonner
      theme="dark"
      className="toaster group"
      position="bottom-right"
      richColors
      closeButton
      style={
        {
          "--normal-bg": "var(--popover)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "var(--border)",
          "--border-radius": "var(--radius)",
        } as React.CSSProperties
      }
      toastOptions={{
        classNames: {
          toast:
            "border border-zinc-700/80 bg-zinc-950/95 text-zinc-100 shadow-xl backdrop-blur-sm",
          description: "text-zinc-400 text-xs",
          title: "text-sm font-medium text-zinc-100",
        },
      }}
      {...props}
    />
  );
}
