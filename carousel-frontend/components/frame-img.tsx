"use client";

import { useState, type ImgHTMLAttributes } from "react";
import { ImageOff } from "lucide-react";
import { withoutCacheOnly } from "@/lib/api";
import { cn } from "@/lib/utils";

type Props = Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  src: string;
  /** Extra class for the placeholder shown when the image can't load. */
  placeholderClassName?: string;
};

/**
 * Frame thumbnail that never fails silently: a `cache_only=1` 404 is retried once
 * without the flag (backend may extract the frame), and a final failure renders a
 * visible placeholder instead of a broken-image icon.
 */
export function FrameImg({ src, alt = "", className, placeholderClassName, onError, ...rest }: Props) {
  const [state, setState] = useState<{ src: string; current: string; failed: boolean }>({
    src,
    current: src,
    failed: false,
  });
  // Reset when the parent swaps the source.
  const view = state.src === src ? state : { src, current: src, failed: false };

  if (view.failed) {
    return (
      <span
        className={cn(
          "frame-img-fallback inline-flex items-center justify-center bg-slate-100 text-slate-400",
          !(placeholderClassName ?? className) && "h-full min-h-10 w-full",
          placeholderClassName ?? className
        )}
        role="img"
        aria-label="Frame unavailable"
        title="Frame unavailable"
      >
        <ImageOff size={16} />
      </span>
    );
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      {...rest}
      src={view.current}
      alt={alt}
      className={className}
      onError={(e) => {
        onError?.(e);
        const retry = withoutCacheOnly(view.current);
        if (retry && retry !== view.current) {
          setState({ src, current: retry, failed: false });
        } else {
          setState({ src, current: view.current, failed: true });
        }
      }}
    />
  );
}
