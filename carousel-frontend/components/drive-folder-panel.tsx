"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Script from "next/script";
import {
  CheckSquare,
  FolderOpen,
  HardDrive,
  ListVideo,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import {
  createDriveApi,
  isVideoMime,
  type DriveLibraryFile,
  type DriveSession,
  type IndexedFolder,
} from "@/lib/drive-api";
import { formatApiError } from "@/lib/api";
import { ModalOverlay } from "@/components/modal";

declare global {
  interface Window {
    gapi: any;
    google: any;
    _pickerApiLoaded?: boolean;
  }
}

type DriveApi = ReturnType<typeof createDriveApi>;

type Props = {
  apiBase: string;
  /** Called after a video is prioritized / ready so the parent can select it. */
  onVideoReady?: (video: {
    id: string;
    name: string;
    mime_type: string;
    status: string;
    has_captions?: boolean;
    cue_count?: number;
    message?: string;
  }) => void;
  /** Refresh parent video lists after sync / pull. */
  onLibraryChanged?: () => void;
  className?: string;
  testIdPrefix?: string;
};

const statusTone: Record<string, string> = {
  pending: "text-amber-700",
  processing: "text-blue-700",
  processed: "text-emerald-700",
  error: "text-red-700",
  skipped: "text-slate-500",
};

const PAGE_SIZE = 100;
const MAX_PAGES = 8;

export function DriveFolderPanel({
  apiBase,
  onVideoReady,
  onLibraryChanged,
  className = "",
  testIdPrefix = "drive",
}: Props) {
  const api: DriveApi = useMemo(() => createDriveApi(apiBase), [apiBase]);
  const [session, setSession] = useState<DriveSession | null>(null);
  const [indexedFolders, setIndexedFolders] = useState<IndexedFolder[]>([]);
  const [videos, setVideos] = useState<DriveLibraryFile[]>([]);
  const [videoTotalHint, setVideoTotalHint] = useState(0);
  const [followShortcuts, setFollowShortcuts] = useState(false);
  const shortcutsBusyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [pickerBusy, setPickerBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modalError, setModalError] = useState<string | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [modalVideos, setModalVideos] = useState<DriveLibraryFile[]>([]);
  const [modalLoading, setModalLoading] = useState(false);
  const [modalIndexing, setModalIndexing] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [filterQuery, setFilterQuery] = useState("");

  const loadVideosFromDrive = useCallback(async (): Promise<DriveLibraryFile[]> => {
    const collected: DriveLibraryFile[] = [];
    let offset = 0;
    for (let page = 0; page < MAX_PAGES; page++) {
      const res = await api.driveFilesPage({
        source: "drive",
        limit: PAGE_SIZE,
        offset,
      });
      const items = res.items ?? [];
      for (const f of items) {
        if (isVideoMime(f.mime_type)) collected.push(f);
      }
      offset += items.length;
      if (items.length < PAGE_SIZE || offset >= (res.total ?? 0)) break;
    }
    return collected;
  }, [api]);

  const load = useCallback(async () => {
    try {
      const [ds, foldersRes, settings, vids] = await Promise.all([
        api.driveSession().catch(() => null as DriveSession | null),
        api.indexedFolders().catch(() => ({ folders: [] as IndexedFolder[], total: 0 })),
        api.settingsShortcuts().catch(() => ({ follow_shortcut_folders: false })),
        loadVideosFromDrive().catch(() => [] as DriveLibraryFile[]),
      ]);
      setSession(ds);
      setIndexedFolders(foldersRes.folders ?? []);
      if (!shortcutsBusyRef.current) {
        setFollowShortcuts(Boolean(settings.follow_shortcut_folders));
      }
      setVideos(vids);
      setVideoTotalHint(vids.length);
      setError(null);
    } catch (e) {
      setError(formatApiError(e, "Failed to load Drive status"));
    }
  }, [api, loadVideosFromDrive]);

  useEffect(() => {
    void load();
  }, [load]);

  // OAuth callback: ?connected=1 or ?error=...
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected") === "1") {
      const url = new URL(window.location.href);
      url.searchParams.delete("connected");
      window.history.replaceState({}, "", url.pathname + url.search);
      setNote("Google Drive connected. Choose a folder to pull videos.");
      api
        .syncDriveFiles()
        .then(() => load())
        .catch(() => load())
        .then(() => onLibraryChanged?.());
    } else if (params.get("error")) {
      setError(`Drive connection failed: ${params.get("error")}`);
      const url = new URL(window.location.href);
      url.searchParams.delete("error");
      url.searchParams.delete("detail");
      window.history.replaceState({}, "", url.pathname + url.search);
    }
  }, [api, load, onLibraryChanged]);

  function dismissGooglePickerShell() {
    document.querySelectorAll(".picker-dialog, .picker-dialog-bg").forEach((el) => {
      el.parentElement?.removeChild(el);
    });
  }

  function elevateGooglePickerShell() {
    // Keep Picker nodes as direct body children so position:fixed is viewport-relative
    // (not trapped by overflow/transform ancestors on the studio page).
    document.querySelectorAll(".picker-dialog-bg, .picker-dialog").forEach((el) => {
      if (el.parentElement !== document.body) {
        document.body.appendChild(el);
      }
    });

    document.querySelectorAll(".picker-dialog-bg").forEach((el) => {
      const node = el as HTMLElement;
      node.style.setProperty("position", "fixed", "important");
      node.style.setProperty("inset", "0", "important");
      node.style.setProperty("top", "0", "important");
      node.style.setProperty("left", "0", "important");
      node.style.setProperty("right", "0", "important");
      node.style.setProperty("bottom", "0", "important");
      node.style.setProperty("width", "100vw", "important");
      node.style.setProperty("height", "100vh", "important");
      node.style.setProperty("margin", "0", "important");
      node.style.setProperty("transform", "none", "important");
      node.style.setProperty("z-index", "29999", "important");
      node.style.setProperty("visibility", "visible", "important");
      node.style.setProperty("opacity", "1", "important");
      node.style.setProperty("pointer-events", "auto", "important");
    });

    const vw = Math.max(320, window.innerWidth);
    const vh = Math.max(320, window.innerHeight);
    const width = Math.min(1050, vw - 24);
    const height = Math.min(650, vh - 24);

    document.querySelectorAll(".picker-dialog").forEach((el) => {
      const node = el as HTMLElement;
      node.style.setProperty("position", "fixed", "important");
      node.style.setProperty("top", "50%", "important");
      node.style.setProperty("left", "50%", "important");
      node.style.setProperty("right", "auto", "important");
      node.style.setProperty("bottom", "auto", "important");
      node.style.setProperty("transform", "translate(-50%, -50%)", "important");
      node.style.setProperty("margin", "0", "important");
      node.style.setProperty("width", `${width}px`, "important");
      node.style.setProperty("height", `${height}px`, "important");
      node.style.setProperty("max-width", `calc(100vw - 1.5rem)`, "important");
      node.style.setProperty("max-height", `calc(100vh - 1.5rem)`, "important");
      node.style.setProperty("z-index", "30000", "important");
      node.style.setProperty("visibility", "visible", "important");
      node.style.setProperty("opacity", "1", "important");
      node.style.setProperty("pointer-events", "auto", "important");
      node.style.setProperty("overflow", "hidden", "important");
    });

    document.querySelectorAll(".picker-dialog iframe, .picker-frame").forEach((el) => {
      const node = el as HTMLElement;
      node.style.setProperty("display", "block", "important");
      node.style.setProperty("visibility", "visible", "important");
      node.style.setProperty("opacity", "1", "important");
      node.style.setProperty("pointer-events", "auto", "important");
      node.style.setProperty("width", "100%", "important");
      node.style.setProperty("height", "100%", "important");
      node.style.setProperty("min-height", "420px", "important");
      node.style.setProperty("border", "0", "important");
    });
  }

  async function ensureGooglePickerApi(): Promise<void> {
    if (!window.gapi) {
      await new Promise<void>((resolve, reject) => {
        const existing = document.querySelector(
          'script[src="https://apis.google.com/js/api.js"]'
        ) as HTMLScriptElement | null;
        const onReady = () => {
          if (window.gapi) resolve();
          else reject(new Error("Google API script loaded without window.gapi"));
        };
        if (existing) {
          if (window.gapi) {
            resolve();
            return;
          }
          existing.addEventListener("load", onReady, { once: true });
          existing.addEventListener(
            "error",
            () => reject(new Error("Failed to load Google API script")),
            { once: true }
          );
          return;
        }
        const script = document.createElement("script");
        script.src = "https://apis.google.com/js/api.js";
        script.async = true;
        script.onload = onReady;
        script.onerror = () => reject(new Error("Failed to load Google API script"));
        document.head.appendChild(script);
      });
    }
    if (!window._pickerApiLoaded) {
      await new Promise<void>((resolve, reject) => {
        try {
          window.gapi.load("picker", {
            callback: () => {
              window._pickerApiLoaded = true;
              resolve();
            },
            onerror: () => reject(new Error("Failed to load Google Picker API")),
          });
        } catch (e) {
          reject(e instanceof Error ? e : new Error("Failed to load Google Picker API"));
        }
      });
    }
    if (!window.google?.picker?.PickerBuilder) {
      throw new Error("Google Picker API is unavailable in this browser session.");
    }
  }

  async function openPicker() {
    setPickerBusy(true);
    setError(null);
    dismissGooglePickerShell();
    try {
      const { accessToken, apiKey, appId } = await api.driveToken();
      if (!apiKey) {
        throw new Error(
          "GOOGLE_API_KEY is missing on the backend. Set a Browser API key (Drive + Picker APIs) and restart."
        );
      }
      if (!accessToken) {
        throw new Error("Drive access token missing — reconnect Google Drive and try again.");
      }
      const FOLDER_MIME = "application/vnd.google-apps.folder";
      await ensureGooglePickerApi();

      const origin = `${window.location.protocol}//${window.location.host}`;
      // My Drive tabs stay split (folders vs media). Pin enableDrives(false) so
      // SUPPORT_DRIVES does not spawn a paired "Shared drives" tab per view.
      const myDriveFolderView = new window.google.picker.DocsView(window.google.picker.ViewId.FOLDERS)
        .setEnableDrives(false)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setMimeTypes(FOLDER_MIME)
        .setLabel("My Drive folders");

      const myDriveMediaView = new window.google.picker.DocsView(
        window.google.picker.ViewId.DOCS_IMAGES_AND_VIDEOS
      )
        .setEnableDrives(false)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("My Drive images & videos");

      // Single Shared drives tab: all file types (docs + images/videos + folders).
      const sharedDriveView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS)
        .setEnableDrives(true)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("Shared drives");

      let pickerAlive = true;
      const builder = new window.google.picker.PickerBuilder()
        .setTitle("Choose a folder to pull videos from")
        .setOrigin(origin)
        .addView(myDriveFolderView)
        .addView(myDriveMediaView)
        .addView(sharedDriveView)
        .setOAuthToken(accessToken)
        .setDeveloperKey(apiKey)
        .enableFeature(window.google.picker.Feature.SUPPORT_DRIVES)
        .setCallback(async (data: any) => {
          if (data.action === window.google.picker.Action.CANCEL) {
            pickerAlive = false;
            return;
          }
          if (data.action !== window.google.picker.Action.PICKED) return;
          pickerAlive = false;
          const doc = data.docs[0];
          if (doc.mimeType && doc.mimeType !== FOLDER_MIME) {
            setError(
              `"${doc.name}" is a file. Use Select folder (top-right) to choose the folder to index.`
            );
            return;
          }
          setBusy(true);
          try {
            await api.saveDriveFolder(doc.id, doc.name);
            await api.syncDriveFiles().catch(() => {});
            setNote(`Folder “${doc.name}” selected — syncing videos…`);
            await load();
            onLibraryChanged?.();
          } catch (e) {
            setError(formatApiError(e, "Could not save folder"));
          } finally {
            setBusy(false);
          }
        });

      if (appId) builder.setAppId(appId);
      const pickerW = Math.min(1050, Math.max(320, window.innerWidth - 24));
      const pickerH = Math.min(650, Math.max(320, window.innerHeight - 24));
      builder.setSize(pickerW, pickerH);
      const picker = builder.build();
      picker.setVisible(true);

      // Picker rewrites inline position after show — re-center repeatedly for a moment.
      elevateGooglePickerShell();
      const recenter = window.setInterval(() => elevateGooglePickerShell(), 120);
      window.setTimeout(() => window.clearInterval(recenter), 2000);
      requestAnimationFrame(() => elevateGooglePickerShell());

      // Never leave a blank / broken Google Picker shell (missing paint OR invalid developerKey).
      // Cross-origin iframe text is unreadable, but a painted-yet-tiny error pane is common when
      // HTTP referrer restrictions block this origin (Google shows "developer key is invalid").
      window.setTimeout(() => {
        if (!pickerAlive) return;
        const dialog = document.querySelector(".picker-dialog") as HTMLElement | null;
        if (!dialog) return;
        elevateGooglePickerShell();
        const frame = dialog.querySelector("iframe") as HTMLIFrameElement | null;
        const rect = dialog.getBoundingClientRect();
        const frameH = frame?.getBoundingClientRect().height ?? 0;
        const frameW = frame?.getBoundingClientRect().width ?? 0;
        const inViewport =
          rect.top >= -8 &&
          rect.left >= -8 &&
          rect.bottom <= window.innerHeight + 8 &&
          rect.right <= window.innerWidth + 8;
        const looksBlank = !frame || frameH < 80;
        // Error panes after invalid developerKey are often short; healthy folder UI is taller.
        const looksLikeKeyError = Boolean(frame) && frameH >= 80 && frameH < 220 && frameW > 200;
        if (!looksBlank && !looksLikeKeyError && inViewport) return;
        if (!looksBlank && !looksLikeKeyError && !inViewport) {
          // Still recoverable — force another center pass and keep open.
          elevateGooglePickerShell();
          return;
        }
        try {
          picker.setVisible(false);
        } catch {
          /* ignore */
        }
        dismissGooglePickerShell();
        setError(
          looksLikeKeyError
            ? `Google Picker rejected the API key for this site. In Google Cloud Console → Credentials → the Browser key used as backend GOOGLE_API_KEY, add HTTP referrer ${origin}/* (Drive API + Picker API). Then retry Change folder.`
            : `Google Drive folder picker opened blank. Allow this origin as an HTTP referrer on GOOGLE_API_KEY (e.g. ${origin}/*), enable Drive API + Picker API, then retry Change folder.`
        );
      }, 2800);
    } catch (e) {
      dismissGooglePickerShell();
      setError(formatApiError(e, "Could not open folder picker"));
    } finally {
      setPickerBusy(false);
    }
  }

  async function disconnectDrive() {
    setBusy(true);
    try {
      await api.driveLogout();
      setNote("Google Drive disconnected. Indexed library files are kept.");
      await load();
    } catch (e) {
      setError(formatApiError(e, "Disconnect failed"));
    } finally {
      setBusy(false);
    }
  }

  async function useIndexedFolder(folder: IndexedFolder) {
    if (!session?.connected) {
      setError("Connect Google Drive first, then re-select this folder.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.saveDriveFolder(folder.id, folder.name);
      await api.syncDriveFiles().catch(() => {});
      setNote(`Using folder “${folder.name}”.`);
      await load();
      onLibraryChanged?.();
    } catch (e) {
      setError(formatApiError(e, "Could not select folder"));
    } finally {
      setBusy(false);
    }
  }

  async function toggleShortcuts(enabled: boolean) {
    const previous = followShortcuts;
    shortcutsBusyRef.current = true;
    setFollowShortcuts(enabled);
    try {
      const updated = await api.updateShortcutFolders(enabled);
      setFollowShortcuts(updated.follow_shortcut_folders);
      void api.syncDriveFiles().catch(() => {});
    } catch (e) {
      setFollowShortcuts(previous);
      setError(formatApiError(e, "Could not update the shortcut setting."));
    } finally {
      shortcutsBusyRef.current = false;
    }
  }

  async function syncNow() {
    if (!session?.connected) {
      setError("Reconnect Google Drive to sync live folder contents.");
      return;
    }
    setBusy(true);
    try {
      await api.syncDriveFiles();
      setNote("Drive folder sync scheduled.");
      await load();
      onLibraryChanged?.();
    } catch (e) {
      setError(formatApiError(e, "Sync failed"));
    } finally {
      setBusy(false);
    }
  }

  async function openSelectModal() {
    setModalOpen(true);
    setFilterQuery("");
    setSelectedIds(new Set());
    setModalLoading(true);
    setError(null);
    setModalError(null);
    try {
      // Permanent library list (DB) — does not require Drive OAuth.
      const vids = await loadVideosFromDrive();
      setVideos(vids);
      setModalVideos(vids);
      setVideoTotalHint(vids.length);
    } catch (e) {
      const msg = formatApiError(e, "Could not list indexed Drive videos. Please try again.");
      setModalError(msg);
      setError(msg);
      setModalVideos(videos);
    } finally {
      setModalLoading(false);
    }
  }

  function closeSelectModal() {
    if (modalIndexing) return;
    setModalOpen(false);
    setSelectedIds(new Set());
    setFilterQuery("");
    setModalError(null);
  }

  const filteredModalVideos = useMemo(() => {
    const q = filterQuery.trim().toLowerCase();
    if (!q) return modalVideos;
    return modalVideos.filter(
      (v) =>
        v.name.toLowerCase().includes(q) ||
        (v.path || "").toLowerCase().includes(q) ||
        (v.status || "").toLowerCase().includes(q)
    );
  }, [modalVideos, filterQuery]);

  function toggleSelected(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAllFiltered() {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const v of filteredModalVideos) next.add(v.id);
      return next;
    });
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  async function indexSelected() {
    const ids = Array.from(selectedIds);
    if (!ids.length) return;
    const selectedFiles = ids
      .map((id) => modalVideos.find((v) => v.id === id) || videos.find((v) => v.id === id))
      .filter((f): f is DriveLibraryFile => Boolean(f));
    const alreadyIndexed = selectedFiles.filter((f) => f.status === "processed");
    const needsIndex = selectedFiles.filter((f) => f.status !== "processed");

    // Already-indexed library videos work offline; live re-index needs Drive OAuth.
    if (!session?.connected && needsIndex.length > 0 && alreadyIndexed.length === 0) {
      const msg =
        "Reconnect Google Drive to index videos that are not already processed. Already-indexed library videos can be selected while disconnected.";
      setModalError(msg);
      setError(msg);
      return;
    }

    setModalIndexing(true);
    setError(null);
    setModalError(null);
    try {
      const requestIds =
        !session?.connected && alreadyIndexed.length > 0
          ? alreadyIndexed.map((f) => f.id)
          : ids;
      const skippedNeedsIndex =
        !session?.connected && needsIndex.length > 0 ? needsIndex.length : 0;
      const res = await api.prioritizeDriveVideos(requestIds);
      const byId = new Map((res.items ?? []).map((it) => [it.drive_file_id, it]));
      for (const id of requestIds) {
        const file = selectedFiles.find((v) => v.id === id);
        if (!file) continue;
        const item = byId.get(id);
        const cueCount = item?.cue_count;
        const hasCaptions =
          typeof item?.has_captions === "boolean"
            ? item.has_captions
            : typeof cueCount === "number"
              ? cueCount > 0
              : file.status === "processed";
        onVideoReady?.({
          id: file.id,
          name: file.name,
          mime_type: file.mime_type || "video/mp4",
          status: item?.status || file.status,
          has_captions: hasCaptions,
          cue_count: typeof cueCount === "number" ? cueCount : undefined,
          message: item?.message,
        });
      }
      const n = res.queued ?? requestIds.length;
      const successNote =
        n === 1 ? "1 video indexed successfully." : `${n} videos indexed successfully.`;
      setNote(
        skippedNeedsIndex
          ? `${successNote} Skipped ${skippedNeedsIndex} not-yet-indexed video(s) — reconnect Drive to index those.`
          : successNote
      );
      if (skippedNeedsIndex) {
        setError(
          `Reconnect Google Drive to index ${skippedNeedsIndex} selected video(s) that are not already processed.`
        );
      }
      setModalOpen(false);
      setSelectedIds(new Set());
      setModalError(null);
      await load();
      onLibraryChanged?.();
    } catch (e) {
      const msg = formatApiError(e, "Could not index the selected videos. Please try again.");
      setModalError(msg);
      setError(msg);
    } finally {
      setModalIndexing(false);
    }
  }

  return (
    <div
      className={`rounded-xl border border-slate-200 bg-slate-50/80 p-3 sm:p-4 ${className}`}
      data-testid={`${testIdPrefix}-folder-panel`}
    >
      <Script src="https://apis.google.com/js/api.js" strategy="afterInteractive" />

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-sm font-medium text-slate-900">
            <HardDrive size={14} />
            Google Drive folder
          </p>
          {session?.connected ? (
            <p className="mt-0.5 text-xs text-slate-500">
              {session.email}
              {session.selected_folder
                ? ` · Folder: ${session.selected_folder.name}`
                : " · No folder selected yet"}
            </p>
          ) : (
            <p className="mt-0.5 text-xs text-slate-500">
              Drive disconnected — indexed library videos stay available. Reconnect only to sync or
              pull new Drive files.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {session?.connected ? (
            <>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                disabled={pickerBusy || busy}
                onClick={() => void openPicker()}
                data-testid={`${testIdPrefix}-change-folder`}
              >
                <FolderOpen size={14} />
                {pickerBusy ? (
                  "Opening…"
                ) : session.selected_folder ? (
                  "Change folder"
                ) : (
                  "Choose folder"
                )}
              </button>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                disabled={busy}
                onClick={() => void syncNow()}
                title="Sync latest files from the connected folder"
              >
                <RefreshCw size={14} className={busy ? "animate-spin" : undefined} />
                Sync
              </button>
              <a
                className="studio-btn studio-btn-ghost inline-flex items-center gap-1.5 no-underline"
                href={api.googleAuthUrl()}
                data-testid={`${testIdPrefix}-provide`}
                title="Re-authorize Google Drive access (same OAuth as Connect)"
              >
                <HardDrive size={14} />
                Provide
              </a>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                disabled={busy}
                onClick={() => void disconnectDrive()}
                data-testid={`${testIdPrefix}-disconnect`}
              >
                Disconnect
              </button>
            </>
          ) : (
            <a
              className="studio-btn studio-btn-ghost inline-flex items-center gap-1.5 no-underline"
              href={api.googleAuthUrl()}
              data-testid={`${testIdPrefix}-connect`}
            >
              <HardDrive size={14} />
              Connect Google Drive
            </a>
          )}
        </div>
      </div>

      {session?.connected && (
        <label className="mt-3 flex cursor-pointer items-start gap-2.5 border-t border-slate-200/80 pt-3 text-sm">
          <input
            type="checkbox"
            checked={followShortcuts}
            onChange={(e) => void toggleShortcuts(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300"
            data-testid={`${testIdPrefix}-shortcuts`}
          />
          <span>
            <span className="text-slate-800">Pull folder shortcuts</span>
            <span className="mt-0.5 block text-xs text-slate-500">
              Include files inside shortcut folders (not only physical subfolders).
            </span>
          </span>
        </label>
      )}

      {indexedFolders.length > 0 && (
        <div className="mt-3 border-t border-slate-200/80 pt-3">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Previously indexed folders
          </p>
          <ul className="mt-2 max-h-36 space-y-1.5 overflow-y-auto">
            {indexedFolders.slice(0, 12).map((f) => (
              <li
                key={f.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200/70 bg-white/70 px-2.5 py-1.5"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-800">
                    {f.name}
                    {f.is_active ? (
                      <span className="ml-1.5 text-[11px] font-medium text-emerald-700">
                        Active
                      </span>
                    ) : null}
                  </p>
                  <p className="truncate text-[11px] text-slate-500">
                    {f.last_file_count != null ? `${f.last_file_count} files · ` : ""}
                    {f.drive_user_email || ""}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <a
                    href={f.drive_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[11px] font-medium text-blue-600 underline-offset-2 hover:underline"
                  >
                    Open
                  </a>
                  {session?.connected && !f.is_active && (
                    <button
                      type="button"
                      className="text-[11px] font-medium text-slate-800 underline-offset-2 hover:underline"
                      disabled={busy}
                      onClick={() => void useIndexedFolder(f)}
                    >
                      Use
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Library browse/select is never gated on Drive OAuth. */}
      <div className="mt-3 border-t border-slate-200/80 pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                {session?.connected ? "Index specific videos" : "Indexed library"}
              </p>
              <p className="mt-0.5 text-xs text-slate-500">
                {session?.connected
                  ? "Browse indexed Drive videos and choose only the ones you need — does not index the whole folder."
                  : "Browse and select already-indexed Drive videos without reconnecting."}
                {videoTotalHint ? ` ${videoTotalHint} video(s) in library.` : ""}
              </p>
            </div>
            <button
              type="button"
              className="studio-btn studio-btn-primary shrink-0"
              disabled={busy}
              onClick={() => void openSelectModal()}
              data-testid={`${testIdPrefix}-select-videos`}
            >
              <ListVideo size={14} />
              {session?.connected ? "Select videos from Drive" : "Select indexed videos"}
            </button>
          </div>
        </div>

      {note && (
        <p className="mt-2 text-xs text-slate-500" role="status">
          {note}
        </p>
      )}
      {error && (
        <p className="mt-2 text-xs text-red-600" role="alert">
          {error}
        </p>
      )}

      <ModalOverlay
        open={modalOpen}
        onClose={closeSelectModal}
        contentClassName="max-w-[min(96vw,36rem)]"
      >
        <div
          className="carousel-studio drive-select-modal rounded-2xl border border-slate-200 shadow-xl"
          role="dialog"
          aria-modal="true"
          aria-labelledby={`${testIdPrefix}-select-title`}
          data-testid={`${testIdPrefix}-select-modal`}
          style={{ minHeight: "22rem" }}
        >
          <div className="drive-select-modal__chrome drive-select-modal__header flex items-start justify-between gap-3 px-4 py-3 sm:px-5">
            <div className="min-w-0">
              <h2
                id={`${testIdPrefix}-select-title`}
                className="text-base font-semibold text-slate-900"
              >
                {session?.connected ? "Select videos from Drive" : "Select indexed videos"}
              </h2>
              <p className="mt-0.5 text-xs text-slate-500">
                {session?.selected_folder?.name
                  ? `Folder: ${session.selected_folder.name}`
                  : session?.connected
                    ? "Connected folder"
                    : "Permanent indexed library"}
                {" · "}
                {session?.connected
                  ? "Check videos to priority-index. Whole-folder index is not run from here."
                  : "Already-processed videos can be used offline. Reconnect Drive to index new ones."}
              </p>
            </div>
            <button
              type="button"
              className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800"
              onClick={closeSelectModal}
              disabled={modalIndexing}
              aria-label="Close"
            >
              <X size={16} />
            </button>
          </div>

          <div className="drive-select-modal__body space-y-3 px-4 py-3 sm:px-5">
            <div className="relative">
              <Search
                size={14}
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
              />
              <input
                type="search"
                value={filterQuery}
                onChange={(e) => setFilterQuery(e.target.value)}
                placeholder="Filter by name or path…"
                className="drive-select-modal__filter w-full rounded-lg border border-slate-200 py-2 pl-9 pr-3 text-sm outline-none ring-slate-300 placeholder:text-slate-400 focus:ring-2"
                data-testid={`${testIdPrefix}-select-filter`}
              />
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
              <button
                type="button"
                className="font-medium text-slate-700 underline-offset-2 hover:underline"
                onClick={selectAllFiltered}
                disabled={modalLoading || !filteredModalVideos.length}
              >
                Select all
                {filterQuery.trim() ? " matching" : ""}
              </button>
              <span className="text-slate-300">·</span>
              <button
                type="button"
                className="font-medium text-slate-700 underline-offset-2 hover:underline"
                onClick={clearSelection}
                disabled={!selectedIds.size}
              >
                Clear
              </button>
              <span className="ml-auto tabular-nums text-slate-500">
                {selectedIds.size} selected
                {!modalLoading ? ` · ${filteredModalVideos.length} listed` : ""}
              </span>
            </div>

            <div className="drive-select-modal__list max-h-[min(50vh,22rem)] overflow-y-auto rounded-xl border border-slate-200">
              {modalLoading ? (
                <p className="flex items-center justify-center gap-2 px-3 py-10 text-sm text-slate-500">
                  <Loader2 size={16} className="animate-spin" />
                  Loading indexed library videos…
                </p>
              ) : filteredModalVideos.length === 0 ? (
                <p className="px-3 py-10 text-center text-sm text-slate-500">
                  {modalVideos.length === 0
                    ? session?.connected
                      ? "No Drive videos synced yet. Hit Sync on the folder panel, then try again."
                      : "No indexed Drive videos in the library yet. Reconnect Google Drive and sync a folder to add videos."
                    : "No videos match this filter."}
                </p>
              ) : (
                <ul className="divide-y divide-slate-200">
                  {filteredModalVideos.map((v) => {
                    const checked = selectedIds.has(v.id);
                    return (
                      <li key={v.id}>
                        <label
                          className={`drive-select-modal__row flex cursor-pointer items-start gap-3 px-3 py-2.5 ${
                            checked ? "is-selected" : ""
                          }`}
                        >
                          <input
                            type="checkbox"
                            className="mt-1 h-4 w-4 shrink-0 rounded border-slate-300"
                            checked={checked}
                            onChange={() => toggleSelected(v.id)}
                            data-testid={`${testIdPrefix}-select-${v.id}`}
                          />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm text-slate-900">{v.name}</span>
                            <span
                              className={`mt-0.5 block text-[11px] capitalize ${
                                statusTone[v.status] || "text-slate-500"
                              }`}
                            >
                              {v.status}
                              {v.path ? ` · ${v.path}` : ""}
                            </span>
                          </span>
                        </label>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>

          {modalError ? (
            <p className="px-4 text-sm text-red-600 sm:px-5" role="alert">
              {modalError}
            </p>
          ) : null}

          <div className="drive-select-modal__chrome drive-select-modal__footer flex flex-wrap items-center justify-end gap-2 px-4 py-3 sm:px-5">
            <button
              type="button"
              className="studio-btn studio-btn-ghost"
              onClick={closeSelectModal}
              disabled={modalIndexing}
            >
              Cancel
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-primary"
              disabled={modalIndexing || selectedIds.size === 0}
              onClick={() => void indexSelected()}
              data-testid={`${testIdPrefix}-index-selected`}
            >
              {modalIndexing ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  {session?.connected ? "Indexing…" : "Selecting…"}
                </>
              ) : (
                <>
                  <CheckSquare size={14} />
                  {session?.connected
                    ? `Index selected (${selectedIds.size})`
                    : `Use selected (${selectedIds.size})`}
                </>
              )}
            </button>
          </div>
        </div>
      </ModalOverlay>
    </div>
  );
}
