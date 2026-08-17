import {
  apiAssetUrl,
  cacheOnlyAssetUrl,
  type CarouselOutlineSlide,
  type CarouselSnapshotContext,
  type SearchMoment,
} from "@/lib/api";

export function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function formatTimestampRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

/**
 * Crop a frame around the detected speaker instead of the geometric centre, so a
 * guest sitting off-centre stays in view inside the 4:5 portrait canvas.
 */
export function focalPointStyle(source: {
  focal_x?: number | null;
  focal_y?: number | null;
}): { objectPosition: string } {
  const clamp = (value: number | null | undefined, fallback: number) =>
    typeof value === "number" && Number.isFinite(value)
      ? Math.min(Math.max(value, 0), 1)
      : fallback;
  const x = clamp(source.focal_x, 0.5);
  const y = clamp(source.focal_y, 0.4);
  return { objectPosition: `${(x * 100).toFixed(2)}% ${(y * 100).toFixed(2)}%` };
}

/**
 * Pipeline frames must never trigger on-demand extraction, but a frame the user
 * just picked from the transcript may not be on disk yet, so it is allowed to.
 */
export function slideFrameUrl(
  url: string,
  frameSource?: string | null
): string {
  return frameSource === "manual" ? apiAssetUrl(url) : cacheOnlyAssetUrl(url);
}

export type PickedFrame = { frame_ts: number; preview_url: string };

export function uniquePickerFrames<T extends { frame_ts: number; preview_url: string }>(
  items: T[],
  minGapSec = 0.45
): T[] {
  const out: T[] = [];
  const seenUrls = new Set<string>();
  for (const item of items) {
    const url = item.preview_url || "";
    if (!url || seenUrls.has(url)) continue;
    if (out.some((prev) => Math.abs(prev.frame_ts - item.frame_ts) < minGapSec)) continue;
    seenUrls.add(url);
    out.push(item);
  }
  return out;
}

export function splitPanelCaptions(
  panels: { caption?: string | null }[],
  fallbackLine: string
): string[] {
  const line = fallbackLine.trim();
  const caps = panels.map((panel, index) =>
    (panel.caption || (index === 0 ? line : "") || "").trim()
  );
  if (caps.length >= 2 && caps[0] && caps[0] === caps[1]) {
    caps[1] = "";
  }
  return caps;
}

/**
 * Swap only the image shown on a slide. The spoken span is left alone so slide
 * copy, clip playback and split panel timings survive the replacement, and the
 * old focal point is dropped because it was measured on the previous frame.
 */
export function withReplacedFrame(
  slide: CarouselOutlineSlide,
  frame: PickedFrame
): CarouselOutlineSlide {
  const replaced: CarouselOutlineSlide = {
    ...slide,
    preview_url: frame.preview_url,
    frame_ts: frame.frame_ts,
    frame_source: "manual",
    focal_x: null,
    focal_y: null,
    front_face_score: null,
  };
  if (slide.panels?.length) {
    replaced.panels = slide.panels.map((panel, index) =>
      index === 0
        ? {
            ...panel,
            preview_url: frame.preview_url,
            frame_ts: frame.frame_ts,
            focal_x: null,
            focal_y: null,
            front_face_score: null,
          }
        : panel
    );
  }
  return replaced;
}

/**
 * Frames chosen against a hook (before slides existed) land on the opening
 * slide of that hook's carousel once generation produces one.
 */
export function applyHookFrameOverrides<
  T extends {
    hooks?: string[] | null;
    hook_goal?: string | null;
    slides: CarouselOutlineSlide[];
  },
>(carousels: T[], overrides: Record<string, PickedFrame>): T[] {
  if (!Object.keys(overrides).length) return carousels;
  return carousels.map((carousel) => {
    const hookText = carousel.hook_goal || carousel.hooks?.[0] || "";
    const frame = overrides[hookText];
    if (!frame || !carousel.slides?.length) return carousel;
    return {
      ...carousel,
      slides: carousel.slides.map((slide, index) =>
        index === 0 ? withReplacedFrame(slide, frame) : slide
      ),
    };
  });
}

export function momentToSnapshot(moment: SearchMoment): CarouselSnapshotContext {
  return {
    drive_file_id: moment.drive_file_id,
    name: moment.name,
    timestamp_sec: moment.timestamp_sec,
    end_timestamp_sec: moment.end_timestamp_sec ?? null,
    snippet: moment.snippet ?? null,
    match_type: moment.match_type,
    preview_url: moment.preview_url,
  };
}

export function momentKey(m: { drive_file_id: string; timestamp_sec: number }): string {
  return `${m.drive_file_id}:${m.timestamp_sec}`;
}

export function snapshotKey(s: CarouselSnapshotContext | null): string | null {
  if (!s) return null;
  return momentKey(s);
}

export function toggleId(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

export function mergePresets<T extends { id: string }>(prev: T[], extra: T[]): T[] {
  const seen = new Set(prev.map((p) => p.id));
  const merged = [...prev];
  for (const item of extra) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      merged.push(item);
    }
  }
  return merged;
}

export function slugify(label: string): string {
  return (
    label
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_|_$/g, "")
      .slice(0, 48) || "item"
  );
}

export function transcriptThemeId(title: string): string {
  return `tx:${slugify(title)}`;
}
