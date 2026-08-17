"use client";

/**
 * Redesigned carousel studio — /test/studio only.
 * Flow: Video → Themes → Topics → Generate copy → Edit → Select images → Finalize.
 * API: real backend via /api/proxy (NEXT_PUBLIC_TEST_USE_REAL_API=0 for mocks).
 */

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowRight,
  Check,
  Clapperboard,
  ImageIcon,
  Loader2,
  Sparkles,
  Target,
  Upload,
  Video,
} from "lucide-react";
import { DriveFolderPanel } from "@/components/drive-folder-panel";
import { TranscriptProgressModal, type TranscriptModalState } from "@/components/transcript-progress-modal";
import { ItemFeedback } from "@/components/item-feedback";
import { ItemReferences } from "@/components/item-references";
import {
  API_BASE,
  testVideoStreamUrl,
  testAssetUrl,
  testApi,
  type TestCarousel,
  type TestExtract,
  type TestGenerate,
  type TestItem,
  type TestSlide,
  type TestTheme,
  type TestVideo,
  type CarouselRunConfig,
} from "@/lib/test-api";
import {
  ensureEnglishTranscript,
  waitForEnglishTranscript,
} from "@/lib/transcript-ensure";
import {
  apiClient,
  formatApiError,
  type CarouselItemFeedback,
  type CarouselItemReference,
  type CarouselPipelineExtractResponse,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { loadRunConfig, persistRunConfig } from "../carousel-llm-picker";
import { StageLlmGenerate } from "../stage-llm-generate";
import { TestIgPost } from "../test-ig-post";
import { TopicsHooksTree } from "../topics-hooks-tree";

type Phase = 1 | 2 | 3 | 4 | 5 | 6;
type StageState = "cache" | "generated" | null;

function StageBadge({ state }: { state: StageState }) {
  if (!state) return null;
  return (
    <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
      {state === "cache" ? "Cache hit" : "Generated"}
    </span>
  );
}

function fmtTs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function toggle(list: string[], text: string): string[] {
  return list.includes(text) ? list.filter((x) => x !== text) : [...list, text];
}

function toggleTheme(list: TestTheme[], theme: TestTheme): TestTheme[] {
  const id = theme.theme_id;
  if (list.some((t) => t.theme_id === id)) {
    return list.filter((t) => t.theme_id !== id);
  }
  return [...list, theme].sort((a, b) => a.start_sec - b.start_sec);
}

function slideCopyText(slide: TestSlide): string {
  return (slide.hook_line || slide.transcript_text || slide.caption || "").trim();
}

function applyTestHookFrames(
  list: TestCarousel[],
  overrides: Record<string, { frame_ts: number; preview_url: string }>
): TestCarousel[] {
  if (!Object.keys(overrides).length) return list;
  return list.map((c) => {
    const hookText = c.hooks?.[0] || c.title || "";
    const frame = overrides[hookText];
    if (!frame || !c.slides?.length) return c;
    return {
      ...c,
      slides: c.slides.map((slide, i) =>
        i === 0
          ? {
              ...slide,
              preview_url: frame.preview_url,
              frame_ts: frame.frame_ts,
              frame_source: "manual",
            }
          : slide
      ),
    };
  });
}

function PhaseRail({ phase }: { phase: Phase }) {
  const steps = [
    { n: 1, label: "Video", Icon: Video },
    { n: 2, label: "Themes", Icon: Sparkles },
    { n: 3, label: "Topics", Icon: Target },
    { n: 4, label: "Copy", Icon: Target },
    { n: 5, label: "Images", Icon: ImageIcon },
    { n: 6, label: "Finalize", Icon: Clapperboard },
  ] as const;
  return (
    <ol className="studio-phase-rail" aria-label="Test studio steps">
      {steps.map((s) => (
        <li
          key={s.n}
          className={cn(
            "studio-phase-chip",
            phase === s.n && "is-active",
            phase > s.n && "is-done"
          )}
        >
          {phase > s.n ? <Check size={12} /> : <s.Icon size={12} />}
          <span>
            {s.n}. {s.label}
          </span>
        </li>
      ))}
    </ol>
  );
}

function CopyEditor({
  carousels,
  onChangeSlideText,
}: {
  carousels: TestCarousel[];
  onChangeSlideText: (carouselId: string, slideIndex: number, text: string) => void;
}) {
  if (!carousels.length) {
    return <p className="text-sm text-muted-foreground">No copy yet.</p>;
  }
  return (
    <div className="test-copy-editor" data-testid="test-copy-editor">
      {carousels.map((c) => (
        <div key={c.id} className="test-copy-card">
          <p className="test-copy-card-title">{c.title || c.hooks?.[0] || "Carousel"}</p>
          <p className="test-copy-card-meta">
            {c.slides?.length ?? 0} slides · edit before selecting images
          </p>
          <ul className="test-copy-slides">
            {(c.slides ?? []).map((slide, i) => (
              <li key={`${c.id}-slide-${i}`}>
                <div className="test-copy-slide-label">
                  <span>Slide {i + 1}</span>
                  <span className="tabular-nums">
                    {fmtTs(slide.timestamp_sec)}
                    {slide.end_timestamp_sec != null
                      ? `–${fmtTs(slide.end_timestamp_sec)}`
                      : ""}
                  </span>
                </div>
                <textarea
                  className="test-copy-slide-input"
                  rows={2}
                  value={slideCopyText(slide)}
                  onChange={(e) => onChangeSlideText(c.id, i, e.target.value)}
                  spellCheck
                  data-testid={`test-copy-slide-${c.id}-${i}`}
                  aria-label={`Slide ${i + 1} copy`}
                />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function TestStudioInner() {
  const searchParams = useSearchParams();
  const videoParam = searchParams.get("video");

  const [phase, setPhase] = useState<Phase>(1);
  const [videos, setVideos] = useState<TestVideo[]>([]);
  const [selected, setSelected] = useState<TestVideo | null>(null);
  const [loadingVideos, setLoadingVideos] = useState(true);
  const [themesLoading, setThemesLoading] = useState(false);
  const [extractLoading, setExtractLoading] = useState(false);
  const [copyLoading, setCopyLoading] = useState(false);
  const [imagesLoading, setImagesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const [previewItem, setPreviewItem] = useState<{ start_sec: number; text: string } | null>(
    null
  );
  const [previewFrames, setPreviewFrames] = useState<
    { frame_ts: number; preview_url: string; text?: string }[]
  >([]);
  const [previewFramesLoading, setPreviewFramesLoading] = useState(false);
  const [itemFeedback, setItemFeedback] = useState<Record<string, CarouselItemFeedback>>({});
  const [itemReferences, setItemReferences] = useState<Record<string, CarouselItemReference[]>>(
    {}
  );
  const [hookFrames, setHookFrames] = useState<
    Record<string, { frame_ts: number; preview_url: string }>
  >({});

  const [themes, setThemes] = useState<TestTheme[]>([]);
  const [selectedThemes, setSelectedThemes] = useState<TestTheme[]>([]);
  const [extract, setExtract] = useState<TestExtract | null>(null);
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [selectedHooks, setSelectedHooks] = useState<string[]>([]);
  const [intent, setIntent] = useState<string | null>(null);
  const [runConfig, setRunConfig] = useState<CarouselRunConfig>(loadRunConfig);
  const [prerunBusy, setPrerunBusy] = useState(false);
  const [prerunNote, setPrerunNote] = useState<string | null>(null);
  const [themeStage, setThemeStage] = useState<StageState>(null);
  const [extractStage, setExtractStage] = useState<StageState>(null);
  const [copyStage, setCopyStage] = useState<StageState>(null);
  const [imageStage, setImageStage] = useState<StageState>(null);
  const [transcriptModal, setTranscriptModal] = useState<TranscriptModalState>({
    open: false,
  });
  const transcriptAbortRef = useRef<AbortController | null>(null);

  const [carousels, setCarousels] = useState<TestCarousel[]>([]);
  const [layouts, setLayouts] = useState<{
    single_1?: TestCarousel[];
    split_2?: TestCarousel[];
  } | null>(null);
  const [carouselLayout, setCarouselLayout] = useState<"single_1" | "split_2">("single_1");
  const [copySource, setCopySource] = useState<string | null>(null);

  const topics = useMemo(() => extract?.topics ?? [], [extract]);
  const hooks = useMemo(() => {
    const seen = new Set<string>();
    return (extract?.hooks ?? []).filter((h) => {
      const key = h.text.trim().toLowerCase();
      if (key.length < 12 || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [extract]);

  const richExtract = useMemo<CarouselPipelineExtractResponse | null>(() => {
    if (!extract) return null;
    return {
      ...extract,
      previews: [],
      verbatim: extract.verbatim ?? true,
      hooks: extract.hooks,
      topics: (extract.topics ?? []).filter((t) => !t.is_subtopic && !t.parent_topic_id),
      topic_tree: extract.topic_tree as CarouselPipelineExtractResponse["topic_tree"],
      save_id: extract.save_id,
    };
  }, [extract]);

  const feedbackHandlers = useMemo(
    () => ({
      onSaved: (item: CarouselItemFeedback) =>
        setItemFeedback((prev) => ({
          ...prev,
          [`${item.target_kind}:${item.target_key}`]: item,
        })),
      onAdded: (item: CarouselItemReference) =>
        setItemReferences((prev) => {
          const key = `${item.target_kind}:${item.target_key}`;
          const existing = prev[key] ?? [];
          if (existing.some((r) => r.id === item.id)) return prev;
          return { ...prev, [key]: [item, ...existing] };
        }),
      onRemoved: (id: number) =>
        setItemReferences((prev) => {
          const next: Record<string, CarouselItemReference[]> = {};
          for (const [k, list] of Object.entries(prev)) {
            const filtered = list.filter((r) => r.id !== id);
            if (filtered.length) next[k] = filtered;
          }
          return next;
        }),
    }),
    []
  );

  const displayCarousels = useMemo(() => {
    const fromLayout =
      carouselLayout === "split_2" ? layouts?.split_2 : layouts?.single_1;
    if (fromLayout?.length) return fromLayout;
    return carousels;
  }, [carouselLayout, layouts, carousels]);

  function applyGenerateResult(
    merged: TestCarousel[],
    genLayouts?: {
      single_1?: { carousels?: TestCarousel[] };
      split_2?: { carousels?: TestCarousel[] };
    } | null,
    source?: string | null
  ) {
    setCarousels(
      applyTestHookFrames(
        merged.map((c, idx) => ({ ...c, id: c.id || `hook_${idx + 1}` })),
        hookFrames
      )
    );
    if (genLayouts) {
      setLayouts({
        single_1: applyTestHookFrames(
          genLayouts.single_1?.carousels?.map((c, i) => ({
            ...c,
            id: c.id || `hook_${i + 1}`,
          })) ?? [],
          hookFrames
        ),
        split_2: applyTestHookFrames(
          genLayouts.split_2?.carousels?.map((c, i) => ({
            ...c,
            id: c.id || `hook_${i + 1}`,
          })) ?? [],
          hookFrames
        ),
      });
    } else {
      setLayouts(null);
    }
    if (source) setCopySource(source);
  }

  function updateSlide(carouselId: string, slideIndex: number, slide: TestSlide) {
    const patch = (list: TestCarousel[]) =>
      list.map((c) => {
        if (c.id !== carouselId) return c;
        const slides = [...(c.slides ?? [])];
        if (slideIndex >= 0 && slideIndex < slides.length) {
          slides[slideIndex] = { ...slides[slideIndex], ...slide };
        }
        return { ...c, slides };
      });
    setCarousels((prev) => patch(prev));
    setLayouts((prev) =>
      prev
        ? {
            single_1: prev.single_1 ? patch(prev.single_1) : prev.single_1,
            split_2: prev.split_2 ? patch(prev.split_2) : prev.split_2,
          }
        : prev
    );
  }

  function onChangeSlideText(carouselId: string, slideIndex: number, text: string) {
    const list = displayCarousels.find((c) => c.id === carouselId);
    const prev = list?.slides?.[slideIndex];
    if (!prev) return;
    updateSlide(carouselId, slideIndex, {
      ...prev,
      hook_line: text,
      transcript_text: text,
      caption: text,
    });
  }

  const loadVideos = useCallback(async () => {
    setLoadingVideos(true);
    try {
      const res = await testApi.recentVideos();
      setVideos(res.items ?? []);
    } catch (e) {
      setError(formatApiError(e, "Could not load videos. Please refresh and try again."));
    } finally {
      setLoadingVideos(false);
    }
  }, []);

  useEffect(() => {
    const task = window.setTimeout(() => void loadVideos(), 0);
    return () => window.clearTimeout(task);
  }, [loadVideos]);

  // Deep-link from library
  useEffect(() => {
    if (!videoParam || !videos.length) return;
    const hit = videos.find((v) => v.id === videoParam);
    if (!hit) return;
    const task = window.setTimeout(() => setSelected(hit), 0);
    return () => window.clearTimeout(task);
  }, [videoParam, videos]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    void apiClient
      .carouselFeedbackList(selected.id)
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, CarouselItemFeedback> = {};
        for (const item of res.items ?? []) {
          map[`${item.target_kind}:${item.target_key}`] = item;
        }
        setItemFeedback(map);
      })
      .catch(() => {
        if (!cancelled) setItemFeedback({});
      });
    void apiClient
      .carouselReferencesList(selected.id)
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, CarouselItemReference[]> = {};
        for (const item of res.items ?? []) {
          const key = `${item.target_kind}:${item.target_key}`;
          (map[key] ??= []).push(item);
        }
        setItemReferences(map);
      })
      .catch(() => {
        if (!cancelled) setItemReferences({});
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  useEffect(() => {
    if (!previewItem || !selected) return;
    let cancelled = false;
    const task = window.setTimeout(() => setPreviewFramesLoading(true), 0);
    void testApi
      .transcriptFrames({
        driveFileId: selected.id,
        startSec: Math.max(0, previewItem.start_sec - 4),
        endSec: previewItem.start_sec + 28,
        limit: 12,
      })
      .then((res) => {
        if (!cancelled) setPreviewFrames(res.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setPreviewFrames([]);
      })
      .finally(() => {
        if (!cancelled) setPreviewFramesLoading(false);
      });
    return () => {
      cancelled = true;
      window.clearTimeout(task);
    };
  }, [previewItem, selected]);

  async function uploadVideos(files: FileList | File[]) {
    const list = Array.from(files);
    if (!list.length) return;
    setUploading(true);
    setUploadNote(null);
    setError(null);
    try {
      const notes: string[] = [];
      let firstId: string | null = null;
      let firstName = "";
      for (const file of list) {
        const res = await testApi.uploadVideo(file);
        notes.push(res.message || `Queued ${res.name}`);
        if (!firstId) {
          firstId = res.drive_file_id;
          firstName = res.name;
        }
        const asVideo: TestVideo = {
          id: res.drive_file_id,
          name: res.name,
          mime_type: file.type || "video/mp4",
          path: null,
          size: res.size ?? file.size,
          status: res.status || "pending",
          has_captions: false,
          cue_count: 0,
        };
        setVideos((prev) =>
          prev.some((v) => v.id === asVideo.id) ? prev : [asVideo, ...prev]
        );
      }
      setUploadNote(notes.join(" · "));
      if (firstId) {
        setSelected({
          id: firstId,
          name: firstName,
          mime_type: "video/mp4",
          path: null,
          size: null,
          status: "pending",
          has_captions: false,
          cue_count: 0,
        });
      }
      void loadVideos();
    } catch (e) {
      setError(formatApiError(e, "Upload failed. Check the file and try again."));
    } finally {
      setUploading(false);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  }

  async function ensureTranscriptForVideo(video: TestVideo, opts?: { force?: boolean }) {
    const hasCues = (video.cue_count ?? 0) > 0 || video.has_captions;
    transcriptAbortRef.current?.abort();
    const ac = new AbortController();
    transcriptAbortRef.current = ac;
    setTranscriptModal({
      open: true,
      videoName: video.name,
      message: hasCues
        ? "Preparing an English transcript…"
        : "Getting transcripts from the video…",
      phase: hasCues ? "english" : "starting",
      error: null,
      cueCount: null,
    });
    setError(null);
    try {
      if (hasCues && !opts?.force) {
        setTranscriptModal((prev) => ({
          ...prev,
          open: true,
          message: "Preparing an English transcript…",
          phase: "english",
        }));
        let english: Awaited<ReturnType<typeof ensureEnglishTranscript>> | null = null;
        try {
          english = await ensureEnglishTranscript(API_BASE, video.id, {
            force: false,
          });
        } catch {
          // Captions already exist — do not block themes because English polish
          // rejected spoken fragments as "incomplete" / missing.
          const next: TestVideo = {
            ...video,
            has_captions: true,
            cue_count: video.cue_count ?? 0,
            status: "processed",
          };
          setSelected(next);
          setVideos((prev) => {
            const without = prev.filter((x) => x.id !== next.id);
            return [next, ...without];
          });
          setTranscriptModal((prev) => ({ ...prev, open: false }));
          setUploadNote(
            `Using existing transcript for “${next.name}” (${next.cue_count} cues).`
          );
          return next;
        }
        const next: TestVideo = {
          ...video,
          has_captions: true,
          cue_count: english?.cue_count ?? video.cue_count ?? 0,
          status: "processed",
        };
        setSelected(next);
        setVideos((prev) => {
          const without = prev.filter((x) => x.id !== next.id);
          return [next, ...without];
        });
        setTranscriptModal({
          open: true,
          videoName: next.name,
          message:
            english?.message ||
            `English transcript ready (${next.cue_count} sentences).`,
          phase: "english_ready",
          cueCount: next.cue_count,
          error: null,
        });
        setUploadNote(`Using “${next.name}” (${next.cue_count} sentences).`);
        return next;
      }

      const { status } = await waitForEnglishTranscript(API_BASE, video.id, {
        force: opts?.force,
        signal: ac.signal,
        onUpdate: (s) => {
          setTranscriptModal((prev) => ({
            ...prev,
            open: true,
            videoName: s.name || video.name,
            message:
              s.message ||
              (s.phase?.includes("english")
                ? "Preparing an English transcript…"
                : "Getting transcripts from the video…"),
            phase: s.phase,
            error:
              s.status === "failed"
                ? s.message ||
                  "We couldn’t prepare this transcript. Please try again."
                : null,
            cueCount: s.status === "ready" ? (s.cue_count ?? null) : null,
          }));
        },
      });
      if (status.status === "failed") {
        setTranscriptModal({
          open: true,
          videoName: video.name,
          error:
            status.message ||
            "We couldn’t prepare this transcript. Please try again.",
          cueCount: 0,
        });
        return null;
      }
      const next: TestVideo = {
        ...video,
        has_captions: true,
        cue_count: status.cue_count ?? 0,
        status: "processed",
      };
      setSelected(next);
      setVideos((prev) => {
        const without = prev.filter((x) => x.id !== next.id);
        return [next, ...without];
      });
      setTranscriptModal({
        open: true,
        videoName: next.name,
        message:
          status.message ||
          `English transcript ready (${next.cue_count} sentences).`,
        phase: status.phase || "english_ready",
        cueCount: next.cue_count,
        error: null,
      });
      setUploadNote(`Using “${next.name}” (${next.cue_count} sentences).`);
      return next;
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return null;
      const msg = formatApiError(
        e,
        "We couldn’t prepare this transcript. Please try again."
      );
      setTranscriptModal({
        open: true,
        videoName: video.name,
        error: msg,
        cueCount: 0,
      });
      return null;
    }
  }

  async function continueToThemes(opts?: { force?: boolean; runConfig?: CarouselRunConfig }) {
    if (!selected || themesLoading) return;
    const cfg = opts?.runConfig ?? runConfig;
    let video = selected;
    const ensured = await ensureTranscriptForVideo(video);
    if (!ensured) return;
    video = ensured;
    setThemesLoading(true);
    setError(null);
    try {
      const themesRes = await testApi.themes(video.id, {
        generate: true,
        force: Boolean(opts?.force),
        runConfig: cfg,
      });
      const nextThemes = themesRes.themes ?? [];
      setThemeStage(themesRes.cache_hit ? "cache" : "generated");
      setExtractStage(null);
      setCopyStage(null);
      setImageStage(null);
      setThemes(nextThemes);
      setSelectedThemes([]);
      setExtract(null);
      setSelectedTopics([]);
      setSelectedHooks([]);
      setCarousels([]);
      setLayouts(null);
      setCopySource(null);
      if (!nextThemes.length) {
        if (themesRes.warning?.toLowerCase().includes("transcript")) {
          const ensured = await ensureTranscriptForVideo(video, { force: true });
          if (ensured) {
            setThemesLoading(false);
            return continueToThemes({ ...opts, force: true });
          }
        }
        setError(
          themesRes.warning
            ? formatApiError(new Error(themesRes.warning), "No themes found for this video. Make sure it has a transcript, then try again.")
            : "No themes found for this video. Make sure it has a transcript, then try again."
        );
        return;
      }
      setTranscriptModal((prev) => ({ ...prev, open: false }));
      setPhase(2);
    } catch (e) {
      setError(
        formatApiError(e, "We couldn’t generate themes for this video. Please try again.")
      );
    } finally {
      setThemesLoading(false);
    }
  }

  async function extractFromSelectedThemes(opts?: {
    force?: boolean;
    runConfig?: CarouselRunConfig;
  }) {
    if (!selected || extractLoading) return;
    if (!selectedThemes.length) {
      setError("Select at least one theme, then extract topics and hooks.");
      return;
    }
    const cfg = opts?.runConfig ?? runConfig;
    setExtractLoading(true);
    setError(null);
    try {
      const ordered = [...selectedThemes].sort((a, b) => a.start_sec - b.start_sec);
      const extractRes = await testApi.extract(selected.id, ordered, {
        force: Boolean(opts?.force),
        runConfig: cfg,
      });
      setExtract(extractRes);
      setExtractStage(extractRes.cache_hit ? "cache" : "generated");
      setCopyStage(null);
      setImageStage(null);
      setSelectedTopics([]);
      setSelectedHooks([]);
      setIntent(extractRes.intent ?? null);
      setCarousels([]);
      setLayouts(null);
      setCopySource(null);
      setPhase(3);
    } catch (e) {
      setError(
        formatApiError(e, "We couldn’t extract topics and hooks. Please try again.")
      );
    } finally {
      setExtractLoading(false);
    }
  }

  /** Phase 4 — text-first generate (no frames yet). */
  async function generateCopy(opts?: { force?: boolean; runConfig?: CarouselRunConfig }) {
    if (!selected || !extract || copyLoading) return;
    if (!selectedHooks.length && !selectedTopics.length) {
      setError("Select at least one topic or hook, then generate copy.");
      return;
    }
    const cfg = opts?.runConfig ?? runConfig;
    setCopyLoading(true);
    setError(null);
    try {
      const intentRes = await testApi.intent(selectedHooks, selectedTopics, cfg);
      const resolvedIntent = intentRes.intent ?? extract.intent ?? "";
      setIntent(resolvedIntent || null);

      const hookPicks = selectedHooks
        .map((text) => hooks.find((h) => h.text === text))
        .filter(Boolean) as TestItem[];
      const topicPicks = selectedTopics
        .map((text) => topics.find((t) => t.text === text))
        .filter(Boolean) as TestItem[];

      // Backend accepts at most one hook and one topic per generate call
      // (same as production /carousel, which loops per hook).
      const jobs: { hooks: TestItem[]; topics: TestItem[] }[] = [];
      if (hookPicks.length) {
        for (const h of hookPicks) {
          jobs.push({ hooks: [h], topics: [] });
        }
      } else {
        for (const t of topicPicks.slice(0, 1)) {
          jobs.push({ hooks: [], topics: [t] });
        }
      }

      const themePayload = selectedThemes.length ? selectedThemes : themes;
      const merged: TestCarousel[] = [];
      let lastLayouts: TestGenerate["layouts"] | null = null;
      let lastCopySource: string | null = null;
      for (let i = 0; i < jobs.length; i++) {
        const job = jobs[i];
        const gen = await testApi.generate({
          drive_file_id: selected.id,
          hooks: job.hooks.map((h) => ({
            text: h.text,
            start_sec: h.start_sec,
            end_sec: h.end_sec,
          })),
          topics: job.topics.map((t) => ({
            text: t.text,
            start_sec: t.start_sec,
            end_sec: t.end_sec,
          })),
          themes: themePayload,
          intent: resolvedIntent,
          select_images: false,
          force: Boolean(opts?.force),
          run_config: cfg,
        });
        const list = gen.carousels ?? [];
        for (const c of list) {
          merged.push({
            ...c,
            id: c.id || `hook_${merged.length + 1}`,
            images_ready: false,
          });
        }
        if (gen.layouts) lastLayouts = gen.layouts;
        if (gen.copy_source) lastCopySource = gen.copy_source;
        setCopyStage(gen.cache_hit ? "cache" : "generated");
      }

      if (!merged.length) {
        setError("No carousel copy came back. Try different topics or hooks.");
        return;
      }

      applyGenerateResult(merged, lastLayouts, lastCopySource);
      setPhase(4);
    } catch (e) {
      setError(formatApiError(e, "We couldn’t generate copy. Please try again."));
    } finally {
      setCopyLoading(false);
    }
  }

  /** Phase 5 — attach ranked frames after copy edit. */
  async function selectImages(opts?: { force?: boolean; runConfig?: CarouselRunConfig }) {
    if (!selected || !carousels.length || imagesLoading) return;
    const cfg = opts?.runConfig ?? runConfig;
    setImagesLoading(true);
    setError(null);
    try {
      const selectedImgs = await testApi.selectImages({
        drive_file_id: selected.id,
        carousels,
        force: Boolean(opts?.force),
        run_config: cfg,
      });
      const withImages = (selectedImgs.carousels ?? carousels).map((c, idx) => ({
        ...c,
        id: c.id || `hook_${idx + 1}`,
        images_ready: true,
      }));
      applyGenerateResult(withImages, selectedImgs.layouts ?? null, copySource);
      setImageStage(selectedImgs.cache_hit ? "cache" : "generated");
      setPhase(5);
    } catch (e) {
      setError(formatApiError(e, "We couldn’t select images. Please try again."));
    } finally {
      setImagesLoading(false);
    }
  }

  function applyRunConfig(next: CarouselRunConfig) {
    persistRunConfig(next);
    setRunConfig(next);
  }

  function finalize() {
    if (!carousels.length) return;
    setPhase(6);
  }

  function resetGeneratedFromPhase(nextPhase: Phase) {
    setCarousels([]);
    setLayouts(null);
    setCopySource(null);
    setPhase((current) => (current > nextPhase ? nextPhase : current));
  }

  function toggleSelectedTheme(theme: TestTheme) {
    setSelectedThemes((prev) => toggleTheme(prev, theme));
    setExtract(null);
    setSelectedTopics([]);
    setSelectedHooks([]);
    setIntent(null);
    resetGeneratedFromPhase(2);
  }

  function toggleSelectedTopic(text: string) {
    setSelectedTopics((prev) => toggle(prev, text));
    setIntent(null);
    resetGeneratedFromPhase(3);
  }

  function toggleSelectedHook(text: string) {
    setSelectedHooks((prev) => toggle(prev, text));
    setIntent(null);
    resetGeneratedFromPhase(3);
  }

  async function runPrerun() {
    if (
      prerunBusy ||
      !selected ||
      selected.status !== "processed" ||
      !(selected.has_captions || (selected.cue_count ?? 0) > 0)
    )
      return;
    setPrerunBusy(true);
    setPrerunNote(null);
    setError(null);
    try {
      const res = await testApi.prerun({
        drive_file_ids: [selected.id],
        force: false,
        run_config: runConfig,
      });
      const hits = res.items.filter(
        (item) => item.themes_cache_hit || item.extract_cache_hit
      ).length;
      const generated = res.items.filter(
        (item) => item.themes_generated || item.extract_generated
      ).length;
      setPrerunNote(
        `Pre-run finished: ${res.ok_count}/${res.count} ok · cache hits ${hits} · generated ${generated}`
      );
    } catch (e) {
      setError(formatApiError(e, "Pre-run failed. Please try again."));
    } finally {
      setPrerunBusy(false);
    }
  }

  const onVideoStep = phase === 1;
  const onThemesStep = phase === 2;
  const onExtractStep = phase === 3;
  const onCopyStep = phase === 4;
  const onImagesStep = phase === 5;
  const transcriptBusy = transcriptModal.open && !transcriptModal.error;

  return (
    <div className="mx-auto max-w-5xl space-y-8 px-4 pb-24 pt-8 sm:px-6 sm:pt-10">
      <header className="studio-rise">
        <p className="studio-eyebrow">Test studio · real API</p>
        <h1 className="studio-title">Create a carousel</h1>
        <p className="studio-lede">
          Pick a video, choose themes, select topics &amp; hooks, generate copy, then select images and finalize.
        </p>
        <PhaseRail phase={phase} />
      </header>

      {error && (
        <div
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          role="alert"
        >
          {error}
          <button
            type="button"
            className="ml-3 underline"
            onClick={() => setError(null)}
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Step 1 — Video */}
      <section className="studio-panel p-5 sm:p-8" data-testid="test-phase-1">
        <p className="studio-section-label">Step 1</p>
        <h2 className="studio-section-heading">
          <Video size={20} />
          Choose a video
        </h2>
        <p className="mt-2 text-sm text-slate-500">
          Upload a local video, select from the indexed library (available while Drive is
          disconnected), or pick an indexed captioned video.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            ref={uploadInputRef}
            type="file"
            accept="video/mp4,video/webm,video/quicktime,video/x-matroska,.mp4,.webm,.mov,.mkv,.avi"
            className="hidden"
            multiple
            onChange={(e) => {
              if (e.target.files?.length) void uploadVideos(e.target.files);
            }}
          />
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            disabled={uploading}
            onClick={() => uploadInputRef.current?.click()}
            data-testid="test-upload-videos"
            title="Upload videos to index at max priority"
          >
            <Upload size={14} />
            {uploading ? "Uploading…" : "Upload video"}
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            disabled={
              prerunBusy ||
              !selected ||
              selected.status !== "processed" ||
              !(selected.has_captions || (selected.cue_count ?? 0) > 0)
            }
            onClick={() => void runPrerun()}
            data-testid="test-prerun"
            title="Warm caches for the selected processed, captioned video"
          >
            <Sparkles size={14} />
            {prerunBusy ? "Pre-running…" : "Pre-run caches"}
          </button>
          {uploadNote && (
            <p className="w-full text-xs text-muted-foreground" role="status">
              {uploadNote}
            </p>
          )}
          {prerunNote && (
            <p className="w-full text-xs text-muted-foreground" role="status">
              {prerunNote}
            </p>
          )}
        </div>

        <DriveFolderPanel
          className="mt-4"
          apiBase={API_BASE}
          testIdPrefix="test-drive"
          onLibraryChanged={() => {
            void loadVideos();
          }}
          onVideoReady={(v) => {
            const cueCount = v.cue_count ?? 0;
            const hasCaptions = Boolean(v.has_captions ?? cueCount > 0);
            const asVideo: TestVideo = {
              id: v.id,
              name: v.name,
              mime_type: v.mime_type || "video/mp4",
              path: null,
              size: null,
              status: v.status,
              has_captions: hasCaptions,
              cue_count: cueCount,
            };
            setSelected(asVideo);
            setVideos((prev) => {
              const without = prev.filter((x) => x.id !== asVideo.id);
              return [asVideo, ...without];
            });
            setError(null);
            if (v.status === "processed" && !hasCaptions) {
              setUploadNote(
                `“${v.name}” is indexed — getting transcripts from the video…`
              );
              void ensureTranscriptForVideo(asVideo);
            } else {
              setUploadNote(
                v.status === "processed"
                  ? `Using Drive video “${v.name}” (${cueCount} cues).`
                  : `Pulling “${v.name}” — indexing at max priority.`
              );
            }
          }}
        />

        <div className="mt-5">
          {loadingVideos ? (
            <p className="text-sm text-muted-foreground">Loading videos…</p>
          ) : videos.length === 0 ? (
            <p className="text-sm text-muted-foreground">No captioned videos — is the backend up?</p>
          ) : (
            <ul className="studio-scroll-fade max-h-[min(20rem,45vh)] divide-y divide-slate-200 overflow-y-auto rounded-xl border border-slate-200 bg-white">
              {videos.map((v) => {
                const on = selected?.id === v.id;
                return (
                  <li key={v.id}>
                    <button
                      type="button"
                      className={cn(
                        "flex w-full items-center gap-3 px-3 py-3 text-left hover:bg-slate-50",
                        on && "bg-slate-100"
                      )}
                      onClick={() => {
                        setSelected(v);
                        setPhase(1);
                        setThemes([]);
                        setSelectedThemes([]);
                        setExtract(null);
                        setCarousels([]);
                        setLayouts(null);
                        setCopySource(null);
                        setSelectedTopics([]);
                        setSelectedHooks([]);
                        setIntent(null);
                        setThemeStage(null);
                        setExtractStage(null);
                        setCopyStage(null);
                        setImageStage(null);
                        setHookFrames({});
                        setThemesLoading(false);
                        setExtractLoading(false);
                        setCopyLoading(false);
                        setImagesLoading(false);
                        setError(null);
                      }}
                      data-testid="test-pick-video"
                    >
                      <span className={cn("studio-check", on && "is-on")}>
                        {on ? <Check size={12} strokeWidth={2.5} /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-semibold">{v.name}</span>
                        <span className="text-xs text-zinc-500">
                          {v.cue_count ?? "…"} cues · {v.status}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {selected && (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
              Selected: <span className="font-medium text-foreground">{selected.name}</span>
            </p>
            <StageLlmGenerate
              label="Generate themes"
              busy={themesLoading}
              disabled={!onVideoStep || !selected || transcriptBusy}
              runConfig={runConfig}
              onRunConfigChange={applyRunConfig}
              onGenerate={(cfg) => continueToThemes({ force: true, runConfig: cfg })}
              testId="test-generate-themes-llm"
            />
            <button
              type="button"
              className="studio-btn studio-btn-primary studio-btn-continue"
              onClick={() => void continueToThemes()}
              disabled={!onVideoStep || themesLoading || transcriptBusy}
              title={
                !onVideoStep
                  ? "Move back to this step to generate themes again"
                  : transcriptBusy
                    ? "Wait for the transcript to finish preparing"
                    : undefined
              }
              data-testid="test-continue-themes"
            >
              {themesLoading ? (
                "Loading themes…"
              ) : (
                <>
                  Continue to themes
                  <ArrowRight size={14} className="studio-btn-continue-arrow" />
                </>
              )}
            </button>
          </div>
        )}
      </section>

      {/* Step 2 — Themes */}
      {phase >= 2 && themes.length > 0 && (
        <section className="studio-panel p-5 sm:p-7" data-testid="test-phase-2-themes">
          <p className="studio-section-label">Step 2</p>
          <h2 className="studio-section-heading">
            <Sparkles size={20} />
            Themes
            <StageBadge state={themeStage} />
          </h2>
          <p className="mt-2 max-w-[65ch] text-sm leading-relaxed text-slate-500">
            Themes are non-overlapping segments of the talk. Select one or more, then extract topics &amp; hooks.
          </p>
          <ul className="mt-4 space-y-2">
            {themes.map((t) => {
              const active = selectedThemes.some((x) => x.theme_id === t.theme_id);
              return (
                <li key={t.theme_id} className="studio-theme-item">
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={active}
                    className={cn(
                      "studio-select-row flex w-full items-start gap-3 px-3 py-3 text-left",
                      active && "is-selected"
                    )}
                    onClick={() => toggleSelectedTheme(t)}
                    data-testid="test-toggle-theme"
                  >
                    <span className={cn("studio-check mt-0.5", active && "is-on")}>
                      {active ? <Check size={12} strokeWidth={2.5} /> : null}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-foreground">{t.title}</span>
                        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                          {fmtTs(t.start_sec)}
                          {t.end_sec != null ? `–${fmtTs(t.end_sec)}` : ""}
                        </span>
                      </span>
                      <span className="mt-1 block line-clamp-2 text-xs text-muted-foreground">
                        {t.summary}
                      </span>
                    </span>
                  </button>
                  {selected ? (
                    <div className="px-3 pb-1">
                      <ItemFeedback
                        driveFileId={selected.id}
                        kind="theme"
                        targetKey={t.theme_id || t.title}
                        targetLabel={t.title}
                        initial={itemFeedback[`theme:${t.theme_id || t.title}`] ?? null}
                        onSaved={feedbackHandlers.onSaved}
                      />
                      <ItemReferences
                        driveFileId={selected.id}
                        kind="theme"
                        targetKey={t.theme_id || t.title}
                        targetLabel={t.title}
                        frameStartSec={t.start_sec}
                        frameEndSec={t.end_sec}
                        items={itemReferences[`theme:${t.theme_id || t.title}`] ?? []}
                        onAdded={feedbackHandlers.onAdded}
                        onRemoved={feedbackHandlers.onRemoved}
                      />
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
          {onThemesStep && (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="studio-btn studio-btn-primary studio-btn-continue"
                onClick={() => void extractFromSelectedThemes({ force: false })}
                disabled={extractLoading || !selectedThemes.length}
                data-testid="test-continue-selection"
              >
                {extractLoading ? (
                  <>
                    <Loader2 size={14} className="animate-spin" aria-hidden />
                    Extracting topics & hooks…
                  </>
                ) : (
                  <>
                    Extract from {selectedThemes.length || 0} theme
                    {selectedThemes.length === 1 ? "" : "s"}
                    <ArrowRight size={14} className="studio-btn-continue-arrow" />
                  </>
                )}
              </button>
              <StageLlmGenerate
                label="Generate topics and hooks"
                busy={extractLoading}
                disabled={!selectedThemes.length}
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={(cfg) =>
                  extractFromSelectedThemes({ force: true, runConfig: cfg })
                }
                testId="test-regen-extract-llm"
              />
            </div>
          )}
          {!onThemesStep && (
            <div className="mt-4">
              <StageLlmGenerate
                label="Generate topics and hooks"
                busy={false}
                disabled
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={() => undefined}
                testId="test-regen-extract-llm-later"
              />
            </div>
          )}
        </section>
      )}

      {/* Step 3 — Topics → Hooks */}
      {extract && phase >= 3 && (
        <section className="studio-panel p-5 sm:p-7" data-testid="test-phase-2">
          <p className="studio-section-label">Step 3</p>
          <h2 className="studio-section-heading">
            Topics &amp; hooks
            <StageBadge state={extractStage} />
          </h2>
          <p className="mt-2 max-w-[65ch] text-sm leading-relaxed text-slate-500">
            Browse topics and hooks — nothing is pre-selected. Preview a moment (video + frames),
            then pick hooks or topics yourself. Topics map directly to hooks (no subtopics).
            {selectedThemes.length
              ? ` Using ${selectedThemes.length} theme${selectedThemes.length === 1 ? "" : "s"}.`
              : ""}
          </p>

          <div className="mt-5">
            {richExtract && selected && (
            <TopicsHooksTree
              driveFileId={selected.id}
              extract={richExtract}
              selectedTopics={selectedTopics}
              selectedHooks={selectedHooks}
              onToggleTopic={toggleSelectedTopic}
              onToggleHook={toggleSelectedHook}
              onPreview={setPreviewItem}
              onRestoreExtract={(next, nextHooks, nextTopics) => {
                setExtract(next as TestExtract);
                // History restore is a click; autosaves already come back empty.
                setSelectedHooks(nextHooks);
                setSelectedTopics(nextTopics);
                resetGeneratedFromPhase(3);
              }}
              onFramePicked={(hookText, frameTs, previewUrl) =>
                setHookFrames((prev) => ({
                  ...prev,
                  [hookText]: { frame_ts: frameTs, preview_url: previewUrl },
                }))
              }
              feedbackByKey={itemFeedback}
              onFeedbackSaved={feedbackHandlers.onSaved}
              referencesByKey={itemReferences}
              onReferenceAdded={feedbackHandlers.onAdded}
              onReferenceRemoved={feedbackHandlers.onRemoved}
            />
            )}
          </div>

          {onExtractStep && (
            <div className="thd-generate-bar mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="studio-btn studio-btn-primary studio-btn-continue"
                onClick={() => void generateCopy({ force: false })}
                disabled={
                  copyLoading || (!selectedHooks.length && !selectedTopics.length)
                }
                data-testid="test-generate-copy"
              >
                {copyLoading ? (
                  "Generating copy…"
                ) : (
                  <>
                    Generate copy
                    <ArrowRight size={14} className="studio-btn-continue-arrow" />
                  </>
                )}
              </button>
              <StageLlmGenerate
                label="Generate copy"
                busy={copyLoading}
                disabled={!selectedHooks.length && !selectedTopics.length}
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={(cfg) => generateCopy({ force: true, runConfig: cfg })}
                testId="test-generate-copy-llm"
              />
              <span className="text-xs text-muted-foreground">
                {selectedHooks.length + selectedTopics.length === 0
                  ? "Select a topic or hook"
                  : `${selectedTopics.length} topic${selectedTopics.length === 1 ? "" : "s"} · ${selectedHooks.length} hook${selectedHooks.length === 1 ? "" : "s"}`}
              </span>
            </div>
          )}
          {!onExtractStep && (
            <div className="mt-4">
              <StageLlmGenerate
                label="Generate copy"
                busy={false}
                disabled
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={() => undefined}
                testId="test-generate-copy-llm-later"
              />
            </div>
          )}
        </section>
      )}

      {/* Step 4 — Edit copy (text-first, no images yet) */}
      {phase >= 4 && (
        <section className="studio-panel p-5 sm:p-7" data-testid="test-phase-3">
          <p className="studio-section-label">Step 4</p>
          <h2 className="studio-section-heading">
            <Target size={20} />
            Edit copy
            <StageBadge state={copyStage} />
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            Review slide lines before attaching frames. Timestamps stay fixed — only text is editable.
          </p>

          {intent && (
            <div className="mt-4 rounded-lg bg-white px-3 py-3 shadow-sm">
              <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                Directional intent
              </p>
              <p className="mt-1 text-sm font-medium">{intent}</p>
            </div>
          )}
          {copySource && (
            <p className="mt-2 text-xs text-muted-foreground">
              Copy source: <span className="font-medium text-foreground">{copySource}</span>
            </p>
          )}

          <div className="mt-5">
            <CopyEditor carousels={displayCarousels} onChangeSlideText={onChangeSlideText} />
          </div>

          {onCopyStep && (
            <div className="mt-5 flex flex-wrap gap-3">
              <StageLlmGenerate
                label="Generate copy"
                busy={copyLoading}
                disabled={!selectedHooks.length && !selectedTopics.length}
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={(cfg) => generateCopy({ force: true, runConfig: cfg })}
                testId="test-regen-copy-llm"
              />
              <button
                type="button"
                className="studio-btn studio-btn-primary studio-btn-continue"
                onClick={() => void selectImages({ force: false })}
                disabled={imagesLoading || !carousels.length}
                data-testid="test-select-images"
              >
                {imagesLoading ? (
                  "Selecting images…"
                ) : (
                  <>
                    Select images
                    <ArrowRight size={14} className="studio-btn-continue-arrow" />
                  </>
                )}
              </button>
            </div>
          )}
          {!onCopyStep && (
            <div className="mt-5">
              <StageLlmGenerate
                label="Generate copy"
                busy={false}
                disabled
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={() => undefined}
                testId="test-regen-copy-llm-later"
              />
            </div>
          )}
        </section>
      )}

      {/* Step 5 — Preview with images */}
      {phase >= 5 && (
        <section className="studio-panel p-5 sm:p-7" data-testid="test-phase-4">
          <p className="studio-section-label">Step 5</p>
          <h2 className="studio-section-heading">
            <ImageIcon size={20} />
            Preview with images
            <StageBadge state={imageStage} />
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            Frames attached. Swap frames or keep editing, then finalize when ready.
          </p>

          <div className="test-ig-stack mt-6" data-testid="carousel-image-preview">
            {displayCarousels.length === 0 ? (
              <p className="text-sm text-muted-foreground">No preview yet.</p>
            ) : (
              displayCarousels.map((c) => (
                <TestIgPost
                  key={`preview-${c.id}`}
                  carousel={c}
                  driveFileId={selected?.id || ""}
                  layoutMode={carouselLayout}
                  onLayoutModeChange={setCarouselLayout}
                  imagesReady
                  runConfig={runConfig}
                  onSlideUpdated={(si, slide) => updateSlide(c.id, si, slide)}
                  onOpenClip={setPreviewItem}
                  feedbackByKey={itemFeedback}
                  referencesByKey={itemReferences}
                  onFeedbackSaved={feedbackHandlers.onSaved}
                  onReferenceAdded={feedbackHandlers.onAdded}
                  onReferenceRemoved={feedbackHandlers.onRemoved}
                />
              ))
            )}
          </div>

          {onImagesStep && (
            <div className="mt-5 flex flex-wrap gap-3">
              <StageLlmGenerate
                label="Generate images"
                busy={imagesLoading}
                disabled={!carousels.length}
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={(cfg) => selectImages({ force: true, runConfig: cfg })}
                testId="test-regen-images-llm"
              />
              <button
                type="button"
                className="studio-btn studio-btn-primary"
                onClick={() => finalize()}
                disabled={!carousels.length}
                data-testid="test-finalize"
              >
                Finalize carousel
                <ArrowRight size={14} />
              </button>
            </div>
          )}
          {phase >= 6 && (
            <div className="mt-5">
              <StageLlmGenerate
                label="Generate images"
                busy={false}
                disabled
                runConfig={runConfig}
                onRunConfigChange={applyRunConfig}
                onGenerate={() => undefined}
                testId="test-regen-images-llm-later"
              />
            </div>
          )}
        </section>
      )}

      {/* Step 6 — Finalize (polished IgPost; distinct from image preview) */}
      {phase >= 6 && (
        <section className="studio-panel space-y-4 p-5 sm:p-7" data-testid="test-phase-5">
          <p className="studio-section-label">Step 6</p>
          <h2 className="studio-section-heading">
            <Clapperboard size={20} />
            {displayCarousels.length
              ? `${displayCarousels.length} carousel${displayCarousels.length === 1 ? "" : "s"}`
              : "Your carousels"}
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            Final output — yellow highlights, layout toggle, Frame / Regenerate. Production studio
            remains at{" "}
            <Link href="/carousel" className="underline underline-offset-2">
              /carousel
            </Link>
            .
          </p>

          <div className="test-ig-stack mt-4">
            {displayCarousels.map((c) => (
              <TestIgPost
                key={`final-${c.id}`}
                carousel={c}
                driveFileId={selected?.id || ""}
                layoutMode={carouselLayout}
                onLayoutModeChange={setCarouselLayout}
                imagesReady
                runConfig={runConfig}
                onSlideUpdated={(si, slide) => updateSlide(c.id, si, slide)}
                onOpenClip={setPreviewItem}
                feedbackByKey={itemFeedback}
                referencesByKey={itemReferences}
                onFeedbackSaved={feedbackHandlers.onSaved}
                onReferenceAdded={feedbackHandlers.onAdded}
                onReferenceRemoved={feedbackHandlers.onRemoved}
              />
            ))}
          </div>
          <p className="text-xs text-muted-foreground" data-testid="test-run-summary">
            Run LLM: <span className="font-medium text-foreground">{runConfig.provider}</span>
            {" · "}
            <span className="font-medium text-foreground">{runConfig.model}</span>
          </p>
        </section>
      )}

      {previewItem && selected && (
        <div className="topics-hooks-frame-overlay" role="dialog" aria-modal="true">
          <div className="topics-hooks-frame-panel">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                  Transcript preview · {fmtTs(previewItem.start_sec)}
                </p>
                <p className="mt-1 text-sm font-medium text-foreground">{previewItem.text}</p>
              </div>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                onClick={() => setPreviewItem(null)}
              >
                Close
              </button>
            </div>
            <video
              ref={previewVideoRef}
              className="mt-4 w-full rounded-lg bg-black"
              src={testVideoStreamUrl(selected.id)}
              controls
              autoPlay
              preload="metadata"
              onLoadedMetadata={() => {
                if (previewVideoRef.current) {
                  previewVideoRef.current.currentTime = previewItem.start_sec;
                }
              }}
            />
            <div className="mt-4" data-testid="test-preview-frames">
              <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                Frames at this moment
              </p>
              {previewFramesLoading && (
                <p className="mt-2 text-xs text-muted-foreground">Loading frames…</p>
              )}
              {!previewFramesLoading && previewFrames.length === 0 && (
                <p className="mt-2 text-xs text-muted-foreground">No cached frames in this span.</p>
              )}
              {previewFrames.length > 0 && (
                <ul className="topics-hooks-frame-grid mt-2">
                  {previewFrames.map((frame) => (
                    <li key={`${frame.frame_ts}-${frame.preview_url}`}>
                      <button
                        type="button"
                        className="topics-hooks-frame-card"
                        onClick={() => {
                          if (previewVideoRef.current) {
                            previewVideoRef.current.currentTime = frame.frame_ts;
                          }
                        }}
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img
                          src={testAssetUrl(frame.preview_url)}
                          alt=""
                          className="topics-hooks-frame-img"
                        />
                        <span className="topics-hooks-frame-ts tabular-nums">
                          {fmtTs(frame.frame_ts)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      <TranscriptProgressModal
        state={transcriptModal}
        onClose={() => setTranscriptModal((prev) => ({ ...prev, open: false }))}
        onRetry={() => {
          if (selected) void ensureTranscriptForVideo(selected, { force: true });
        }}
      />
    </div>
  );
}

export default function TestStudioPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-5xl px-4 py-16 text-sm text-slate-500">
          Loading test studio…
        </div>
      }
    >
      <TestStudioInner />
    </Suspense>
  );
}
