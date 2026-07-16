export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const SERVICE_UNAVAILABLE_MESSAGE =
  "Can't reach the DFI service right now. It may be starting up or temporarily unavailable.";

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
  auto_index_enabled: boolean;
  auto_index_interval_seconds: number;
};

export type Settings = {
  gemini_model: string;
  gemini_file_search_store_display_name: string;
  auto_index_enabled: boolean;
  auto_index_interval_seconds: number;
  reindex_errored_files: boolean;
  reindex_skipped_files: boolean;
  follow_shortcut_folders: boolean;
  gemini_file_search_search_enabled: boolean;
  search_parallel_variants_enabled: boolean;
  search_use_captions: boolean;
  search_rerank_enabled: boolean;
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
    throw new Error(formatApiError(error));
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
  driveFiles: (status?: string) => api<DriveFile[]>(`/drive/files${status ? `?status=${status}` : ""}`),
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
  captionStats: () => api<CaptionStats>("/index/captions"),
  backfillCaptions: () => api<{ ok: boolean; scheduled: boolean }>("/backfill/image-captions", { method: "POST" }),
  retryDriveFile: (id: string) => api<DriveFile>(`/drive/files/${id}/retry`, { method: "POST" }),
  removeDriveFile: (id: string) => api<void>(`/drive/files/${id}`, { method: "DELETE" }),
  youtubeVideos: () => api<DriveFile[]>("/youtube/videos"),
  addYoutubeVideos: (urls: string[], indexNow = true, downloadLocal = true) =>
    api<YoutubeRegisterResponse>("/youtube/videos", {
      method: "POST",
      body: JSON.stringify({ urls, index_now: indexNow, download_local: downloadLocal }),
    }),
  indexStatus: () => api<IndexStatus>("/index"),
  triggerIndex: () => api<IndexStatus>("/index", { method: "POST" }),
  triggerReindex: () => api<IndexStatus>("/reindex", { method: "POST" }),
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
        throw new Error(formatApiError(new Error(text || res.statusText)));
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
        throw new Error("Search timed out after 2 minutes. Try a shorter query.");
      }
      throw new Error(formatApiError(e));
    } finally {
      clearTimeout(timeout);
    }
  },
  settings: () => api<Settings>("/settings"),
  updateSettings: (body: Partial<Settings>) => api<Settings>("/settings", { method: "PUT", body: JSON.stringify(body) }),
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
