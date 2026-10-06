"use client";

import { useEffect, useRef, useState, type ImgHTMLAttributes } from "react";
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
  const [current, setCurrent] = useState(src);
  const [failed, setFailed] = useState(false);
  const attemptRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    attemptRef.current = 0;
    setCurrent(src);
    setFailed(false);
    return () => {
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [src]);

  if (failed) {
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
      src={current}
      alt={alt}
      className={className}
      onError={(e) => {
        onError?.(e);
        const attempt = ++attemptRef.current;
        if (retryTimerRef.current) clearTimeout(retryTimerRef.current);

        // The transcript-frame endpoint may have a coalesced extraction finishing
        // just after this image request. Retry with a cache-buster, then make one
        // explicit non-cache-only request that can complete extraction itself.
        if (attempt <= 3) {
          const base =
            attempt >= 2 ? withoutCacheOnly(current) || withoutCacheOnly(src) || src : src;
          const separator = base.includes("?") ? "&" : "?";
          retryTimerRef.current = setTimeout(
            () => setCurrent(`${base}${separator}_frame_retry=${attempt}`),
            attempt === 1 ? 800 : 1_500
          );
        } else {
          setFailed(true);
        }
      }}
    />
  );
}
