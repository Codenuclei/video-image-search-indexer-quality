"use client";

import { useEffect, useState } from "react";
import type { CarouselOutlineSlide } from "@/lib/api";
import {
  type CarouselExportLayout,
  type CarouselRenderOptions,
  renderCarouselSlidePreviewUrl,
} from "@/lib/carousel-export";

export function ExportSlidePreview({
  slide,
  layout,
  slideIndex,
  slideCount,
  label,
  options,
}: {
  slide: CarouselOutlineSlide;
  layout: CarouselExportLayout;
  slideIndex: number;
  slideCount: number;
  label: string;
  options?: CarouselRenderOptions;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    void renderCarouselSlidePreviewUrl(slide, layout, slideIndex, slideCount, options)
      .then((next) => {
        if (cancelled) {
          URL.revokeObjectURL(next);
          return;
        }
        objectUrl = next;
        setFailed(false);
        setUrl(next);
      })
      .catch(() => {
        if (!cancelled) {
          setFailed(true);
          setUrl(null);
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [slide, layout, slideIndex, slideCount, options]);

  if (failed) {
    return (
      <div className="ig-slide-placeholder" aria-hidden data-testid="carousel-export-preview-fallback">
        <span className="ig-slide-placeholder-label">Preview unavailable</span>
      </div>
    );
  }
  if (!url) {
    return (
      <div className="ig-slide-placeholder" aria-hidden data-testid="carousel-export-preview-loading">
        <span className="ig-slide-placeholder-label">Rendering preview</span>
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt={label}
      draggable={false}
      className="ig-export-preview"
      data-testid="carousel-export-preview"
    />
  );
}
