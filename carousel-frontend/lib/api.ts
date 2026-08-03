/**
 * Carousel Studio API client — talks to the same backend as the main app.
 * Base URL: NEXT_PUBLIC_API_URL.
 * Prefer `/backend` (Next rewrite → :8000) so the browser stays same-origin
 * and avoids CORS when the studio runs on :3002.
 */

const API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "/backend").replace(/\/+$/, "");

export { API_BASE };

export const SERVICE_UNAVAILABLE_MESSAGE =
  "Can't reach the API right now. It may be starting up or temporarily unavailable.";

export function formatApiError(
  error: unknown,
  fallback = "Something went wrong. Please try again."
): string {
  if (!(error instanceof Error)) return fallback;
  const raw = error.message.trim();
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
      const parsed = JSON.parse(raw) as { detail?: string };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) {
        return parsed.detail.trim();
      }
    } catch {
      // fall through
    }
  }

  if (
    /localhost:\d+/i.test(raw) ||
    /127\.0\.0\.1/i.test(raw) ||
    /:\d{4,5}\/?/i.test(raw) ||
    /internal server error/i.test(raw) ||
    /<html/i.test(raw)
  ) {
    return SERVICE_UNAVAILABLE_MESSAGE;
  }

  return raw.length > 240 ? `${raw.slice(0, 240)}…` : raw;
}

export function isServiceUnavailableMessage(message: string): boolean {
  return message === SERVICE_UNAVAILABLE_MESSAGE;
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
  carousels?: CarouselGeneratedItem[];
  carousel_count?: number;
  images_ready?: boolean;
  intent?: string | null;
  layouts?: CarouselLayouts | null;
  quality?: {
    candidates?: number;
    kept?: number;
    rejected?: Record<string, number>;
    slides_polished?: number;
  };
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
};

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

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text();
      if (res.status >= 500) {
        throw new Error(SERVICE_UNAVAILABLE_MESSAGE);
      }
      throw new Error(formatApiError(new Error(text || res.statusText)));
    }
    if (res.status === 204) return undefined as T;
    return res.json();
  } catch (error) {
    if (
      (error instanceof DOMException || error instanceof Error) &&
      error.name === "AbortError"
    ) {
      throw error;
    }
    throw new Error(formatApiError(error));
  }
}

export const driveFileDownloadUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/download`;

export const driveVideoStreamUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/preview`;

export const apiAssetUrl = (path: string) =>
  path.startsWith("http") ? path : `${API_BASE}${path}`;

export const cacheOnlyAssetUrl = (path: string) => {
  const asset = apiAssetUrl(path);
  if (!asset.includes("/media/video/") || !asset.includes("/frame?")) return asset;
  return asset.includes("cache_only=") ? asset : `${asset}&cache_only=1`;
};

export const apiClient = {
  persons: () => api<Person[]>("/persons"),
  carouselRecentVideos: (limit = 5, captionedOnly = true) =>
    api<{ items: CarouselRecentVideo[]; captioned_only?: boolean }>(
      `/search/carousel/recent-videos?limit=${limit}&captioned_only=${captionedOnly ? "true" : "false"}`
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
      }),
      signal: opts?.signal,
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
  }) =>
    api<CarouselPipelineExtractResponse>("/search/carousel/pipeline/extract", {
      method: "POST",
      body: JSON.stringify(body),
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
      }
    ),
  carouselPipelineGenerate: (body: CarouselGenerateRequest) =>
    api<CarouselOutlineResponse>("/search/carousel/pipeline/generate", {
      method: "POST",
      body: JSON.stringify({ ...body, select_images: Boolean(body.select_images) }),
    }),
  carouselPipelineSelectImages: (body: {
    drive_file_id: string;
    carousels: CarouselGeneratedItem[];
  }) =>
    api<CarouselOutlineResponse>("/search/carousel/pipeline/select-images", {
      method: "POST",
      body: JSON.stringify(body),
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
  }) => {
    const params = new URLSearchParams({
      drive_file_id: opts.driveFileId,
      start_sec: String(opts.startSec ?? 0),
      limit: String(opts.limit ?? 40),
    });
    if (opts.endSec != null) params.set("end_sec", String(opts.endSec));
    return api<{ drive_file_id: string; items: CarouselTranscriptFrameItem[] }>(
      `/search/carousel/pipeline/transcript-frames?${params}`
    );
  },
};
