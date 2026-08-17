/**
 * Where the backend lives, resolved once from the page's own origin.
 *
 * A single build-time URL cannot be right for every visitor: loopback is the
 * fast path but only works on the host machine, while an HTTPS page (Cloudflare
 * tunnel) may not call an http:// backend at all — the browser blocks it as
 * mixed content. So decide in the browser, where the origin is known:
 *
 *   localhost / 127.0.0.1  → loopback; local use never leaves the machine
 *   LAN IP                 → same host, API port
 *   tunnel / other origin  → the configured public URL, protocol-matched
 */
import { sanitizeUserFacingError } from "./index-errors";
import { toastApiError } from "./toast-api-error";

const LOOPBACK_API = (process.env.NEXT_PUBLIC_API_URL_LOCAL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const PUBLIC_API = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");
const API_PORT = process.env.NEXT_PUBLIC_API_PORT ?? "8000";

const isLoopbackUrl = (url: string) =>
  /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/.test(url);

function resolveApiBase(): string {
  // Server-side rendering happens on the host, so loopback is always correct.
  if (typeof window === "undefined") return LOOPBACK_API || PUBLIC_API;

  const { hostname, protocol } = window.location;
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]") {
    return LOOPBACK_API;
  }

  // A plain-http page means we were served directly off this machine (localhost
  // handled above, so: LAN address). The backend is reachable on the same host,
  // which beats sending LAN traffic out to the tunnel and back.
  if (protocol === "http:") {
    return `http://${hostname}:${API_PORT}`;
  }

  // https page — the tunnel. Only an https backend is usable here; an http one
  // would be blocked as mixed content.
  if (PUBLIC_API.startsWith("https://") && !isLoopbackUrl(PUBLIC_API)) {
    return PUBLIC_API;
  }

  return `https://${hostname}:${API_PORT}`;
}

export const API_BASE = resolveApiBase();

export const SERVICE_UNAVAILABLE_MESSAGE =
  "Can't reach the DFI service right now. It may be starting up or temporarily unavailable.";

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

  if (
    /localhost:\d+/i.test(raw) ||
    /127\.0\.0\.1/i.test(raw) ||
    /internal server error/i.test(raw) ||
    /<html/i.test(raw)
  ) {
    return SERVICE_UNAVAILABLE_MESSAGE;
  }

  return sanitizeUserFacingError(raw, fallback);
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

export type Cluster = {
  id: number;
  status: string;
  member_count: number;
  representative_face_id: number | null;
  representative_confidence: number | null;
  appears_in: {
    media_id: number;
    drive_file_id: string;
    name: string;
    path: string;
    media_type: string;
    frame_timestamp?: number | null;
  }[];
  created_at: string;
};

export type ClusterListResponse = {
  items: Cluster[];
  total: number;
  offset: number;
  limit: number;
};

export type DriveFile = {
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

export type DriveFilesPage = {
  total: number;
  offset: number;
  limit: number;
  items: DriveFile[];
};

export type DriveFilesQuery = {
  status?: string;
  source?: string;
  limit?: number;
  offset?: number;
};

export type SkipStats = {
  total_skipped: number;
  by_reason: { reason: string; count: number }[];
};

export type IndexedFolder = {
  id: string;
  name: string;
  drive_url: string;
  drive_user_email?: string | null;
  is_active: boolean;
  first_indexed_at?: string | null;
  last_indexed_at?: string | null;
  last_file_count?: number | null;
};

export type FileIndexConflict = {
  id: number;
  incoming_file_id: string;
  existing_file_id: string;
  conflict_kind: string;
  status: string;
  incoming_name: string;
  existing_name: string;
  content_hash?: string | null;
  message?: string | null;
  created_at?: string | null;
  resolved_at?: string | null;
  can_replace?: boolean;
  can_merge?: boolean;
  can_skip?: boolean;
};

export type RetrySkippedByReasonResult = {
  ok: boolean;
  reason: string;
  requeued: number;
  ineligible: number;
  action: "requeue" | "resume_paused" | "unsupported" | string;
  message: string;
  folders_resumed?: number;
};

export type IndexErrorsPage = {
  total: number;
  offset: number;
  limit: number;
  items: DriveFile[];
};

export type HowToResponse = {
  source: string;
  page: string;
  answer: string;
  warning?: string;
};

export type FaceSearchAppearance = {
  media_id: number;
  drive_file_id: string;
  name: string;
  path: string;
  media_type: string;
  frame_timestamp?: number | null;
};

export type FaceSearchMatch = {
  face_id: number;
  person_id: number | null;
  person_name: string;
  score: number;
  distance: number;
  linkedin_url: string | null;
  cluster_id?: number | null;
  cluster_status?: string | null;
  cluster_member_count?: number | null;
  appears_in?: FaceSearchAppearance[];
};

export type FaceSearchResponse = {
  faces_detected: number;
  query_confidence?: number;
  matches: FaceSearchMatch[];
  message?: string;
};

export type FaceCrawlResult = {
  url: string;
  ok: boolean;
  error?: string;
  search?: FaceSearchResponse;
};

export type FaceCrawlResponse = {
  crawled: number;
  results: FaceCrawlResult[];
};

export type LeadershipPerson = {
  name: string;
  role: string;
  image_url: string;
  linkedin_url?: string;
};

export type LeadershipRoster = {
  source_url: string;
  tab: string;
  section_id: string;
  label: string;
  count: number;
  people: LeadershipPerson[];
};

export type LeadershipScanResult = {
  name: string;
  role: string;
  image_url: string;
  linkedin_url?: string | null;
  ok: boolean;
  error?: string;
  faces_detected?: number;
  internal_matches?: FaceSearchMatch[];
  matched_person?: string;
  match_score?: number;
  name_alignment?: boolean;
};

export type LeadershipScanResponse = {
  source_url: string;
  tab: string;
  section_id: string;
  label: string;
  count: number;
  matched: number;
  results: LeadershipScanResult[];
};

export type LeadershipNameTagResponse = {
  ok: boolean;
  named: string;
  person: {
    id: number;
    name: string;
    role?: string | null;
    representative_face_id?: number | null;
    occurrence_count: number;
  };
  actions: {
    type: string;
    id: number;
    ok: boolean;
    action?: string;
    error?: string;
  }[];
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

export type IndexLaneSlots = {
  active: number;
  max: number;
};

export type IndexStatus = {
  is_running: boolean;
  counts_by_status: Record<string, number>;
  last_run: {
    discovered: number;
    processed: number;
    skipped: number;
    errored: number;
    deferred: number;
  } | null;
  last_run_at: string | null;
  current_file: string | null;
  current_files?: string[];
  current_image_files?: string[];
  current_video_files?: string[];
  image_slots?: IndexLaneSlots;
  video_slots?: IndexLaneSlots;
  active_image_jobs?: number;
  active_video_jobs?: number;
  auto_index_enabled: boolean;
  auto_index_interval_seconds: number;
  pending_count?: number;
  go_indexer_enabled?: boolean;
  go_indexer_alive?: boolean;
  go_files_per_sec?: number | null;
};

export type GoIndexerStatus = {
  enabled: boolean;
  alive: boolean;
  last_heartbeat_at: string | null;
  max_parallel: number;
  canary_limit: number;
  claimed_open: number;
  last_files_ok: number;
  last_files_err: number;
  last_elapsed_ms: number;
  last_files_per_sec: number;
  last_download_bytes: number;
  last_reported_at: string | null;
};

export type Settings = {
  gemini_model: string;
  gemini_file_search_store_display_name: string;
  auto_index_enabled: boolean;
  auto_index_interval_seconds: number;
  reindex_errored_files: boolean;
  reindex_skipped_files: boolean;
  follow_shortcut_folders: boolean;
  experimental_manual_face_tag: boolean;
  gemini_file_search_search_enabled: boolean;
  search_parallel_variants_enabled: boolean;
  search_use_captions: boolean;
  search_rerank_enabled: boolean;
  go_indexer_enabled: boolean;
};

export type FileFace = {
  id: number;
  media_id: number;
  bbox_x: number;
  bbox_y: number;
  bbox_width: number;
  bbox_height: number;
  detection_confidence: number;
  cluster_id: number | null;
  person_id: number | null;
  person_name?: string | null;
  has_thumbnail: boolean;
};

export type SearchCitation = {
  file_name?: string;
  source?: string;
  drive_file_id?: string;
  drive_path?: string;
  metadata?: Record<string, string>;
};

export type SearchResultFile = {
  drive_file_id: string;
  name: string;
  path: string;
  mime_type: string;
  person_names: string[];
  score?: number | null;
  caption?: string | null;
};

export type SearchMoment = {
  drive_file_id: string;
  name: string;
  path: string;
  mime_type: string;
  timestamp_sec: number;
  end_timestamp_sec?: number | null;
  match_type: string;
  fennec_scene_id?: number | null;
  preview_url: string;
  video_url?: string | null;
  person_names: string[];
  snippet?: string | null;
  score?: number | null;
};

export type SearchResponse = {
  query: string;
  answer: string;
  citations: SearchCitation[];
  files: SearchResultFile[];
  moments: SearchMoment[];
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

export type CarouselScriptTurn = {
  role: string;
  content: string;
};

export type CarouselScriptRequest = {
  prompt: string;
  hooks: string[];
  topics: string[];
  snapshot?: CarouselSnapshotContext | null;
  history?: CarouselScriptTurn[];
};

export type CarouselScriptResponse = {
  source: string;
  script: string;
  hooks: string[];
  topics: string[];
  warning?: string;
};

export type CarouselExpandResponse = {
  source: string;
  kind: string;
  items: CarouselPresetItem[];
  warning?: string;
};

export type CarouselOutlineSlide = {
  index: number;
  hook_line: string;
  /** Exact transcript line for editing before image selection. */
  transcript_text?: string | null;
  /** Source-language line when display text was translated to English. */
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
  /** Chosen display frame timestamp inside the spoken span (may differ from mid-span). */
  frame_ts?: number | null;
  /** How the preview frame was chosen. */
  frame_source?: "ai" | "heuristic" | "fallback" | "deferred" | "manual" | string | null;
  instagram_ready?: boolean | null;
  images_ready?: boolean | null;
  frames_prewarmed?: boolean | null;
  frame_candidates?: number[] | null;
  frame_quality?: Record<string, unknown> | null;
  /** Dominant-face center as 0..1 fractions, used to keep the speaker centred when cropping. */
  focal_x?: number | null;
  focal_y?: number | null;
  front_face_score?: number | null;
  /** Rendered panels for the active layout: 1 entry for single_1, 2 for split_2. */
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

export type CarouselOutlineRequest = {
  script: string;
  moments: CarouselSnapshotContext[];
  hooks?: string[];
  topics?: string[];
  slide_count?: number;
  title?: string;
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
  /** Multi-carousel generate: one entry per topic + mixed narratives. */
  carousels?: CarouselGeneratedItem[];
  carousel_count?: number;
  images_ready?: boolean;
  intent?: string | null;
  /** Cached dual layouts so toggling single/split never regenerates. */
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
  /** Spoken seed that produced a crafted hook — used for slide relevance. */
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
  /** When false (default), return transcript slides only — no Gemini frame calls. */
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

export type CarouselCuesRequest = {
  hooks?: string[];
  topics?: string[];
  moments?: CarouselSnapshotContext[];
  drive_file_id?: string;
};

export type CarouselCuesResponse = {
  source: string;
  hooks: string[];
  topics: string[];
  cues: CarouselCueItem[];
};

export type CarouselTranscriptSubtopic = {
  title: string;
  start_sec: number;
  end_sec?: number | null;
  explanation: string;
};

export type CarouselTranscriptTopic = {
  title: string;
  start_sec: number;
  end_sec?: number | null;
  explanation: string;
  subtopics: CarouselTranscriptSubtopic[];
};

export type CarouselTranscriptTopicsRequest = {
  drive_file_id: string;
};

export type CarouselTranscriptTopicsResponse = {
  source: string;
  drive_file_id: string;
  name: string;
  cue_count: number;
  topics: CarouselTranscriptTopic[];
  warning?: string;
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
  /** Non-contiguous spans when a thread recurs across the talk. */
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
  /** False when the frame still has to be extracted from the video on first view. */
  cached?: boolean;
};

export type FolderContext = {
  id: number;
  folder_path: string;
  description: string;
};

export type DriveSession = {
  connected: boolean;
  email?: string;
  selected_folder?: { id: string; name: string } | null;
};

export type LibraryFile = {
  id: string;
  name: string;
  path: string;
  folder_path: string;
  mime_type: string;
  status: string;
  size: number | null;
  source: string;
  is_image: boolean;
  is_video: boolean;
  has_caption: boolean;
  has_embedding: boolean;
  caption_preview: string | null;
  error_message: string | null;
};

export type LibraryFolder = {
  name: string;
  path: string;
  file_count: number;
  image_count: number;
  captioned_count: number;
  embedded_count: number;
  pending_count: number;
  error_count: number;
  skipped_count: number;
  archived_count: number;
  indexing_paused: boolean;
  folders: LibraryFolder[];
  files: LibraryFile[];
};

export type LibraryMaintenance = {
  caption_backfill_running: boolean;
  embed_backfill_running: boolean;
  last_caption_run_at: string | null;
  last_embed_run_at: string | null;
  last_caption_indexed: number;
  last_embed_indexed: number;
};

export type LibraryResponse = {
  tree: LibraryFolder;
  summary: {
    total_files: number;
    images: number;
    videos: number;
    captioned: number;
    embedded: number;
    pending: number;
    errors: number;
    skipped: number;
    archived?: number;
    /** Processed images still missing a caption (not pending images). */
    missing_captions?: number;
    /** pending + errors + missing_captions — excludes skipped / no double-count. */
    needs_work?: number;
    caption_pct: number;
  };
  maintenance: LibraryMaintenance;
  paused_folders: string[];
};

export type CaptionStats = {
  processed_images: number;
  visual_embeddings: number;
  captioned: number;
  remaining: number;
  pct_captioned: number;
  missing_captions: number;
  missing_embeddings: number;
  maintenance: LibraryMaintenance;
};

export type DriveTokenResponse = {
  accessToken: string;
  apiKey: string;
  appId?: string | null;
};

async function api<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number; silent?: boolean }
): Promise<T> {
  const { timeoutMs, silent, signal: external, ...rest } = init ?? {};
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let signal = external;
  if (typeof timeoutMs === "number" && timeoutMs > 0) {
    controller = new AbortController();
    if (external) {
      if (external.aborted) controller.abort();
      else external.addEventListener("abort", () => controller!.abort(), { once: true });
    }
    signal = controller.signal;
    timer = setTimeout(() => controller!.abort(), timeoutMs);
  }
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal,
      headers: { "Content-Type": "application/json", ...rest.headers },
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
    // Preserve abort so callers can ignore cancelled requests.
    if (
      (error instanceof DOMException || error instanceof Error) &&
      error.name === "AbortError"
    ) {
      if (typeof timeoutMs === "number" && timeoutMs > 0 && !external?.aborted) {
        const msg = `Request timed out after ${Math.round(timeoutMs / 1000)}s. The API may be busy — retry in a moment.`;
        if (!silent) toastApiError(msg);
        throw new Error(msg);
      }
      throw error;
    }
    const msg = formatApiError(error);
    if (!silent) toastApiError(msg);
    throw new Error(msg);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export const faceThumbnailUrl = (faceId: number | null) =>
  faceId ? `${API_BASE}/faces/${faceId}/thumbnail` : null;

export const driveGoogleViewUrl = (driveFileId: string) =>
  `https://drive.google.com/file/d/${driveFileId}/view`;

/** Image/PDF previews served by Google Drive (not proxied through Railway). */
export const driveFilePreviewUrl = (driveFileId: string, mimeType?: string) => {
  if (mimeType?.startsWith("image/")) {
    return `https://drive.google.com/thumbnail?id=${encodeURIComponent(driveFileId)}&sz=w1200`;
  }
  if (mimeType === "application/pdf") {
    return `https://drive.google.com/file/d/${driveFileId}/preview`;
  }
  return driveGoogleViewUrl(driveFileId);
};

export const driveFileDownloadUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/download`;

/** Stream indexed video/audio for in-app playback (seek via HTML5 video element). */
export const driveVideoStreamUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/preview`;

export const apiAssetUrl = (path: string) =>
  path.startsWith("http") ? path : `${API_BASE}${path}`;

/** Ready carousel previews must never fall back to on-demand video extraction. */
export const cacheOnlyAssetUrl = (path: string) => {
  const asset = apiAssetUrl(path);
  if (!asset.includes("/media/video/") || !asset.includes("/frame?")) return asset;
  return asset.includes("cache_only=") ? asset : `${asset}&cache_only=1`;
};

export type ReidStatus = {
  body_signatures: { total: number; labeled: number; unlabeled: number; full_body: number };
  web_matches: { total: number; with_linkedin: number };
  reverse_search_configured: boolean;
  apify_google_lens_configured?: boolean;
  person_detector?: string;
  yolov8_available?: boolean;
};

export type ReidCandidate = {
  face_id: number;
  matched_face_id: number;
  person_id: number | null;
  person_name: string | null;
  body_similarity: number;
  face_similarity?: number;
  same_folder: boolean;
  combined_score: number;
  source_path: string | null;
  matched_path: string | null;
  is_full_body: boolean;
  gated_by_face?: boolean;
};

export type ReidGalleryItem = {
  signature_id: number;
  face_id: number;
  person_id: number | null;
  person_name: string | null;
  drive_file_id: string;
  file_name: string;
  file_path: string;
  mime_type: string;
  prominence_pct: number;
  body_coverage_pct: number;
  is_full_body: boolean;
  has_body_crop: boolean;
  has_face_thumb: boolean;
  has_proof?: boolean;
  proof_url?: string | null;
  candidate: ReidCandidate | null;
};

export type ReidProveResult = {
  media_id: number;
  drive_file_id: string;
  file_name: string;
  image_size: { width: number; height: number };
  detector: string;
  yolov8_available: boolean;
  persons_detected: number;
  faces_on_media: number;
  faces_linked_to_person_box: number;
  embedded: number;
  proof_url: string;
  persons: { x: number; y: number; w: number; h: number; confidence: number; backend: string }[];
  links: { face_id: number; has_crop: boolean; embedded: boolean }[];
};

export type ReidBackfillStats = {
  scanned: number;
  embedded: number;
  no_person_box?: number;
  not_full_body: number;
  errors: number;
  relinked: number;
  detector?: string;
};

export type OfficialImageSearchStatus = {
  configured: boolean;
  key_source: string | null;
  api: string;
  endpoint: string;
  feature: string;
  scope_required_if_using_oauth: string;
  api_key_scopes: string;
  enable_url: string;
};

export type OfficialImageSearchResult = {
  provider: string;
  key_source: string;
  face_id?: number;
  best_guess_labels: string[];
  web_entities: { description?: string | null; entity_id?: string | null; score?: number | null }[];
  full_matching_images: { url: string; score?: number | null }[];
  partial_matching_images: { url: string; score?: number | null }[];
  visually_similar_images: { url: string; score?: number | null }[];
  pages_with_matching_images: {
    url: string;
    page_title?: string | null;
    score?: number | null;
    full_matching_images: { url: string; score?: number | null }[];
    partial_matching_images: { url: string; score?: number | null }[];
  }[];
};

export type FaceReverseSearchResult = {
  face_id: number;
  provider: string;
  image_url: string;
  google_guess: string | null;
  result_count: number;
  linkedin_url: string | null;
  match_thumbnails?: Record<string, string>;
  matches: {
    title: string | null;
    url: string | null;
    linkedin_url: string | null;
    score: number | null;
    thumbnail?: string | null;
  }[];
};

export const apiClient = {
  health: () =>
    api<{ status: string; search?: string; fennec_enabled?: boolean; fennec_ready?: boolean }>("/health"),
  persons: () => api<Person[]>("/persons"),
  searchPersons: (q: string, limit = 20) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    return api<Person[]>(`/persons/search?${params}`);
  },
  person: (id: number) => api<Person>(`/persons/${id}`),
  renamePerson: (id: number, name: string) =>
    api<Person>(`/persons/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  updatePerson: (id: number, body: { name?: string; role?: PersonRole }) =>
    api<Person>(`/persons/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deletePerson: (id: number) => api<void>(`/persons/${id}`, { method: "DELETE" }),
  personMedia: (id: number) =>
    api<
      {
        media_id: number;
        drive_file_id: string;
        name: string;
        path: string;
        media_type: string;
        frame_timestamp?: number | null;
      }[]
    >(`/persons/${id}/media`),
  clusters: (opts?: { includeIgnored?: boolean; limit?: number; offset?: number }) => {
    const params = new URLSearchParams();
    if (opts?.includeIgnored) params.set("include_ignored", "true");
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.offset != null) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return api<ClusterListResponse>(`/clusters${qs ? `?${qs}` : ""}`);
  },
  nameCluster: (id: number, name: string) =>
    api<Person>(`/clusters/${id}/name`, { method: "POST", body: JSON.stringify({ name }) }),
  ignoreCluster: (id: number) => api<void>(`/clusters/${id}/ignore`, { method: "POST" }),
  mergeCluster: (id: number, personId: number) =>
    api<Person>(`/clusters/${id}/merge`, { method: "POST", body: JSON.stringify({ person_id: personId }) }),
  syncDriveFiles: () => api<{ ok: boolean; scheduled: boolean }>("/drive/sync", { method: "POST" }),
  driveFiles: (statusOrOpts?: string | DriveFilesQuery) => {
    const opts: DriveFilesQuery =
      typeof statusOrOpts === "string" ? { status: statusOrOpts } : statusOrOpts ?? {};
    const params = new URLSearchParams();
    if (opts.status) params.set("status", opts.status);
    if (opts.source) params.set("source", opts.source);
    if (opts.limit != null) params.set("limit", String(opts.limit));
    if (opts.offset != null) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return api<DriveFile[]>(`/drive/files${qs ? `?${qs}` : ""}`);
  },
  driveFilesPage: (opts?: DriveFilesQuery) => {
    const params = new URLSearchParams();
    if (opts?.status) params.set("status", opts.status);
    if (opts?.source) params.set("source", opts.source);
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.offset != null) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return api<DriveFilesPage>(`/drive/files/page${qs ? `?${qs}` : ""}`);
  },
  driveLibrary: () => api<LibraryResponse>("/drive/library"),
  pauseFolderIndexing: (folder_path: string) =>
    api<{ ok: boolean; stopped: number; cancelled: number }>("/drive/library/folders/pause", {
      method: "POST",
      body: JSON.stringify({ folder_path }),
    }),
  resumeFolderIndexing: (folder_path: string) =>
    api<{ ok: boolean; resumed: number }>("/drive/library/folders/resume", {
      method: "POST",
      body: JSON.stringify({ folder_path }),
    }),
  skipCorruptFiles: () =>
    api<{ ok: boolean; skipped: number }>("/drive/skip-corrupt", { method: "POST" }),
  captionStats: () => api<CaptionStats>("/index/captions", { silent: true }),
  backfillCaptions: () => api<{ ok: boolean; scheduled: boolean }>("/backfill/image-captions", { method: "POST" }),
  retryDriveFile: (id: string) => api<DriveFile>(`/drive/files/${id}/retry`, { method: "POST" }),
  removeDriveFile: (id: string) => api<void>(`/drive/files/${id}`, { method: "DELETE" }),
  youtubeVideos: () => api<DriveFile[]>("/youtube/videos"),
  addYoutubeVideos: (urls: string[], indexNow = true, downloadLocal = true) =>
    api<YoutubeRegisterResponse>("/youtube/videos", {
      method: "POST",
      body: JSON.stringify({ urls, index_now: indexNow, download_local: downloadLocal }),
    }),
  indexStatus: () => api<IndexStatus>("/index", { silent: true }),
  goIndexerStatus: () => api<GoIndexerStatus>("/index/go/status", { silent: true }),
  skipStats: () => api<SkipStats>("/index/skip-stats", { silent: true }),
  indexedFolders: () =>
    api<{ folders: IndexedFolder[]; total: number }>("/index/folders"),
  indexConflicts: (status: string | null = "pending", limit = 50, offset = 0) => {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (status != null) params.set("status", status);
    return api<{
      total: number;
      offset: number;
      limit: number;
      items: FileIndexConflict[];
    }>(`/index/conflicts?${params}`);
  },
  resolveIndexConflict: (id: number, action: "skip" | "replace" | "merge") =>
    api<{ ok: boolean; conflict: FileIndexConflict }>(`/index/conflicts/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  retrySkippedByReason: (reason: string) =>
    api<RetrySkippedByReasonResult>("/index/skipped/retry", {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  indexErrors: (limit = 50, offset = 0) => {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    return api<IndexErrorsPage>(`/index/errors?${params}`);
  },
  triggerIndex: () => api<IndexStatus>("/index", { method: "POST" }),
  triggerReindex: () => api<IndexStatus>("/reindex", { method: "POST" }),
  howto: (page: string, question: string) =>
    api<HowToResponse>("/help/howto", {
      method: "POST",
      body: JSON.stringify({ page, question }),
    }),
  carouselPresets: () => api<CarouselPresets>("/search/carousel/presets"),
  expandCarouselPresets: (kind: "hooks" | "topics", seed = "", count = 4) =>
    api<CarouselExpandResponse>("/search/carousel/presets/expand", {
      method: "POST",
      body: JSON.stringify({ kind, seed, count }),
    }),
  generateCarouselScript: (body: CarouselScriptRequest) =>
    api<CarouselScriptResponse>("/search/carousel/script", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  generateCarouselOutline: (body: CarouselOutlineRequest) =>
    api<CarouselOutlineResponse>("/search/carousel/outline", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  matchCarouselCues: (body: CarouselCuesRequest) =>
    api<CarouselCuesResponse>("/search/carousel/cues", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  analyzeCarouselTranscriptTopics: (driveFileId: string) =>
    api<CarouselTranscriptTopicsResponse>("/search/carousel/transcript-topics", {
      method: "POST",
      body: JSON.stringify({ drive_file_id: driveFileId }),
    }),
  carouselRecentVideos: (limit = 5, captionedOnly = true) =>
    api<{ items: CarouselRecentVideo[]; captioned_only?: boolean }>(
      `/search/carousel/recent-videos?limit=${limit}&captioned_only=${captionedOnly ? "true" : "false"}`,
      { timeoutMs: 15_000 }
    ),
  carouselVideos: (opts?: { q?: string; limit?: number; offset?: number; captionedOnly?: boolean }) => {
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
  searchUploadedFace: async (file: File, limit = 20): Promise<FaceSearchResponse> => {
    const params = new URLSearchParams({ limit: String(limit) });
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`${API_BASE}/reid/faces/search?${params}`, {
        method: "POST",
        body: form,
        cache: "no-store",
      });
      if (!res.ok) {
        const text = await res.text();
        if (res.status >= 500) throw new Error(SERVICE_UNAVAILABLE_MESSAGE);
        throw new Error(text || res.statusText);
      }
      return res.json();
    } catch (error) {
      const msg = formatApiError(error);
      toastApiError(msg);
      throw new Error(msg);
    }
  },
  searchFaceByUrl: (imageUrl: string, limit = 20) =>
    api<FaceSearchResponse>("/reid/faces/search-by-url", {
      method: "POST",
      body: JSON.stringify({ image_url: imageUrl, limit }),
    }),
  crawlFaceUrls: (urls: string[]) =>
    api<FaceCrawlResponse>("/reid/faces/crawl", {
      method: "POST",
      body: JSON.stringify({ urls }),
    }),
  leadershipRoster: (tab = "executive") =>
    api<LeadershipRoster>(`/reid/leadership/roster?tab=${encodeURIComponent(tab)}`),
  leadershipScan: (opts?: {
    tab?: string;
    run_web_reverse?: boolean;
    match_limit?: number;
  }) =>
    api<LeadershipScanResponse>("/reid/leadership/scan", {
      method: "POST",
      body: JSON.stringify({
        tab: opts?.tab ?? "executive",
        run_web_reverse: opts?.run_web_reverse ?? false,
        match_limit: opts?.match_limit ?? 8,
      }),
    }),
  leadershipNameTag: (body: {
    name: string;
    role?: string | null;
    cluster_ids?: number[];
    face_ids?: number[];
  }) =>
    api<LeadershipNameTagResponse>("/reid/leadership/name-tag", {
      method: "POST",
      body: JSON.stringify({
        name: body.name,
        role: body.role ?? undefined,
        cluster_ids: body.cluster_ids ?? [],
        face_ids: body.face_ids ?? [],
      }),
    }),
  // Shared visual search: images via Qdrant embeddings; videos (mime=video) via
  // Gemini frame embeddings + Qdrant + optional VLM rerank. Carousel uses mime=video.
  search: async (q: string, person?: string, mime?: string): Promise<SearchResponse> => {
    const params = new URLSearchParams({ q });
    if (person?.trim()) params.set("person", person.trim());
    if (mime && mime !== "all") params.set("mime", mime);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 900_000);
    try {
      const res = await fetch(`${API_BASE}/search?${params}`, {
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!res.ok) {
        const text = await res.text();
        if (res.status >= 500) {
          throw new Error(SERVICE_UNAVAILABLE_MESSAGE);
        }
        throw new Error(text || res.statusText);
      }
      const raw = (await res.json()) as Partial<SearchResponse>;
      return {
        query: raw.query ?? q.trim(),
        answer: raw.answer ?? "",
        citations: Array.isArray(raw.citations) ? raw.citations.filter(Boolean) : [],
        files: Array.isArray(raw.files) ? raw.files.filter(Boolean) : [],
        moments: Array.isArray(raw.moments) ? raw.moments.filter(Boolean) : [],
      };
    } catch (e) {
      if (e instanceof Error && e.name === "AbortError") {
        const msg = "Search timed out after 2 minutes. Try a shorter query.";
        toastApiError(msg);
        throw new Error(msg);
      }
      const msg = formatApiError(e);
      toastApiError(msg);
      throw new Error(msg);
    } finally {
      clearTimeout(timeout);
    }
  },
  settings: () => api<Settings>("/settings"),
  updateSettings: (body: Partial<Settings>) => api<Settings>("/settings", { method: "PUT", body: JSON.stringify(body) }),
  facesForFile: (driveFileId: string) => api<FileFace[]>(`/faces/by-file/${encodeURIComponent(driveFileId)}`),
  tagFace: (faceId: number, name: string) =>
    api<Person>(`/faces/${faceId}/tag`, { method: "POST", body: JSON.stringify({ name }) }),
  createManualFaceBox: (body: {
    drive_file_id: string;
    bbox_x: number;
    bbox_y: number;
    bbox_width: number;
    bbox_height: number;
    name?: string | null;
  }) =>
    api<{ face: FileFace; person: { id: number; name: string } | null }>("/faces/manual-box", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  reidLinkedinMap: () => api<Record<string, string>>("/reid/linkedin-map"),
  reidStatus: () => api<ReidStatus>("/reid/status"),
  reidGallery: (limit = 48) => api<ReidGalleryItem[]>(`/reid/gallery?limit=${limit}`),
  reidBackfill: (limit = 200) =>
    api<ReidBackfillStats>(`/reid/backfill?limit=${limit}`, { method: "POST" }),
  reidProve: (mediaId: number, embed = true) =>
    api<ReidProveResult>(`/reid/prove/${mediaId}?embed=${embed ? "true" : "false"}`, { method: "POST" }),
  reidProofUrl: (mediaId: number) => `${API_BASE}/reid/proof/${mediaId}`,
  reidBodyCropUrl: (faceId: number) => `${API_BASE}/reid/body-crop/${faceId}`,
  officialImageSearchStatus: () =>
    api<OfficialImageSearchStatus>("/reid/official-image-search/status"),
  officialImageSearch: (body: { face_id?: number; image_url?: string; max_results?: number }) =>
    api<OfficialImageSearchResult>("/reid/official-image-search", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  reverseSearchFace: (faceId: number) =>
    api<FaceReverseSearchResult>(`/reid/faces/${faceId}/reverse-search`, { method: "POST" }),
  googleLensUrlForFace: (faceId: number) => {
    const thumb = faceThumbnailUrl(faceId);
    return thumb
      ? `https://lens.google.com/uploadbyurl?url=${encodeURIComponent(thumb)}`
      : null;
  },
  folderContexts: () => api<FolderContext[]>("/folder-contexts"),
  upsertFolderContext: (folder_path: string, description: string) =>
    api<FolderContext>("/folder-contexts", {
      method: "PUT",
      body: JSON.stringify({ folder_path, description }),
    }),
  deleteFolderContext: (folder_path: string) => {
    const encoded = btoa(folder_path).replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
    return api<void>(`/folder-contexts/${encoded}`, { method: "DELETE" });
  },
  driveSession: () => api<DriveSession>("/api/session"),
  driveToken: () => api<DriveTokenResponse>("/api/drive-token"),
  saveDriveFolder: (id: string, name: string) =>
    api<{ ok: boolean }>("/api/save-folder", {
      method: "POST",
      body: JSON.stringify({ id, name }),
    }),
  driveLogout: () => api<{ ok: boolean }>("/api/logout", { method: "POST" }),
};
