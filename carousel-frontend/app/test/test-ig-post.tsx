"use client";

/**
 * Polished Instagram-style carousel for /test studio.
 * Ports production IgPost controls (layout / Frame / Regenerate) and
 * always renders yellow keyword highlights when the API provides them.
 * Frame picker prefers browser canvas capture when a playable stream exists.
 */

import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  ImageIcon,
  Pencil,
  Play,
  RefreshCw,
  Upload,
} from "lucide-react";
import { IgHighlightedCaption } from "@/components/ig-caption";
import { ItemFeedback } from "@/components/item-feedback";
import { ItemReferences } from "@/components/item-references";
import { ModalOverlay } from "@/components/modal";
import { captureVideoFramesInRange } from "@/lib/browser-frame-capture";
import {
  apiClient,
  formatApiError,
  type CarouselItemFeedback,
  type CarouselItemReference,
} from "@/lib/api";
import {
  testApi,
  testAssetUrl,
  testVideoStreamUrl,
  type TestCarousel,
  type CarouselRunConfig,
  type TestSlide,
  type TestSlidePanel,
} from "@/lib/test-api";
import { cn } from "@/lib/utils";
import { focalPointStyle } from "@/app/carousel/utils";

function slideUrl(url?: string | null) {
  if (!url) return "";
  // Manual browser captures are already data URLs; API paths get prefixed.
  return testAssetUrl(url);
}

function captionOf(slide: TestSlide): string {
  return (
    slide.transcript_text ||
    slide.caption ||
    slide.hook_line ||
    ""
  ).trim();
}

export function TestIgPost({
  carousel,
  driveFileId,
  layoutMode,
  onLayoutModeChange,
  imagesReady = true,
  runConfig,
  onSlideUpdated,
  onOpenClip,
  references = [],
  feedbackByKey,
  referencesByKey,
  onFeedbackSaved,
  onReferenceAdded,
  onReferenceRemoved,
}: {
  carousel: TestCarousel;
  driveFileId: string;
  layoutMode: "single_1" | "split_2";
  onLayoutModeChange: (mode: "single_1" | "split_2") => void;
  imagesReady?: boolean;
  runConfig: CarouselRunConfig;
  onSlideUpdated?: (slideIndex: number, slide: TestSlide) => void;
  onOpenClip?: (item: { start_sec: number; text: string }) => void;
  references?: CarouselItemReference[];
  feedbackByKey?: Record<string, CarouselItemFeedback>;
  referencesByKey?: Record<string, CarouselItemReference[]>;
  onFeedbackSaved?: (item: CarouselItemFeedback) => void;
  onReferenceAdded?: (item: CarouselItemReference) => void;
  onReferenceRemoved?: (id: number) => void;
}) {
  const slides = carousel.slides ?? [];
  const n = slides.length;
  const [active, setActive] = useState(0);
  const [pickingFrame, setPickingFrame] = useState(false);
  const [changingImage, setChangingImage] = useState(false);
  const [editingCopy, setEditingCopy] = useState(false);
  const [copyDraft, setCopyDraft] = useState("");
  const [copied, setCopied] = useState(false);
  const [imageUrlDraft, setImageUrlDraft] = useState("");
  const [imageBusy, setImageBusy] = useState(false);
  const [imageNote, setImageNote] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [savingCopy, setSavingCopy] = useState(false);
  const trackRef = useRef<HTMLDivElement>(null);
  const imageFileRef = useRef<HTMLInputElement>(null);
  const current = slides[active];
  const imageRefs = references.filter((r) => r.ref_kind === "image" && r.image_url);
  const copyRefs = references.filter((r) => r.ref_kind === "copy" && r.copy_text);
  const slideTargetKey = current
    ? `${carousel.id}:${current.index ?? active}`
    : carousel.id;
  const slideFbKey = `hook:${slideTargetKey}`;

  function goTo(i: number) {
    const next = Math.max(0, Math.min(n - 1, i));
    setActive(next);
    const el = trackRef.current;
    if (el) {
      const child = el.children[next] as HTMLElement | undefined;
      child?.scrollIntoView({ behavior: "smooth", inline: "start", block: "nearest" });
    }
  }

  async function regenerate() {
    if (!current || regenerating) return;
    setRegenerating(true);
    try {
      const res = await testApi.regenerateSlide({
        drive_file_id: driveFileId,
        carousel_id: carousel.id,
        slide_index: active,
        slide: current,
        run_config: runConfig,
      });
      if (res.slide) onSlideUpdated?.(active, res.slide);
    } catch {
      /* demo-tolerant */
    } finally {
      setRegenerating(false);
    }
  }

  function applySlideImage(previewUrl: string, frameTs?: number | null) {
    if (!current) return;
    const next: TestSlide = {
      ...current,
      preview_url: previewUrl,
      frame_ts: frameTs ?? current.frame_ts ?? current.timestamp_sec,
      frame_source: "manual",
    };
    onSlideUpdated?.(active, next);
    setChangingImage(false);
    setImageUrlDraft("");
    setImageNote("Image updated");
    setTimeout(() => setImageNote(null), 1200);
  }

  async function uploadSlideImage(file: File | null | undefined) {
    if (!file) return;
    setImageBusy(true);
    setImageNote("Uploading…");
    try {
      const uploaded = await apiClient.carouselReferenceUploadImage(file);
      applySlideImage(uploaded.url, current?.frame_ts ?? current?.timestamp_sec);
    } catch (e) {
      setImageNote(formatApiError(e, "Upload failed"));
    } finally {
      setImageBusy(false);
      if (imageFileRef.current) imageFileRef.current.value = "";
    }
  }

  async function copySlideText() {
    if (!current) return;
    const text = captionOf(current);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  function startEditCopy() {
    if (!current) return;
    setCopyDraft(captionOf(current));
    setEditingCopy(true);
  }

  function commitEditCopy() {
    if (!current) return;
    const text = copyDraft;
    onSlideUpdated?.(active, {
      ...current,
      hook_line: text,
      transcript_text: text,
      caption: text,
    });
    setEditingCopy(false);
  }

  async function saveCopy() {
    if (savingCopy) return;
    setSavingCopy(true);
    try {
      await apiClient.carouselCopySave({
        drive_file_id: driveFileId,
        layout_mode: layoutMode,
        slides: slides.map((slide, index) => ({
          index: slide.index ?? index,
          hook_line: slide.hook_line,
          transcript_text: slide.transcript_text,
          caption: slide.caption,
          drive_file_id: driveFileId,
          name: carousel.title || "slide",
          timestamp_sec: slide.timestamp_sec,
          end_timestamp_sec: slide.end_timestamp_sec,
          preview_url: slide.preview_url,
          moment_index: slide.index ?? index,
          frame_ts: slide.frame_ts,
          frame_source: slide.frame_source,
          focal_x: slide.focal_x,
          focal_y: slide.focal_y,
          front_face_score: slide.front_face_score,
          panels: slide.panels,
        })),
        theme: { title: carousel.title },
        references: slides.map((slide, index) => ({
          type: "copy+image",
          slide_index: index,
          copy: captionOf(slide),
          image: {
            drive_file_id: driveFileId,
            timestamp_sec: slide.frame_ts ?? slide.timestamp_sec,
            preview_url: slide.preview_url ?? null,
          },
        })),
      });
      setImageNote("Copy saved");
      setTimeout(() => setImageNote(null), 1200);
    } catch (e) {
      setImageNote(formatApiError(e, "Could not save copy"));
    } finally {
      setSavingCopy(false);
    }
  }

  if (!n || !current) {
    return <p className="text-sm text-muted-foreground">No slides yet.</p>;
  }

  return (
    <div className="ig-post studio-fade-in" data-testid="test-ig-carousel-post">
      <div className="ig-post-header">
        <div className="ig-post-header-row">
          <p className="ig-post-title" title={carousel.title}>
            {carousel.title}
          </p>
          <span className="ig-post-count" aria-live="polite">
            {active + 1}/{n}
          </span>
        </div>
        <div className="ig-post-actions" role="toolbar" aria-label="Slide frame controls">
          <div className="studio-field studio-field-inline">
            <label htmlFor={`test-layout-${carousel.id}`} className="sr-only">
              Layout
            </label>
            <select
              id={`test-layout-${carousel.id}`}
              className="studio-select ig-post-action-control"
              value={layoutMode}
              onChange={(e) =>
                onLayoutModeChange(e.target.value as "single_1" | "split_2")
              }
              aria-label="Layout"
            >
              <option value="single_1">Single image</option>
              <option value="split_2">Split panels</option>
            </select>
          </div>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Replace this slide's image with a frame from the transcript"
            onClick={() => {
              setChangingImage(false);
              setPickingFrame(true);
            }}
          >
            <ImageIcon size={14} />
            Frame
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Change this slide's image (upload, URL, or attached ref)"
            onClick={() => {
              setPickingFrame(false);
              setChangingImage((v) => !v);
            }}
            disabled={imageBusy}
            data-testid="test-slide-image"
          >
            <Upload size={14} />
            Image
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Edit this slide's copy"
            onClick={() => (editingCopy ? commitEditCopy() : startEditCopy())}
            data-testid="test-slide-edit"
          >
            <Pencil size={14} />
            {editingCopy ? "Done" : "Edit"}
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Copy this slide's text"
            onClick={() => void copySlideText()}
            data-testid="test-slide-copy"
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? "Copied" : "Copy"}
          </button>
          {imagesReady && (
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
              disabled={regenerating}
              title="Regenerate this slide frame"
              onClick={() => void regenerate()}
            >
              <RefreshCw size={14} className={cn(regenerating && "animate-spin")} />
              {regenerating ? "Working…" : "Regenerate"}
            </button>
          )}
        </div>
      </div>

      {changingImage ? (
        <div className="mt-3 rounded-lg border border-border bg-white px-3 py-2" data-testid="test-slide-image-changer">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Change slide image
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              ref={imageFileRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif,.jpg,.jpeg,.png,.webp,.gif"
              className="sr-only"
              disabled={imageBusy}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void uploadSlideImage(f);
              }}
            />
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              disabled={imageBusy}
              onClick={() => imageFileRef.current?.click()}
            >
              <Upload size={12} />
              Upload file
            </button>
            <input
              type="url"
              className="studio-input min-w-[12rem] flex-1 px-2 py-1 text-xs"
              placeholder="Paste image URL…"
              value={imageUrlDraft}
              disabled={imageBusy}
              onChange={(e) => setImageUrlDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && imageUrlDraft.trim()) {
                  e.preventDefault();
                  applySlideImage(imageUrlDraft.trim());
                }
              }}
            />
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              disabled={imageBusy || !imageUrlDraft.trim()}
              onClick={() => applySlideImage(imageUrlDraft.trim())}
            >
              Use URL
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              onClick={() => setChangingImage(false)}
            >
              Close
            </button>
          </div>
          {imageRefs.length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-2">
              {imageRefs.map((r) => {
                const src =
                  r.image_url?.startsWith("http")
                    ? r.image_url
                    : testAssetUrl(r.image_url || "");
                return (
                  <li key={r.id}>
                    <button
                      type="button"
                      className="overflow-hidden rounded border border-border"
                      title={r.note || r.image_url || "Attached ref"}
                      disabled={imageBusy}
                      onClick={() =>
                        applySlideImage(r.image_url!, r.frame_ts ?? current.frame_ts)
                      }
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={src} alt="" className="h-12 w-12 object-cover" />
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {imageNote ? <p className="mt-1 text-[11px] text-muted-foreground">{imageNote}</p> : null}
        </div>
      ) : null}

      {editingCopy && current ? (
        <label className="mt-3 block rounded-lg border border-border bg-white px-3 py-2">
          <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Edit slide copy
          </span>
          <textarea
            className="mt-2 w-full rounded border border-slate-200 px-2 py-1.5 text-sm"
            rows={3}
            value={copyDraft}
            onChange={(e) => setCopyDraft(e.target.value)}
            onBlur={commitEditCopy}
            data-testid="test-slide-copy-editor"
          />
        </label>
      ) : null}

      {(imageRefs.length > 0 || copyRefs.length > 0) && (
        <div className="mt-3 rounded-lg border border-border bg-slate-50 px-3 py-2" data-testid="test-attached-refs">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Attached references
          </p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {imageRefs.map((r) => {
              const src =
                r.image_url?.startsWith("http")
                  ? r.image_url
                  : testAssetUrl(r.image_url || "");
              return (
                <li key={`img-${r.id}`} className="flex items-center gap-2 text-xs">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={src} alt="" className="h-8 w-8 rounded object-cover" />
                  <span className="truncate">
                    <span className="font-medium">Image</span>
                    {r.note ? ` · ${r.note}` : ""}
                  </span>
                </li>
              );
            })}
            {copyRefs.map((r) => (
              <li key={`copy-${r.id}`} className="text-xs">
                <span className="font-medium">Copy</span>
                {r.note ? ` · ${r.note}` : ""}:{" "}
                <span className="text-muted-foreground">{r.copy_text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="ig-stage mt-2">
        <div
          ref={trackRef}
          className="ig-track"
          role="region"
          aria-roledescription="carousel"
          aria-label="Carousel slides"
        >
          {slides.map((slide, i) => {
            const showReal = Boolean(slide.preview_url);
            const rawPanels =
              layoutMode === "split_2" && (slide.panels?.length ?? 0) >= 2
                ? slide.panels!.slice(0, 2)
                : null;
            const splitPanels =
              rawPanels &&
              rawPanels[0]?.preview_url &&
              rawPanels[1]?.preview_url &&
              rawPanels[0].preview_url !== rawPanels[1].preview_url
                ? rawPanels
                : null;
            const line = captionOf(slide);
            return (
              <article
                key={`${carousel.id}-${slide.index}-${i}`}
                className={cn("ig-slide", splitPanels && "ig-slide-split")}
                aria-label={`Slide ${i + 1} of ${n}`}
                aria-hidden={i !== active}
              >
                {splitPanels ? (
                  splitPanels.map((panel: TestSlidePanel, p: number) => (
                    <div className="ig-panel" key={`${slide.index}-panel-${p}`}>
                      {panel.preview_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={slideUrl(panel.preview_url)}
                          alt=""
                          draggable={false}
                          style={focalPointStyle(panel)}
                        />
                      ) : (
                        <div className="ig-slide-placeholder" aria-hidden>
                          <span className="ig-slide-placeholder-label">No frame</span>
                        </div>
                      )}
                      <div className="ig-panel-scrim" aria-hidden />
                      <p className="ig-panel-caption">
                        <IgHighlightedCaption
                          text={
                            panel.caption ||
                            (p === 0 ? line : "") ||
                            ""
                          }
                          highlight={panel.highlight ?? slide.highlight}
                          highlight_words={
                            panel.highlight_words ?? slide.highlight_words
                          }
                        />
                      </p>
                    </div>
                  ))
                ) : (
                  <>
                    {showReal ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={slideUrl(slide.preview_url)}
                        alt=""
                        draggable={false}
                        style={focalPointStyle(slide)}
                      />
                    ) : (
                      <div className="ig-slide-placeholder" aria-hidden>
                        <span className="ig-slide-placeholder-label">
                          {imagesReady ? "No frame" : "Background pending"}
                        </span>
                      </div>
                    )}
                    <div className="ig-slide-scrim" aria-hidden />
                    <div className="ig-slide-body">
                      <p className="ig-slide-hook">
                        <IgHighlightedCaption
                          text={line}
                          highlight={slide.highlight}
                          highlight_words={slide.highlight_words}
                        />
                      </p>
                    </div>
                  </>
                )}
              </article>
            );
          })}
        </div>

        {n > 1 && (
          <>
            <button
              type="button"
              className="ig-nav ig-nav-prev"
              aria-label="Previous slide"
              disabled={active <= 0}
              onClick={() => goTo(active - 1)}
            >
              <ChevronLeft size={18} strokeWidth={2.25} />
            </button>
            <button
              type="button"
              className="ig-nav ig-nav-next"
              aria-label="Next slide"
              disabled={active >= n - 1}
              onClick={() => goTo(active + 1)}
            >
              <ChevronRight size={18} strokeWidth={2.25} />
            </button>
          </>
        )}
      </div>

      {n > 1 && (
        <div className="ig-dots" role="tablist" aria-label="Slide position">
          {slides.map((slide, i) => (
            <button
              key={`dot-${slide.index}-${i}`}
              type="button"
              className="ig-dot"
              role="tab"
              aria-label={`Go to slide ${i + 1}`}
              aria-selected={i === active}
              data-on={i === active ? "true" : "false"}
              onClick={() => goTo(i)}
            />
          ))}
        </div>
      )}

      {current && driveFileId ? (
        <div className="mt-3 px-1" data-testid="test-slide-feedback">
          <ItemFeedback
            driveFileId={driveFileId}
            kind="hook"
            targetKey={slideTargetKey}
            targetLabel={captionOf(current)}
            initial={feedbackByKey?.[slideFbKey] ?? null}
            onSaved={onFeedbackSaved}
          />
          <ItemReferences
            driveFileId={driveFileId}
            kind="hook"
            targetKey={slideTargetKey}
            targetLabel={captionOf(current)}
            frameStartSec={current.timestamp_sec}
            frameEndSec={current.end_timestamp_sec}
            items={referencesByKey?.[slideFbKey] ?? []}
            onAdded={onReferenceAdded}
            onRemoved={onReferenceRemoved}
          />
        </div>
      ) : null}

      {onOpenClip && current ? (
        <div className="mt-3 flex justify-center">
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm"
            onClick={() =>
              onOpenClip({
                start_sec: current.timestamp_sec,
                text: captionOf(current),
              })
            }
            data-testid="test-open-clip"
          >
            <Play size={14} />
            Open clip at this moment
          </button>
        </div>
      ) : null}

      <button
        type="button"
        className="studio-btn studio-btn-ghost mt-3 w-full"
        disabled={savingCopy}
        onClick={() => void saveCopy()}
        data-testid="test-save-copy"
      >
        {savingCopy ? "Saving…" : "Save copy for this generation"}
      </button>
      {imageNote && !changingImage ? (
        <p className="mt-1 text-center text-[11px] text-muted-foreground">{imageNote}</p>
      ) : null}

      {imagesReady && n > 1 && (
        <div className="ig-filmstrip" aria-label="Slide filmstrip">
          {slides.map((slide, i) => (
            <button
              key={`thumb-${slide.index}-${i}`}
              type="button"
              className="ig-thumb"
              data-on={i === active ? "true" : "false"}
              aria-label={`Slide ${i + 1}`}
              aria-current={i === active ? "true" : undefined}
              onClick={() => goTo(i)}
            >
              {slide.preview_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={slideUrl(slide.preview_url)}
                  alt=""
                  draggable={false}
                  style={focalPointStyle(slide)}
                />
              ) : (
                <span className="ig-slide-placeholder" style={{ position: "absolute", inset: 0 }} />
              )}
              <span className="ig-thumb-num">{i + 1}</span>
            </button>
          ))}
        </div>
      )}

      {pickingFrame && (
        <ModalOverlay open={pickingFrame} onClose={() => setPickingFrame(false)}>
          <TestFramePicker
            driveFileId={driveFileId}
            startSec={current.timestamp_sec}
            endSec={current.end_timestamp_sec}
            hookText={captionOf(current)}
            onClose={() => setPickingFrame(false)}
            onSave={(picked) => {
              if (!picked.length) return;
              const first = picked[0];
              const next: TestSlide = {
                ...current,
                frame_ts: first.frame_ts,
                preview_url: first.preview_url,
                frame_source: "manual",
              };
              onSlideUpdated?.(active, next);
              setPickingFrame(false);
            }}
          />
        </ModalOverlay>
      )}
    </div>
  );
}

type FrameThumb = {
  frame_ts: number;
  preview_url: string;
  source: "browser" | "api";
};

const testFrameJobs = new Map<string, Promise<{ items: FrameThumb[]; sourceNote: string }>>();

function testFrameJobKey(driveFileId: string, startSec: number, endSec?: number | null): string {
  const lo = Math.max(0, startSec - 4);
  const hi = endSec != null ? endSec + 4 : startSec + 28;
  return `${driveFileId}:${lo}:${hi}`;
}

function loadTestFrames(
  driveFileId: string,
  startSec: number,
  endSec?: number | null
): Promise<{ items: FrameThumb[]; sourceNote: string }> {
  const key = testFrameJobKey(driveFileId, startSec, endSec);
  const existing = testFrameJobs.get(key);
  if (existing) return existing;
  const lo = Math.max(0, startSec - 4);
  const hi = endSec != null ? endSec + 4 : startSec + 28;
  const job = (async () => {
    if (driveFileId) {
      try {
        const videoUrl = testVideoStreamUrl(driveFileId);
        const captured = await captureVideoFramesInRange(videoUrl, lo, hi, {
          count: 12,
          maxWidth: 480,
          timeoutMs: 15_000,
        });
        if (captured.length) {
          return {
            items: captured.map((f) => ({
              frame_ts: f.frame_ts,
              preview_url: f.dataUrl,
              source: "browser" as const,
            })),
            sourceNote: "Browser capture",
          };
        }
      } catch {
        /* fall through to API frames */
      }
    }
    const res = await testApi.transcriptFrames({
      driveFileId,
      startSec: lo,
      endSec: hi,
      limit: 24,
    });
    return {
      items: (res.items ?? []).map((item) => ({
        frame_ts: item.frame_ts,
        preview_url: item.preview_url,
        source: "api" as const,
      })),
      sourceNote: "API frames",
    };
  })();
  testFrameJobs.set(key, job);
  job.catch(() => {
    testFrameJobs.delete(key);
  });
  return job;
}

function TestFramePicker({
  driveFileId,
  startSec,
  endSec,
  hookText,
  onClose,
  onSave,
}: {
  driveFileId: string;
  startSec: number;
  endSec?: number | null;
  hookText?: string;
  onClose: () => void;
  onSave: (items: FrameThumb[]) => void | Promise<void>;
}) {
  const [items, setItems] = useState<FrameThumb[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [sourceNote, setSourceNote] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr(null);
    setSourceNote(null);
    loadTestFrames(driveFileId, startSec, endSec)
      .then((res) => {
        if (!alive) return;
        setItems(res.items);
        setSourceNote(res.sourceNote);
      })
      .catch((e) => {
        if (alive) setErr(formatApiError(e, "Could not load frames"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [driveFileId, startSec, endSec]);

  function toggle(ts: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(ts)) next.delete(ts);
      else next.add(ts);
      return next;
    });
  }

  async function commit() {
    const picked = items.filter((item) => selected.has(item.frame_ts));
    if (!picked.length) return;
    setSaving(true);
    try {
      await onSave(picked);
    } finally {
      setSaving(false);
    }
  }

  const n = selected.size;

  return (
    <div
      className="topics-hooks-frame-panel rounded-xl border border-slate-200 bg-white p-4 shadow-xl"
      role="dialog"
      aria-modal="true"
      aria-label="Pick a frame"
      data-testid="test-frame-picker"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Choose image from transcript
          </p>
          {hookText ? (
            <p className="mt-1 line-clamp-2 text-sm font-medium text-foreground">
              “{hookText}”
            </p>
          ) : null}
          {sourceNote && (
            <p className="mt-1 text-[10px] text-muted-foreground">{sourceNote}</p>
          )}
        </div>
        <button type="button" className="studio-btn studio-btn-ghost studio-btn-sm" onClick={onClose}>
          Close
        </button>
      </div>
      {loading && <p className="text-sm text-muted-foreground">Loading frames…</p>}
      {err && <p className="text-sm text-red-600">{err}</p>}
      {!loading && !err && (
        <ul className="topics-hooks-frame-grid max-h-[min(56vh,28rem)] overflow-y-auto">
          {items.map((item) => {
            const on = selected.has(item.frame_ts);
            return (
              <li key={`${item.source}-${item.frame_ts}`}>
                <button
                  type="button"
                  className={cn("topics-hooks-frame-card", on && "is-selected")}
                  aria-pressed={on}
                  onClick={() => toggle(item.frame_ts)}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={
                      item.source === "browser"
                        ? item.preview_url
                        : testAssetUrl(item.preview_url)
                    }
                    alt=""
                    className="topics-hooks-frame-img"
                    width={1080}
                    height={1350}
                  />
                  <span className="topics-hooks-frame-ts tabular-nums">
                    {Math.floor(item.frame_ts / 60)}:
                    {Math.floor(item.frame_ts % 60)
                      .toString()
                      .padStart(2, "0")}
                  </span>
                </button>
              </li>
            );
          })}
          {!items.length && (
            <li className="col-span-full text-sm text-muted-foreground">
              No frames in this span.
            </li>
          )}
        </ul>
      )}
      {items.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground tabular-nums">
            {n === 0 ? "No frames selected" : `${n} frame${n === 1 ? "" : "s"} selected`}
          </p>
          <button
            type="button"
            className="studio-btn studio-btn-primary studio-btn-sm"
            disabled={saving || n === 0}
            onClick={() => void commit()}
          >
            {saving
              ? "Saving…"
              : n === 0
                ? "Save frames"
                : `Save ${n} frame${n === 1 ? "" : "s"}`}
          </button>
        </div>
      ) : null}
    </div>
  );
}
