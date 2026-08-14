/**
 * Shared Google Drive OAuth / folder / pull helpers for carousel + /test.
 * Uses the same backend routes as the main indexer frontend folders page.
 */

export type DriveSession = {
  connected: boolean;
  email?: string;
  selected_folder?: { id: string; name: string } | null;
};

export type DriveTokenResponse = {
  accessToken: string;
  apiKey: string;
  appId?: string | null;
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

export type DriveLibraryFile = {
  id: string;
  name: string;
  mime_type: string;
  path: string;
  status: string;
  size: number | null;
  error_message?: string | null;
  source?: string;
};

export type DriveShortcutSettings = {
  follow_shortcut_folders: boolean;
};

async function jsonApi<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export function createDriveApi(apiBase: string) {
  return {
    driveSession: () => jsonApi<DriveSession>(apiBase, "/api/session"),
    driveToken: () => jsonApi<DriveTokenResponse>(apiBase, "/api/drive-token"),
    saveDriveFolder: (id: string, name: string) =>
      jsonApi<{ ok: boolean; folder?: { id: string; name: string; drive_url?: string } }>(
        apiBase,
        "/api/save-folder",
        { method: "POST", body: JSON.stringify({ id, name }) }
      ),
    driveLogout: () =>
      jsonApi<{ ok: boolean }>(apiBase, "/api/logout", { method: "POST" }),
    syncDriveFiles: () =>
      jsonApi<{ ok: boolean; scheduled?: boolean }>(apiBase, "/drive/sync", {
        method: "POST",
      }),
    indexedFolders: () =>
      jsonApi<{ folders: IndexedFolder[]; total: number }>(apiBase, "/index/folders"),
    driveFilesPage: (opts?: {
      status?: string;
      source?: string;
      limit?: number;
      offset?: number;
    }) => {
      const params = new URLSearchParams();
      if (opts?.status) params.set("status", opts.status);
      if (opts?.source) params.set("source", opts.source);
      params.set("limit", String(opts?.limit ?? 60));
      params.set("offset", String(opts?.offset ?? 0));
      return jsonApi<{
        items: DriveLibraryFile[];
        total: number;
        offset: number;
        limit: number;
      }>(apiBase, `/drive/files/page?${params}`);
    },
    settingsShortcuts: () =>
      jsonApi<DriveShortcutSettings>(apiBase, "/settings").then((s) => ({
        follow_shortcut_folders: Boolean(s.follow_shortcut_folders),
      })),
    updateShortcutFolders: (enabled: boolean) =>
      jsonApi<DriveShortcutSettings>(apiBase, "/settings", {
        method: "PUT",
        body: JSON.stringify({ follow_shortcut_folders: enabled }),
      }).then((s) => ({
        follow_shortcut_folders: Boolean(s.follow_shortcut_folders),
      })),
    prioritizeDriveVideos: (driveFileIds: string[]) =>
      jsonApi<{
        ok: boolean;
        queued: number;
        message: string;
        items: {
          drive_file_id: string;
          ok: boolean;
          name?: string;
          status?: string;
          queued?: boolean;
          message?: string;
          error?: string;
          has_captions?: boolean;
          cue_count?: number;
        }[];
      }>(apiBase, "/search/carousel/prioritize", {
        method: "POST",
        body: JSON.stringify({ drive_file_ids: driveFileIds }),
      }),
    /** Absolute URL for OAuth start (proxied same-origin). */
    googleAuthUrl: (returnTo?: string) => {
      const dest =
        returnTo ||
        (typeof window !== "undefined"
          ? `${window.location.origin}${window.location.pathname}`
          : "");
      const qs = dest ? `?return_to=${encodeURIComponent(dest)}` : "";
      return `${apiBase}/auth/google${qs}`;
    },
  };
}

export function isVideoMime(mime: string | null | undefined): boolean {
  return Boolean(mime && mime.toLowerCase().startsWith("video/"));
}
