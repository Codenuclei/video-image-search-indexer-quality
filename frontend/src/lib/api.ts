export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Person = {
  id: number;
  name: string;
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

export type DriveTokenResponse = {
  accessToken: string;
  apiKey: string;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const faceThumbnailUrl = (faceId: number | null) =>
  faceId ? `${API_BASE}/faces/${faceId}/thumbnail` : null;

export const driveFilePreviewUrl = (driveFileId: string) =>
  `${API_BASE}/drive/files/${driveFileId}/preview`;

export const apiAssetUrl = (path: string) =>
  path.startsWith("http") ? path : `${API_BASE}${path}`;

export const apiClient = {
  health: () =>
    api<{ status: string; search?: string; fennec_enabled?: boolean; fennec_ready?: boolean }>("/health"),
  persons: () => api<Person[]>("/persons"),
  person: (id: number) => api<Person>(`/persons/${id}`),
  renamePerson: (id: number, name: string) =>
    api<Person>(`/persons/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
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
  clusters: (includeIgnored = false) =>
    api<Cluster[]>(`/clusters${includeIgnored ? "?include_ignored=true" : ""}`),
  nameCluster: (id: number, name: string) =>
    api<Person>(`/clusters/${id}/name`, { method: "POST", body: JSON.stringify({ name }) }),
  ignoreCluster: (id: number) => api<void>(`/clusters/${id}/ignore`, { method: "POST" }),
  mergeCluster: (id: number, personId: number) =>
    api<void>(`/clusters/${id}/merge`, { method: "POST", body: JSON.stringify({ person_id: personId }) }),
  syncDriveFiles: () => api<{ ok: boolean; scheduled: boolean }>("/drive/sync", { method: "POST" }),
  driveFiles: (status?: string) => api<DriveFile[]>(`/drive/files${status ? `?status=${status}` : ""}`),
  retryDriveFile: (id: string) => api<DriveFile>(`/drive/files/${id}/retry`, { method: "POST" }),
  removeDriveFile: (id: string) => api<void>(`/drive/files/${id}`, { method: "DELETE" }),
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
        throw new Error("Search timed out after 2 minutes. Try a shorter query.");
      }
      throw e;
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
