/**
 * Shared Google Drive OAuth / folder / pull helpers for carousel + /test.
 * Hits this backend's own auth routes (GIS + Drive OAuth), not the search API.
 */

import { formatApiError } from "@/lib/api";
import { toastApiError } from "@/lib/toast-api-error";

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
  hidden?: boolean;
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

/** Bound to the browser that completed OAuth — not shared across machines. */
const DRIVE_BROWSER_SESSION_KEY = "carousel_drive_session";

export function readDriveBrowserSession(): string {
  if (typeof window === "undefined") return "";
  try {
    return (window.sessionStorage.getItem(DRIVE_BROWSER_SESSION_KEY) || "").trim();
  } catch {
    return "";
  }
}

export function writeDriveBrowserSession(token: string): void {
  if (typeof window === "undefined") return;
  const value = (token || "").trim();
  try {
    if (value) window.sessionStorage.setItem(DRIVE_BROWSER_SESSION_KEY, value);
    else window.sessionStorage.removeItem(DRIVE_BROWSER_SESSION_KEY);
  } catch {
    /* ignore quota / private mode */
  }
}

export function clearDriveBrowserSession(): void {
  writeDriveBrowserSession("");
}

function driveSessionHeaders(): HeadersInit {
  const token = readDriveBrowserSession();
  return token ? { "X-Carousel-Drive-Session": token } : {};
}

async function jsonApi<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...driveSessionHeaders(),
      ...(init?.headers || {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    const msg = formatApiError(new Error(text || res.statusText));
    toastApiError(msg);
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export function createDriveApi(apiBase: string) {
  return {
    driveSession: () => jsonApi<DriveSession>(apiBase, "/api/session"),
    driveToken: () => jsonApi<DriveTokenResponse>(apiBase, "/api/drive-token"),
    saveDriveFolder: (id: string, name: string) =>
      jsonApi<{
        ok: boolean;
        folder?: { id: string; name: string; drive_url?: string };
        reused_existing_index?: boolean;
        requeued?: number;
      }>(apiBase, "/api/save-folder", {
        method: "POST",
        body: JSON.stringify({ id, name }),
      }),
    driveLogout: async () => {
      try {
        return await jsonApi<{ ok: boolean }>(apiBase, "/api/logout", { method: "POST" });
      } finally {
        clearDriveBrowserSession();
      }
    },
    syncDriveFiles: () =>
      jsonApi<{ ok: boolean; scheduled?: boolean }>(apiBase, "/drive/sync", {
        method: "POST",
      }),
    indexedFolders: () =>
      jsonApi<{ folders: IndexedFolder[]; total: number }>(apiBase, "/index/folders"),
    /** Soft-hide a folder from history. Never deletes indexed media. */
    hideIndexedFolder: (folderId: string) =>
      jsonApi<{ ok: boolean; id: string; hidden: boolean }>(
        apiBase,
        `/index/folders/${encodeURIComponent(folderId)}`,
        { method: "DELETE" }
      ),
    driveFilesPage: (opts?: {
      status?: string;
      source?: string;
      rootFolderId?: string | null;
      limit?: number;
      offset?: number;
    }) => {
      const params = new URLSearchParams();
      if (opts?.status) params.set("status", opts.status);
      if (opts?.source) params.set("source", opts.source);
      if (opts?.rootFolderId) params.set("root_folder_id", opts.rootFolderId);
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
    /**
     * Top-level navigation to start Drive OAuth.
     * Prefer the FastAPI origin so the browser follows the 307 to Google.
     * Same-origin `/api/proxy` fetch would otherwise serve Google HTML on Studio.
     */
    googleAuthUrl: (returnTo?: string) => {
      const dest = (returnTo || "").trim();
      const qs = dest ? `?return_to=${encodeURIComponent(dest)}` : "";
      const origin = (process.env.NEXT_PUBLIC_BACKEND_URL || "").replace(/\/+$/, "");
      // Same-origin `/backend/*` Route Handler (app/backend/[...path]) forwards the
      // 307 to Google without following it; prefer it over the relative proxy base.
      const base = origin || (apiBase.startsWith("/") ? "/backend" : apiBase);
      return `${base}/auth/google${qs}`;
    },
  };
}

export function isVideoMime(mime: string | null | undefined): boolean {
  return Boolean(mime && mime.toLowerCase().startsWith("video/"));
}

export { driveFileOpenUrl, driveFolderPath } from "./drive-path";
