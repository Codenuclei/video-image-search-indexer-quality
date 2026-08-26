/**
 * Carousel Studio API client — talks to the same backend as the main app.
 * Base URL: NEXT_PUBLIC_API_URL.
 * Prefer `/api/proxy` (App Router proxy → :8000) so the browser stays same-origin
 * and long extract/generate calls are not killed by Next rewrite timeouts.
 */

import { toastApiError } from "./toast-api-error";

const RAW_API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "/api/proxy").replace(/\/+$/, "");
/**
 * Prefer the durable App Router proxy. Legacy `/backend` Next rewrites die at ~30s;
 * both `/api/proxy` and `/backend` are now Route Handlers — keep whichever env sets.
 */
const API_BASE =
  RAW_API_BASE === "/backend" || RAW_API_BASE === "/api/proxy"
    ? RAW_API_BASE
    : RAW_API_BASE || "/api/proxy";

export { API_BASE };

export const SERVICE_UNAVAILABLE_MESSAGE =
  "Can't reach the API right now. It may be starting up or temporarily unavailable.";

export function formatApiError(
  error: unknown,
  fallback = "Something went wrong. Please try again."
): string {
  const raw =
    error instanceof Error
      ? error.message.trim()
      : typeof error === "string"
        ? error.trim()
        : "";
  if (!raw) return fallback;

  const lower = raw.toLowerCase();
  if (
    lower === "failed to fetch" ||
    lower.includes("networkerror") ||
    lower.includes("load failed") ||
    lower.includes("network request failed") ||
    lower.includes("econnrefused") ||
    lower.includes("fetch failed")
  ) {
    return SERVICE_UNAVAILABLE_MESSAGE;
  }

  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw) as { detail?: string | { msg?: string }[] };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        return friendlyDetail(parsed.detail.trim(), fallback);
      }
      if (Array.isArray(parsed.detail) && parsed.detail[0]?.msg) {
        return friendlyDetail(String(parsed.detail[0].msg), fallback);
      }
    } catch {
      // fall through
    }
  }

  if (
    lower === "failed to fetch" ||
    /localhost:\d+/i.test(raw) ||
    /127\.0\.0\.1/i.test(raw) ||
    /internal server error/i.test(raw) ||
    /<html/i.test(raw) ||
    /traceback \(most recent call last\)/i.test(raw) ||
    /file ".*", line \d+/i.test(raw)
  ) {
    return fallback === "Something went wrong. Please try again."
      ? SERVICE_UNAVAILABLE_MESSAGE
      : fallback;
  }

  return friendlyDetail(raw, fallback);
}

function looksLibraryDiagnostic(detail: string): boolean {
  const lower = detail.toLowerCase();
  return (
    lower.includes("traceback") ||
    lower.includes("exception:") ||
    /at\s+\S+\s+\(\S+:\d+:\d+\)/.test(detail) ||
    /\b(?:type|reference|syntax|value|key|attribute|name|index|runtime|assertion)error\b/.test(
      lower
    ) ||
    /format ['"][^'"]+['"] not found/i.test(detail) ||
    /returned from (the )?api/i.test(lower) ||
    /unknown (?:format|strftime|token)/i.test(detail) ||
    /invalid format (?:string|code|specifier)/i.test(detail) ||
    lower.includes("sqlalchemy") ||
    lower.includes("asyncpg") ||
    lower.includes("psycopg") ||
    lower.includes("integrityerror") ||
    lower.includes("operationalerror") ||
    detail.startsWith("{") ||
    detail.startsWith("[") ||
    detail.startsWith("<") ||
    detail.includes("\n") ||
    detail.length > 180 ||
    /file ["'].*["'], line \d+/i.test(detail) ||
    /internal server error|status code 50\d/i.test(detail) ||
    /\b(?:select|insert|update|delete)\b.+\bfrom\b/i.test(detail)
  );
}

function friendlyDetail(detail: string, fallback: string): string {
  if (looksLibraryDiagnostic(detail)) return fallback;
  return detail;
}

/** Keep local-only rows (e.g. a just-uploaded file) at the front of an API list. */
export function prependUniqueById<T extends { id: string }>(prefix: T[], rest: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of [...prefix, ...rest]) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}

export function isServiceUnavailableMessage(message: string): boolean {
  return message === SERVICE_UNAVAILABLE_MESSAGE;
}

function throwApiError(error: unknown, fallback?: string, silent?: boolean): never {
  const msg = formatApiError(error, fallback);
  if (!silent) toastApiError(msg);
  throw new Error(msg);
}

export type PersonRole = "student" | "non_student" | null;

export type Person = {
  id: number;
  name: string;
  role: PersonRole;
  representative_face_id: number | null;
  occurrence_count: number;
  created_at: string;
};

export type CarouselPresetItem = {
  id: string;
  label: string;
  blurb: string;
};

export type CarouselPresets = {
  hooks: CarouselPresetItem[];
  topics: CarouselPresetItem[];
};

export type CarouselSnapshotContext = {
  drive_file_id: string;
  name: string;
  timestamp_sec: number;
  end_timestamp_sec?: number | null;
  snippet?: string | null;
  match_type?: string | null;
  preview_url?: string | null;
};

export type CarouselOutlineSlide = {
  index: number;
  hook_line: string;
  highlight?: number[] | null;
  highlight_words?: string[] | null;
  transcript_text?: string | null;
  original_text?: string | null;
  translated?: boolean | null;
  english_source?: string | null;
  caption?: string | null;
  drive_file_id: string;
  name: string;
  timestamp_sec: number;
  end_timestamp_sec?: number | null;
  snippet?: string | null;
  match_type?: string | null;
  preview_url?: string | null;
  moment_index: number;
  frame_ts?: number | null;
  frame_source?: "ai" | "heuristic" | "fallback" | "deferred" | "manual" | string | null;
  instagram_ready?: boolean | null;
  images_ready?: boolean | null;
  frames_prewarmed?: boolean | null;
  frame_candidates?: number[] | null;
  frame_quality?: Record<string, unknown> | null;
  frame_diversity?: {
    adjacent_duplicate_avoided?: boolean;
    phash_available?: boolean;
  } | null;
  focal_x?: number | null;
  focal_y?: number | null;
  front_face_score?: number | null;
  panels?: CarouselSlidePanel[] | null;
};

export type CarouselSlidePanel = {
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

export type CarouselLayoutBundle = {
  layout_mode: "single_1" | "split_2" | string;
  carousels: CarouselGeneratedItem[];
};

export type CarouselLayouts = {
  single_1?: CarouselLayoutBundle;
  split_2?: CarouselLayoutBundle;
};

export type CarouselOutlineResponse = {
  source: string;
  title: string;
  slide_count: number;
  hooks: string[];
  topics: string[];
  slides: CarouselOutlineSlide[];
  cues?: CarouselCueItem[];
  warning?: string;
  message?: string;
  cache_hit?: boolean;
  generated?: boolean;
  carousels?: CarouselGeneratedItem[];
  carousel_count?: number;
  images_ready?: boolean;
  intent?: string | null;
  layouts?: CarouselLayouts | null;
  references?: CarouselItemReference[];
  quality?: {
    candidates?: number;
    kept?: number;
    rejected?: Record<string, number>;
    slides_polished?: number;
  };
  quality_summary?: CarouselQualitySummary | null;
};

export type CarouselQualityDimensions = {
  cover_strength?: number;
  swipe_progression?: number;
  copy_readability?: number;
  idea_uniqueness?: number;
  ending_payoff?: number;
};

export type CarouselQualityReport = {
  score: number;
  dimensions?: CarouselQualityDimensions;
  issues?: string[];
  repairs?: string[];
  duplicate_pairs?: number[][];
  grounding?: "transcript_locked" | string;
};

export type CarouselQualitySummary = {
  carousel_count: number;
  average_score: number;
  needs_attention: number;
  issue_count: number;
  repair_count: number;
  algorithm?: string;
};

export type CarouselGeneratedItem = {
  id: string;
  kind: "hook" | "topic" | "mixed" | string;
  title: string;
  topic_labels: string[];
  slide_count: number;
  slides: CarouselOutlineSlide[];
  hooks?: string[];
  hook_goal?: string | null;
  topics?: string[];
  images_ready?: boolean;
  plan_source?: string | null;
  quality_report?: CarouselQualityReport | null;
  /** Theme/hook image+copy refs that influenced this carousel. */
  references?: CarouselItemReference[];
};

export type CarouselTimedPick = {
  id?: string;
  text: string;
  start_sec: number;
  end_sec?: number | null;
  theme_id?: string | null;
  topic_id?: string | null;
  topic_text?: string | null;
  original_text?: string | null;
  time_ranges?: { start_sec: number; end_sec?: number | null }[];
};

export type CarouselGenerateRequest = {
  drive_file_id: string;
  video_name?: string;
  intent?: string;
  themes?: {
    theme_id?: string;
    title?: string;
    start_sec: number;
    end_sec?: number | null;
    summary?: string;
  }[];
  hooks?: CarouselTimedPick[];
  topics?: CarouselTimedPick[];
  min_slides?: number;
  max_slides?: number;
  select_images?: boolean;
  /** Finalize concise Instagram copy and sparse keyword highlights. */
  polish_copy?: boolean;
  /** Explicit generate on cache miss. Continue/Load leave this false. */
  generate?: boolean;
  /** Explicit regenerate — bypasses cache. */
  force?: boolean;
  llm_provider?: string;
  llm_model?: string;
};

/** Studio picker from /test sessionStorage, if the user set one this tab. */
export function studioLlmFields(): { llm_provider?: string; llm_model?: string } {
  if (typeof window === "undefined") return {};
  try {
    const raw = sessionStorage.getItem("test-carousel-run-config");
    if (!raw) return {};
    const parsed = JSON.parse(raw) as { provider?: string; model?: string };
    if (parsed?.provider && parsed?.model) {
      return { llm_provider: parsed.provider, llm_model: parsed.model };
    }
  } catch {
    /* ignore */
  }
  return {};
}

export type CarouselCueItem = {
  kind: "hook" | "topic" | string;
  id: string;
  label: string;
  snapshot?: CarouselSnapshotContext | null;
  score?: number;
  cue_text?: string | null;
};

export type CarouselRecentVideo = {
  id: string;
  name: string;
  mime_type: string;
  path: string | null;
  size: number | null;
  modified_time: string | null;
  last_synced_at: string | null;
  created_at?: string | null;
  status: string;
  has_captions?: boolean;
  cue_count?: number;
};

export type YoutubeDriveFile = {
  id: string;
  name: string;
  mime_type: string;
  path: string;
  status: string;
  size: number | null;
  modified_time: string | null;
  last_synced_at: string | null;
  error_message: string | null;
  source?: string;
};

export type YoutubeRegisterResult = {
  drive_file_id: string;
  name: string;
  youtube_video_id: string | null;
  linked_to_drive: boolean;
  download_queued?: boolean;
  message: string;
};

export type YoutubeRegisterResponse = {
  ok: boolean;
  registered: YoutubeRegisterResult[];
  index_scheduled: boolean;
};

export type CarouselPipelineTheme = {
  theme_id: string;
  title: string;
  start_sec: number;
  end_sec?: number | null;
  summary: string;
  harmonized?: boolean;
  search_entity?: string | null;
};

export type CarouselPipelineThemesResponse = {
  source: string;
  drive_file_id: string;
  name: string;
  search_entity?: string | null;
  person_name?: string | null;
  person_found?: boolean | null;
  harmonized: boolean;
  cue_count?: number;
  themes: CarouselPipelineTheme[];
  cache_hit?: boolean;
  generated?: boolean;
  save_id?: number | null;
  transcript_hash?: string | null;
  model?: string | null;
  error?: string;
  message?: string;
  warning?: string;
};

export type CarouselVerbatimItem = {
  id: string;
  text: string;
  start_sec: number;
  end_sec?: number | null;
  verbatim?: boolean;
  analysed?: boolean;
  translated?: boolean;
  original_text?: string | null;
  english_source?: string | null;
  theme_id?: string | null;
  topic_id?: string | null;
  topic_text?: string | null;
  subtopic_id?: string | null;
  subtopic_text?: string | null;
  explanation?: string | null;
  parent_topic_id?: string | null;
  is_subtopic?: boolean;
  has_subtopics?: boolean;
};

export type CarouselTopicTreeNode = {
  id: string;
  text: string;
  start_sec: number;
  end_sec?: number | null;
  time_ranges?: { start_sec: number; end_sec?: number | null }[];
  explanation?: string | null;
  theme_id?: string | null;
  subtopics?: CarouselTopicTreeNode[];
  hooks?: CarouselVerbatimItem[];
};

export type CarouselPipelineExtractResponse = {
  drive_file_id: string;
  theme_id?: string | null;
  theme_ids?: string[];
  hooks: CarouselVerbatimItem[];
  topics: CarouselVerbatimItem[];
  topic_tree?: CarouselTopicTreeNode[];
  save_id?: number | null;
  previews: {
    start_sec: number;
    end_sec?: number | null;
    text: string;
    label: string;
    theme_id?: string | null;
    theme_title?: string | null;
  }[];
  intent?: string | null;
  intent_score?: number | null;
  intent_source?: string | null;
  verbatim: boolean;
  hooks_english?: boolean;
  topics_english?: boolean;
  any_translated?: boolean;
  english_source?: string | null;
  cache_hit?: boolean;
  generated?: boolean;
  message?: string;
  warning?: string;
  transcript_meta?: {
    cue_count_total?: number;
    theme_count?: number;
    transcript_chars_sent?: number;
    chunks_used?: number;
    topic_tree_count?: number;
    flat_topic_count?: number;
    hook_count?: number;
    topics_with_multi_ranges?: number;
    verbatim_guard?: {
      checked?: number;
      rejected_verbatim?: number;
      rewritten?: number;
      dropped?: number;
    };
    per_theme?: Record<string, unknown>[];
  } | null;
};

export type CarouselGenerationSaveListItem = {
  id: number;
  drive_file_id: string;
  kind?: "topics_hooks" | "themes" | "carousel" | string;
  theme_key: string;
  label?: string | null;
  created_at?: string | null;
  source?: string | null;
  model?: string | null;
  transcript_hash?: string | null;
  hook_count?: number;
  topic_count?: number;
  theme_count?: number;
  status?: string;
  input_hash?: string | null;
  layout_mode?: "single_1" | "split_2" | string;
  copy_version?: number;
};

export type CarouselTranscriptFrameItem = {
  start_sec: number;
  end_sec?: number | null;
  text: string;
  frame_ts: number;
  preview_url: string;
  cached?: boolean;
};

export type CarouselItemFeedback = {
  id: number;
  drive_file_id: string;
  target_kind: "theme" | "hook" | string;
  target_key: string;
  target_label?: string | null;
  rating?: "up" | "down" | null;
  comment?: string | null;
  updated_at?: string | null;
};

export type CarouselItemReference = {
  id: number;
  drive_file_id: string;
  target_kind: "theme" | "hook" | string;
  target_key: string;
  target_label?: string | null;
  ref_kind: "image" | "copy" | string;
  image_url?: string | null;
  frame_ts?: number | null;
  copy_text?: string | null;
  note?: string | null;
  updated_at?: string | null;
};

async function api<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number; silent?: boolean }
): Promise<T> {
  const { timeoutMs, silent, ...rest } = init ?? {};
  const timeout =
    typeof timeoutMs === "number" && timeoutMs > 0
      ? timeoutMs
      : // Default short timeout for list/cache reads so a wedged backend cannot
        // leave the studio spinner forever. Long extract/generate pass their own.
        45_000;
  const controller = new AbortController();
  const external = rest.signal;
  const onAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", onAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...rest?.headers },
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text();
      if (res.status >= 500) {
        const textBody = text.trim();
        if (/^internal server error$/i.test(textBody)) {
          throw new Error(
            "The API proxy timed out before extract finished. Soft-refresh the studio and try again — long extract calls now go through a durable proxy."
          );
        }
        throw new Error(SERVICE_UNAVAILABLE_MESSAGE);
      }
      throw new Error(text || res.statusText);
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (error) {
    if (
      (error instanceof DOMException || error instanceof Error) &&
      error.name === "AbortError"
    ) {
      if (external?.aborted) throw error;
      return throwApiError(
        `Request timed out after ${Math.round(timeout / 1000)}s. The API may be busy — retry in a moment.`,
        undefined,
        silent
      );
    }
    return throwApiError(error, undefined, silent);
  } finally {
    clearTimeout(timer);
    if (external) external.removeEventListener("abort", onAbort);
  }
}

export const driveFileDownloadUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/download`;

export const driveVideoStreamUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/preview`;

/** Video frames are cached at the source video's native aspect (9:16 for
 * reels); the carousel studio always wants the Instagram 4:5 portrait crop. */
const withCarouselAspect = (asset: string) => {
  if (!asset.includes("/media/video/") || !asset.includes("/frame?")) return asset;
  return asset.includes("ar=") ? asset : `${asset}&ar=4x5`;
};

export const apiAssetUrl = (path: string) =>
  withCarouselAspect(path.startsWith("http") ? path : `${API_BASE}${path}`);

export const cacheOnlyAssetUrl = (path: string) => {
  const asset = apiAssetUrl(path);
  if (!asset.includes("/media/video/") || !asset.includes("/frame?")) return asset;
  return asset.includes("cache_only=") ? asset : `${asset}&cache_only=1`;
};

export const apiClient = {
  persons: () => api<Person[]>("/persons"),
  youtubeVideos: () => api<YoutubeDriveFile[]>("/youtube/videos"),
  addYoutubeVideos: (urls: string[], indexNow = true, downloadLocal = true) =>
    api<YoutubeRegisterResponse>("/youtube/videos", {
      method: "POST",
      body: JSON.stringify({ urls, index_now: indexNow, download_local: downloadLocal }),
      timeoutMs: 120_000,
    }),
  carouselRecentVideos: (limit = 5, captionedOnly = true) =>
    api<{ items: CarouselRecentVideo[]; captioned_only?: boolean }>(
      `/search/carousel/recent-videos?limit=${limit}&captioned_only=${captionedOnly ? "true" : "false"}`,
      { timeoutMs: 15_000 }
    ),
  carouselVideos: (opts?: {
    q?: string;
    limit?: number;
    offset?: number;
    captionedOnly?: boolean;
  }) => {
    const params = new URLSearchParams();
    if (opts?.q) params.set("q", opts.q);
    params.set("limit", String(opts?.limit ?? 20));
    params.set("offset", String(opts?.offset ?? 0));
    params.set("captioned_only", opts?.captionedOnly === false ? "false" : "true");
    return api<{
      items: CarouselRecentVideo[];
      q?: string | null;
      captioned_only?: boolean;
      limit?: number;
      offset?: number;
      has_more?: boolean;
    }>(`/search/carousel/videos?${params}`);
  },
  carouselPipelineThemes: (
    driveFileId: string,
    opts?: {
      searchEntity?: string;
      personName?: string;
      force?: boolean;
      generate?: boolean;
      signal?: AbortSignal;
    }
  ) =>
    api<CarouselPipelineThemesResponse>("/search/carousel/pipeline/themes", {
      method: "POST",
      body: JSON.stringify({
        drive_file_id: driveFileId,
        search_entity: opts?.searchEntity ?? "",
        person_name: opts?.personName ?? "",
        force: Boolean(opts?.force),
        generate: Boolean(opts?.generate),
      }),
      signal: opts?.signal,
      timeoutMs: opts?.force || opts?.generate ? 300_000 : 90_000,
    }),
  carouselPipelineExtract: (body: {
    drive_file_id: string;
    theme_id?: string;
    title?: string;
    start_sec?: number;
    end_sec?: number | null;
    summary?: string;
    search_entity?: string;
    themes?: {
      theme_id?: string;
      title?: string;
      start_sec: number;
      end_sec?: number | null;
      summary?: string;
    }[];
    force?: boolean;
    generate?: boolean;
  }) =>
    api<CarouselPipelineExtractResponse>("/search/carousel/pipeline/extract", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: body.force || body.generate ? 900_000 : 90_000,
    }),
  carouselPipelineIntent: (body: {
    theme_title?: string;
    theme_summary?: string;
    theme_titles?: string[];
    theme_summaries?: string[];
    hooks?: string[];
    topics?: string[];
    search_entity?: string;
  }) =>
    api<{ intent?: string | null; intent_score?: number | null; intent_source?: string | null }>(
      "/search/carousel/pipeline/intent",
      {
        method: "POST",
        body: JSON.stringify(body),
        timeoutMs: 120_000,
      }
    ),
  carouselPipelineGenerate: (body: CarouselGenerateRequest) =>
    api<CarouselOutlineResponse & { cache_hit?: boolean; generated?: boolean; message?: string }>(
      "/search/carousel/pipeline/generate",
      {
        method: "POST",
        body: JSON.stringify({ ...body, select_images: Boolean(body.select_images) }),
        timeoutMs: body.force || body.generate ? 900_000 : 90_000,
      }
    ),
  carouselFeedbackList: (driveFileId: string, targetKind?: "theme" | "hook") => {
    const qs = new URLSearchParams({ drive_file_id: driveFileId });
    if (targetKind) qs.set("target_kind", targetKind);
    return api<{ drive_file_id: string; items: CarouselItemFeedback[] }>(
      `/search/carousel/pipeline/feedback?${qs}`
    );
  },
  carouselFeedbackUpsert: (body: {
    drive_file_id: string;
    target_kind: "theme" | "hook";
    target_key: string;
    target_label?: string;
    rating?: "up" | "down" | null;
    comment?: string;
  }) =>
    api<{ ok: boolean; item: CarouselItemFeedback }>("/search/carousel/pipeline/feedback", {
      method: "PUT",
      body: JSON.stringify(body),
      timeoutMs: 30_000,
    }),
  carouselReferencesList: (driveFileId: string, targetKind?: "theme" | "hook") => {
    const qs = new URLSearchParams({ drive_file_id: driveFileId });
    if (targetKind) qs.set("target_kind", targetKind);
    return api<{ drive_file_id: string; items: CarouselItemReference[] }>(
      `/search/carousel/pipeline/references?${qs}`
    );
  },
  carouselReferenceCreate: (body: {
    drive_file_id: string;
    target_kind: "theme" | "hook";
    target_key: string;
    target_label?: string;
    ref_kind: "image" | "copy";
    image_url?: string;
    frame_ts?: number | null;
    copy_text?: string;
    note?: string;
  }) =>
    api<{ ok: boolean; item: CarouselItemReference }>("/search/carousel/pipeline/references", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 30_000,
    }),
  carouselReferenceDelete: (refId: number) =>
    api<{ ok: boolean; id: number }>(`/search/carousel/pipeline/references/${refId}`, {
      method: "DELETE",
      timeoutMs: 30_000,
    }),
  carouselReferenceUploadImage: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120_000);
    try {
      const res = await fetch(`${API_BASE}/search/carousel/pipeline/references/upload-image`, {
        method: "POST",
        body: form,
        signal: controller.signal,
        cache: "no-store",
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      return (await res.json()) as {
        ok: boolean;
        url: string;
        name: string;
        size: number;
      };
    } catch (error) {
      throwApiError(error, "Image upload failed");
    } finally {
      clearTimeout(timer);
    }
  },
  carouselPipelinePrerun: (body?: { drive_file_ids?: string[]; force?: boolean }) =>
    api<{
      count: number;
      ok_count: number;
      force: boolean;
      items: {
        drive_file_id: string;
        ok: boolean;
        themes_cache_hit?: boolean;
        themes_generated?: boolean;
        theme_count?: number;
        extract_cache_hit?: boolean;
        extract_generated?: boolean;
        hook_count?: number;
        topic_count?: number;
        error?: string;
      }[];
    }>("/search/carousel/pipeline/prerun", {
      method: "POST",
      body: JSON.stringify({
        drive_file_ids: body?.drive_file_ids ?? [],
        force: Boolean(body?.force),
      }),
      timeoutMs: 900_000,
    }),
  carouselUploadVideo: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 900_000);
    try {
      const res = await fetch(`${API_BASE}/search/carousel/upload`, {
        method: "POST",
        body: form,
        signal: controller.signal,
        cache: "no-store",
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      return (await res.json()) as {
        drive_file_id: string;
        name: string;
        status: string;
        size?: number;
        queued: boolean;
        message: string;
      };
    } catch (error) {
      throwApiError(error, "Upload failed");
    } finally {
      clearTimeout(timer);
    }
  },
  carouselPipelineSelectImages: (body: {
    drive_file_id: string;
    carousels: CarouselGeneratedItem[];
    llm_provider?: string;
    llm_model?: string;
  }) =>
    api<CarouselOutlineResponse>("/search/carousel/pipeline/select-images", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 900_000,
    }),
  carouselQualityCheck: (body: {
    drive_file_id: string;
    carousels: CarouselGeneratedItem[];
  }) =>
    api<{
      drive_file_id: string;
      carousels: CarouselGeneratedItem[];
      quality_summary?: CarouselQualitySummary | null;
      transcript_guard?: Record<string, number>;
      status?: "current" | string;
    }>("/search/carousel/pipeline/quality-check", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 45_000,
    }),
  carouselCached: (driveFileId: string) =>
    api<{
      id: number;
      status: string;
      layout_mode: "single_1" | "split_2" | string;
      copy_version: number;
      slides: CarouselOutlineSlide[];
      carousels?: CarouselGeneratedItem[];
      layouts?: CarouselLayouts | null;
      title?: string;
      theme?: Record<string, unknown>;
      references?: Record<string, unknown>[];
    }>(`/search/carousel/pipeline/carousel?drive_file_id=${encodeURIComponent(driveFileId)}`),
  carouselPipelineStatus: (driveFileId: string) =>
    api<{
      drive_file_id: string;
      status: string;
      locked: boolean;
      ready_artifact_id?: number | null;
    }>(
      `/search/carousel/pipeline/status?drive_file_id=${encodeURIComponent(driveFileId)}`
    ),
  carouselCopySave: (body: {
    drive_file_id: string;
    save_id?: number;
    layout_mode?: "single_1" | "split_2";
    slides?: CarouselOutlineSlide[];
    theme?: Record<string, unknown>;
    references?: Record<string, unknown>[];
  }) =>
    api<{ id: number; copy_version: number; layout_mode: string }>(
      "/search/carousel/pipeline/carousel/copy",
      { method: "POST", body: JSON.stringify(body) }
    ),
  carouselRegenerateSlide: (body: {
    drive_file_id: string;
    save_id?: number;
    carousel_id?: string;
    slide_index: number;
    slide?: CarouselOutlineSlide;
  }) =>
    api<{ id: number; copy_version: number; slide: CarouselOutlineSlide }>(
      "/search/carousel/pipeline/carousel/slide/regenerate",
      { method: "POST", body: JSON.stringify(body) }
    ),
  carouselPipelineSaves: (
    driveFileId: string,
    limit = 20,
    kind: "topics_hooks" | "themes" | "carousel" = "topics_hooks"
  ) =>
    api<{ items: CarouselGenerationSaveListItem[] }>(
      `/search/carousel/pipeline/saves?drive_file_id=${encodeURIComponent(driveFileId)}&limit=${limit}&kind=${encodeURIComponent(kind)}`
    ),
  carouselPipelineSaveGet: (saveId: number) =>
    api<{
      id: number;
      drive_file_id: string;
      kind?: string;
      theme_key: string;
      label?: string | null;
      created_at?: string | null;
      source?: string | null;
      model?: string | null;
      transcript_hash?: string | null;
      payload: CarouselPipelineExtractResponse & {
        carousels?: CarouselGeneratedItem[];
        slides?: CarouselOutlineSlide[];
        title?: string;
        selected_hooks?: string[];
        selected_topics?: string[];
        themes?: CarouselPipelineTheme[];
        source?: string;
        cue_count?: number;
      };
    }>(`/search/carousel/pipeline/saves/${saveId}`),
  carouselPipelineSaveCreate: (body: {
    drive_file_id: string;
    theme_key?: string;
    label?: string;
    topic_tree?: CarouselTopicTreeNode[];
    hooks?: CarouselVerbatimItem[];
    topics?: CarouselVerbatimItem[];
    selected_hooks?: string[];
    selected_topics?: string[];
    intent?: string | null;
    intent_score?: number | null;
    themes?: CarouselPipelineTheme[];
  }) =>
    api<{ id: number; created_at?: string | null }>("/search/carousel/pipeline/saves", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  carouselPipelineShuffle: (body: {
    topic_tree?: CarouselTopicTreeNode[];
    hooks?: CarouselVerbatimItem[];
    topics?: CarouselVerbatimItem[];
    count_hooks?: number;
    count_topics?: number;
  }) =>
    api<{
      selected_hooks: string[];
      selected_topics: string[];
      hooks: CarouselVerbatimItem[];
      topics: CarouselVerbatimItem[];
    }>("/search/carousel/pipeline/shuffle", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  carouselTranscriptFrames: (opts: {
    driveFileId: string;
    startSec?: number;
    endSec?: number | null;
    limit?: number;
    timeoutMs?: number;
    silent?: boolean;
  }) => {
    const params = new URLSearchParams({
      drive_file_id: opts.driveFileId,
      start_sec: String(opts.startSec ?? 0),
      limit: String(opts.limit ?? 40),
    });
    if (opts.endSec != null) params.set("end_sec", String(opts.endSec));
    return api<{ drive_file_id: string; items: CarouselTranscriptFrameItem[] }>(
      `/search/carousel/pipeline/transcript-frames?${params}`,
      {
        timeoutMs: opts.timeoutMs ?? 180_000,
        silent: opts.silent,
      }
    );
  },
};
