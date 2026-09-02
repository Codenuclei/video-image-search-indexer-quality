/**
 * API client for `/test` pages.
 *
 * By default talks to the real FastAPI backend via the same durable proxy as
 * production `/carousel` (`NEXT_PUBLIC_API_URL`, usually `/api/proxy` → :8000).
 *
 * Set `NEXT_PUBLIC_TEST_USE_REAL_API=0` to fall back to local `/api/test` mocks.
 */

import { formatApiError } from "@/lib/api";
import { toastApiError } from "@/lib/toast-api-error";

const USE_REAL_API = process.env.NEXT_PUBLIC_TEST_USE_REAL_API !== "0";

const RAW_REAL_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "/api/proxy").replace(/\/+$/, "");
const REAL_API_BASE =
  RAW_REAL_BASE === "/backend" || RAW_REAL_BASE === "/api/proxy"
    ? RAW_REAL_BASE
    : RAW_REAL_BASE || "/api/proxy";

const API_BASE = USE_REAL_API ? REAL_API_BASE : "/api/test";

export { API_BASE, USE_REAL_API };

/** Prefix relative media paths (e.g. `/media/video/.../frame`) with API_BASE. */
export const testAssetUrl = (path: string) => {
  if (!path) return path;
  let url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  if (url.includes("/media/video/") && url.includes("/frame?")) {
    if (!url.includes("ar=")) url = `${url}&ar=4x5`;
    if (!url.includes("cache_only=")) url = `${url}&cache_only=1`;
  }
  return url;
};

/** Playable preview stream for browser frame capture (same-origin via proxy). */
export const testVideoStreamUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${encodeURIComponent(driveFileId)}/preview`;

async function api<T>(path: string, init?: RequestInit & { timeoutMs?: number; silent?: boolean }): Promise<T> {
  const { timeoutMs, silent, ...rest } = init ?? {};
  const timeout =
    typeof timeoutMs === "number" && timeoutMs > 0
      ? timeoutMs
      : String(rest.method || "GET").toUpperCase() === "GET" ||
          String(rest.method || "GET").toUpperCase() === "HEAD"
        ? 15_000
        : 120_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(rest.headers || {}) },
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (error) {
    if (
      (error instanceof DOMException || error instanceof Error) &&
      error.name === "AbortError"
    ) {
      const msg = `Request timed out after ${Math.round(timeout / 1000)}s. The API may be busy — retry in a moment.`;
      if (!silent) toastApiError(msg);
      throw new Error(msg);
    }
    const msg = formatApiError(error);
    if (!silent) toastApiError(msg);
    throw new Error(msg);
  } finally {
    clearTimeout(timer);
  }
}

export type TestVideo = {
  id: string;
  name: string;
  mime_type: string;
  path: string | null;
  size: number | null;
  status: string;
  has_captions?: boolean;
  cue_count?: number;
};

export type TestTheme = {
  theme_id: string;
  title: string;
  start_sec: number;
  end_sec?: number | null;
  summary: string;
};

export type TestItem = {
  id: string;
  text: string;
  start_sec: number;
  end_sec?: number | null;
  explanation?: string | null;
  theme_id?: string | null;
  topic_id?: string | null;
  topic_text?: string | null;
  subtopic_id?: string | null;
  subtopic_text?: string | null;
  is_subtopic?: boolean;
  parent_topic_id?: string | null;
  has_subtopics?: boolean;
  /** Exact spoken transcript line (same as text when verbatim). */
  original_text?: string | null;
  verbatim?: boolean;
  english_source?: string | null;
};

export type TestExtract = {
  drive_file_id: string;
  hooks: TestItem[];
  topics: TestItem[];
  topic_tree?: unknown;
  save_id?: number | null;
  verbatim?: boolean;
  intent?: string | null;
  intent_score?: number | null;
  cache_hit?: boolean;
  message?: string;
  warning?: string;
};

export type TestSlidePanel = {
  role?: string | null;
  frame_ts?: number | null;
  preview_url?: string | null;
  caption?: string | null;
  highlight?: number[] | null;
  highlight_words?: string[] | null;
  focal_x?: number | null;
  focal_y?: number | null;
  front_face_score?: number | null;
};

export type TestFrameCandidateItem = {
  frame_ts: number;
  preview_url?: string | null;
  label?: string | null;
  order?: number | null;
  quality_score?: number | null;
  front_face_score?: number | null;
  selected?: boolean | null;
};

export type TestSlide = {
  index: number;
  hook_line: string;
  caption?: string | null;
  transcript_text?: string | null;
  original_text?: string | null;
  timestamp_sec: number;
  end_timestamp_sec?: number | null;
  preview_url?: string | null;
  frame_ts?: number | null;
  frame_source?: string | null;
  frame_candidates?: number[] | null;
  frame_candidate_items?: TestFrameCandidateItem[] | null;
  frame_quality?: Record<string, unknown> | null;
  highlight?: number[] | null;
  highlight_words?: string[] | null;
  focal_x?: number | null;
  focal_y?: number | null;
  front_face_score?: number | null;
  panels?: TestSlidePanel[] | null;
  copy_source?: string | null;
};

export type TestCarousel = {
  id: string;
  kind: string;
  title: string;
  topic_labels: string[];
  slide_count: number;
  slides: TestSlide[];
  hooks?: string[];
  topics?: string[];
  images_ready?: boolean;
  copy_source?: string | null;
};

export type TestLayoutBundle = {
  layout_mode: "single_1" | "split_2" | string;
  carousels: TestCarousel[];
};

export type TestGenerate = {
  title: string;
  carousels: TestCarousel[];
  images_ready?: boolean;
  intent?: string | null;
  copy_source?: string | null;
  cache_hit?: boolean;
  generated?: boolean;
  layouts?: {
    single_1?: TestLayoutBundle;
    split_2?: TestLayoutBundle;
  } | null;
};

export type CarouselLlmProvider = "auto" | "openrouter" | "claude" | "gemini";

export type CarouselRunConfig = {
  provider: CarouselLlmProvider;
  model: string;
};

export type CarouselLlmModelOption = {
  id: string;
  label: string;
  provider: string;
};

export type CarouselLlmProviderOption = {
  id: string;
  label: string;
};

/** Subset of GET /settings used by the test studio LLM picker. */
export type TestSettings = {
  carousel_llm_provider?: CarouselLlmProvider | string;
  openrouter_model?: string;
  claude_model?: string;
  openrouter_configured?: boolean;
  claude_configured?: boolean;
  carousel_llm_model_options?: CarouselLlmModelOption[];
  [key: string]: unknown;
};

export type TestSettingsUpdate = {
  carousel_llm_provider?: CarouselLlmProvider | string;
  openrouter_model?: string;
  claude_model?: string;
};

export type CarouselLlmModelsResponse = {
  models?: CarouselLlmModelOption[];
  carousel_llm_model_options?: CarouselLlmModelOption[];
  providers?: CarouselLlmProviderOption[];
  carousel_llm_providers?: CarouselLlmProviderOption[];
  openrouter_configured?: boolean;
  claude_configured?: boolean;
  gemini_configured?: boolean;
  claude_model?: string;
  openrouter_model?: string;
  gemini_model?: string;
  carousel_llm_provider?: CarouselLlmProvider | string;
  current?: {
    carousel_llm_provider?: CarouselLlmProvider | string;
    openrouter_model?: string;
    claude_model?: string;
    gemini_model?: string;
  };
};

export const testApi = {
  recentVideos: () =>
    api<{ items: TestVideo[] }>("/search/carousel/recent-videos?limit=20&captioned_only=true"),
  allVideos: (q?: string) => {
    const params = new URLSearchParams({ limit: "40", captioned_only: "true" });
    if (q) params.set("q", q);
    return api<{ items: TestVideo[] }>(`/search/carousel/videos?${params}`);
  },
  youtubeVideos: () => api<TestVideo[]>("/youtube/videos"),
  indexYoutube: (urls: string[]) =>
    api<{
      ok: boolean;
      registered: {
        drive_file_id: string;
        name: string;
        message: string;
      }[];
      index_scheduled: boolean;
    }>("/youtube/videos", {
      method: "POST",
      body: JSON.stringify({ urls, index_now: true, download_local: true }),
    }),
  /** Local video upload → carousel /upload (max-priority indexing). */
  uploadVideo: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API_BASE}/search/carousel/upload`, {
      method: "POST",
      body: form,
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text();
      const msg = formatApiError(new Error(text || res.statusText));
      toastApiError(msg);
      throw new Error(msg);
    }
    return (await res.json()) as {
      drive_file_id: string;
      name: string;
      status: string;
      size?: number;
      queued: boolean;
      message: string;
    };
  },
  themes: (
    driveFileId: string,
    opts?: { force?: boolean; generate?: boolean; runConfig?: CarouselRunConfig }
  ) =>
    api<{
      themes: TestTheme[];
      cache_hit?: boolean;
      generated?: boolean;
      save_id?: number | null;
      warning?: string;
      message?: string;
    }>(
      "/search/carousel/pipeline/themes",
      {
        method: "POST",
        body: JSON.stringify({
          drive_file_id: driveFileId,
          generate: opts?.generate !== false,
          force: Boolean(opts?.force),
          llm_provider: opts?.runConfig?.provider,
          llm_model: opts?.runConfig?.model,
        }),
        // Themes runs 5+ sequential OpenRouter hops (~3 min observed).
        timeoutMs: 600_000,
      }
    ),
  extract: (
    driveFileId: string,
    themes: TestTheme[],
    opts?: {
      force?: boolean;
      generate?: boolean;
      include_hooks?: boolean;
      runConfig?: CarouselRunConfig;
      timeoutMs?: number;
      silent?: boolean;
    }
  ) =>
    api<TestExtract>("/search/carousel/pipeline/extract", {
      method: "POST",
      timeoutMs: opts?.timeoutMs ?? 600_000,
      silent: opts?.silent,
      body: JSON.stringify({
        drive_file_id: driveFileId,
        // Live-generate on miss; same video+themes+LLM config may cache-hit.
        // Different provider/model cache keys force a fresh run.
        generate: opts?.generate !== false,
        force: Boolean(opts?.force),
        // Test studio matches production: topics first, hooks after selection.
        include_hooks: opts?.include_hooks ?? false,
        llm_provider: opts?.runConfig?.provider,
        llm_model: opts?.runConfig?.model,
        themes: themes.map((t) => ({
          theme_id: t.theme_id,
          title: t.title,
          start_sec: t.start_sec,
          end_sec: t.end_sec,
          summary: t.summary,
        })),
      }),
    }),
  extractHooks: (
    driveFileId: string,
    body: {
      themes: TestTheme[];
      topics: {
        id?: string;
        text: string;
        start_sec?: number;
        end_sec?: number | null;
        explanation?: string;
        theme_id?: string | null;
      }[];
      min_hooks?: number;
      max_hooks?: number;
      force?: boolean;
      runConfig?: CarouselRunConfig;
    }
  ) =>
    api<TestExtract>("/search/carousel/pipeline/extract/hooks", {
      method: "POST",
      timeoutMs: 180_000,
      body: JSON.stringify({
        drive_file_id: driveFileId,
        generate: true,
        force: Boolean(body.force),
        min_hooks: body.min_hooks ?? 2,
        max_hooks: body.max_hooks ?? 4,
        llm_provider: body.runConfig?.provider,
        llm_model: body.runConfig?.model,
        themes: body.themes.map((t) => ({
          theme_id: t.theme_id,
          title: t.title,
          start_sec: t.start_sec,
          end_sec: t.end_sec,
          summary: t.summary,
        })),
        topics: body.topics,
      }),
    }),
  intent: (hooks: string[], topics: string[], runConfig?: CarouselRunConfig) =>
    api<{ intent?: string | null; intent_score?: number | null }>(
      "/search/carousel/pipeline/intent",
      {
        method: "POST",
        body: JSON.stringify({
          hooks,
          topics,
          llm_provider: runConfig?.provider,
          llm_model: runConfig?.model,
        }),
      }
    ),
  generate: (body: {
    drive_file_id: string;
    hooks: { text: string; start_sec: number; end_sec?: number | null }[];
    topics: { text: string; start_sec: number; end_sec?: number | null }[];
    themes: TestTheme[];
    intent?: string | null;
    /** Text = text-first (edit copy before frames). Default false. */
    select_images?: boolean;
    force?: boolean;
    run_config?: CarouselRunConfig;
  }) =>
    api<TestGenerate>("/search/carousel/pipeline/generate", {
      method: "POST",
      body: JSON.stringify({
        drive_file_id: body.drive_file_id,
        hooks: body.hooks,
        topics: body.topics,
        themes: body.themes,
        // Backend rejects null; empty string is the schema default.
        intent: body.intent ?? "",
        select_images: body.select_images === true,
        // Claude-preferred Instagram polish + yellow keyword highlights.
        polish_copy: true,
        generate: true,
        force: Boolean(body.force),
        llm_provider: body.run_config?.provider,
        llm_model: body.run_config?.model,
        min_slides: 6,
        max_slides: 10,
      }),
    }),
  /** Attach ranked frames after generate (same as production "Select & filter images"). */
  selectImages: (body: {
    drive_file_id: string;
    carousels: TestCarousel[];
    force?: boolean;
    run_config?: CarouselRunConfig;
  }) =>
    api<TestGenerate>("/search/carousel/pipeline/select-images", {
      method: "POST",
      body: JSON.stringify({
        ...body,
        llm_provider: body.run_config?.provider,
        llm_model: body.run_config?.model,
      }),
    }),
  regenerateSlide: (body: {
    drive_file_id: string;
    carousel_id?: string;
    slide_index: number;
    slide?: TestSlide;
    run_config?: CarouselRunConfig;
  }) =>
    api<{ slide: TestSlide }>("/search/carousel/pipeline/carousel/slide/regenerate", {
      method: "POST",
      body: JSON.stringify({
        ...body,
        llm_provider: body.run_config?.provider,
        llm_model: body.run_config?.model,
      }),
    }),
  transcriptFrames: (opts: {
    driveFileId: string;
    startSec: number;
    endSec?: number | null;
    limit?: number;
  }) => {
    const params = new URLSearchParams({
      drive_file_id: opts.driveFileId,
      start_sec: String(opts.startSec),
      limit: String(opts.limit ?? 24),
    });
    if (opts.endSec != null) params.set("end_sec", String(opts.endSec));
    return api<{
      drive_file_id: string;
      items: { frame_ts: number; preview_url: string; cached?: boolean }[];
    }>(`/search/carousel/pipeline/transcript-frames?${params}`, {
      timeoutMs: 180_000,
      silent: true,
    });
  },
  feedback: (body: {
    drive_file_id: string;
    target_kind: "hook" | "theme";
    target_key: string;
    target_label?: string;
    rating?: "up" | "down" | null;
    comment?: string;
  }) =>
    api<{ ok: boolean; item: { id: number; rating?: string | null; comment?: string | null } }>(
      "/search/carousel/pipeline/feedback",
      { method: "PUT", body: JSON.stringify(body) }
    ),
  addReference: (body: {
    drive_file_id: string;
    target_kind: "hook" | "theme";
    target_key: string;
    target_label?: string;
    ref_kind: "image" | "copy";
    image_url?: string;
    copy_text?: string;
    note?: string;
  }) =>
    api<{ ok: boolean; item: { id: number; image_url?: string | null; copy_text?: string | null } }>(
      "/search/carousel/pipeline/references",
      { method: "POST", body: JSON.stringify(body) }
    ),
  getSettings: () => api<TestSettings>("/settings"),
  updateSettings: (partial: TestSettingsUpdate) =>
    api<TestSettings>("/settings", {
      method: "PUT",
      body: JSON.stringify(partial),
    }),
  /** Optional curated list; 404/unsupported → callers fall back to settings or locals. */
  getCarouselLlmModels: () => api<CarouselLlmModelsResponse>("/settings/carousel-llm-models"),
  prerun: (body: {
    drive_file_ids?: string[];
    force?: boolean;
    run_config?: CarouselRunConfig;
  }) =>
    api<{
      count: number;
      ok_count: number;
      items: {
        themes_cache_hit?: boolean;
        extract_cache_hit?: boolean;
        themes_generated?: boolean;
        extract_generated?: boolean;
      }[];
    }>("/search/carousel/pipeline/prerun", {
      method: "POST",
      body: JSON.stringify({
        drive_file_ids: body.drive_file_ids ?? [],
        force: Boolean(body.force),
        llm_provider: body.run_config?.provider,
        llm_model: body.run_config?.model,
      }),
    }),
};
