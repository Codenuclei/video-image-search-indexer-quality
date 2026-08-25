"use client";

import { useEffect, useRef, useState, useMemo, useCallback } from "react";
import Link from "next/link";
import Script from "next/script";
import {
  apiClient,
  type FolderContext,
  type DriveFile,
  type IndexStatus,
  type DriveSession,
  type Settings,
  type IndexedFolder,
  type DriveCacheStatus,
  type LibraryResponse,
  API_BASE,
} from "@/lib/api";
import { Button, Card, Input, LoadingLabel, Spinner } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { formatCount, humanizeIndexError } from "@/lib/index-errors";
import { formatDate } from "@/lib/utils";
import { toastApiError } from "@/lib/toast-api-error";
import { toast } from "sonner";
import { getCachedIndexStatus, pollIndexStatus, useIndexStatusStore } from "@/lib/index-status-store";
import { useCachedResource } from "@/lib/use-cached-resource";
import { cacheStatusRevision, indexStatusRevision } from "@/lib/fingerprints";
import { useAuthSession } from "@/components/auth-gate";
import { indexedFolderPickerOptions } from "@/lib/library-folders";

declare global {
  interface Window {
    gapi: any;
    google: any;
    _pickerApiLoaded?: boolean;
  }
}

const QUEUE_PAGE_SIZE = 40;
const QUEUE_STATUS_TABS = [
  { value: "", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "processing", label: "Active" },
  { value: "processed", label: "Completed" },
  { value: "error", label: "Failed" },
  { value: "skipped", label: "Skipped" },
] as const;

const statusColor: Record<string, string> = {
  pending: "text-amber-600 dark:text-yellow-400",
  processing: "text-blue-600 dark:text-blue-400",
  processed: "text-emerald-600 dark:text-green-400",
  error: "text-red-600 dark:text-red-400",
  skipped: "text-muted-foreground",
};

type FoldersSnapshot = {
  folderContexts: FolderContext[];
  driveSession: DriveSession | null;
  settings: Settings | null;
  indexedFolders: IndexedFolder[];
  libraryFolderPaths: string[];
};

export function FoldersPage({
  embedded = false,
  indexedLayout = "list",
}: {
  embedded?: boolean;
  indexedLayout?: "list" | "cards";
} = {}) {
  const { isAdmin } = useAuthSession();
  const { status: sharedStatus } = useIndexStatusStore();
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [folderContexts, setFolderContexts] = useState<FolderContext[]>([]);
  const [driveSession, setDriveSession] = useState<DriveSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerBusy, setPickerBusy] = useState(false);
  const [folderBusy, setFolderBusy] = useState(false);
  const [editingFolder, setEditingFolder] = useState<string | null>(null);
  const [editDescription, setEditDescription] = useState("");
  const [savingFolder, setSavingFolder] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const shortcutsBusyRef = useRef(false);
  const [indexedFolders, setIndexedFolders] = useState<IndexedFolder[]>([]);
  const [libraryFolderPaths, setLibraryFolderPaths] = useState<string[]>([]);

  const [queueOpen, setQueueOpen] = useState(false);
  const [queueStatus, setQueueStatus] = useState("");
  const [queueOffset, setQueueOffset] = useState(0);
  const [queueTotal, setQueueTotal] = useState(0);
  const [queueItems, setQueueItems] = useState<DriveFile[]>([]);
  const [queueLoading, setQueueLoading] = useState(false);
  const foldersResource = useCachedResource<FoldersSnapshot>({
    key: "foldersPage",
    fetcher: async () => {
      const [fc, ds, st, foldersRes, shell] = await Promise.all([
        apiClient.folderContexts().catch(() => [] as FolderContext[]),
        apiClient.driveSession().catch(() => null as DriveSession | null),
        apiClient.settings().catch(() => null as Settings | null),
        apiClient.indexedFolders().catch(() => ({ folders: [] as IndexedFolder[], total: 0 })),
        apiClient.driveLibraryShell().catch(() => null as LibraryResponse | null),
      ]);
      return {
        folderContexts: Array.isArray(fc) ? fc : [],
        driveSession: ds,
        settings: st,
        indexedFolders: foldersRes.folders ?? [],
        libraryFolderPaths: indexedFolderPickerOptions(shell?.tree).map((f) => f.value),
      };
    },
    getRevision: async () => {
      const [cacheStatus, settingsRev, contextsRev] = await Promise.all([
        apiClient.cacheStatus().catch(() => null as DriveCacheStatus | null),
        apiClient.settingsRevision().catch(() => null),
        apiClient.folderContextsRevision().catch(() => null),
      ]);
      const index = getCachedIndexStatus();
      return [
        cacheStatus ? cacheStatusRevision(cacheStatus) : "drive:unknown",
        settingsRev?.revision ?? "settings:unknown",
        contextsRev?.revision ?? "contexts:unknown",
        index?.revision ?? (index ? indexStatusRevision(index) : "index:unknown"),
      ].join("|");
    },
    pollMs: 30000,
  });

  const load = useCallback(
    async (force = true) => {
      await foldersResource.refresh(force);
      void pollIndexStatus();
    },
    [foldersResource.refresh]
  );

  useEffect(() => {
    const snapshot = foldersResource.data;
    if (!snapshot) return;
    setFolderContexts(snapshot.folderContexts);
    setDriveSession(snapshot.driveSession);
    setSettings((prev) => {
      const st = snapshot.settings;
      if (!st) return prev;
      if (shortcutsBusyRef.current && prev) {
        return { ...st, follow_shortcut_folders: prev.follow_shortcut_folders };
      }
      return st;
    });
    setIndexedFolders(snapshot.indexedFolders);
    setLibraryFolderPaths(snapshot.libraryFolderPaths ?? []);
  }, [foldersResource.data]);

  const loadQueue = useCallback(async (statusFilter: string, offset: number) => {
    setQueueLoading(true);
    try {
      const page = await apiClient.driveFilesPage({
        status: statusFilter || undefined,
        limit: QUEUE_PAGE_SIZE,
        offset,
      });
      setQueueItems(page.items);
      setQueueTotal(page.total);
      setQueueOffset(page.offset);
    } catch {
      /* api() already toasted */
    } finally {
      setQueueLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!queueOpen) return;
    void loadQueue(queueStatus, queueOffset);
  }, [queueOpen, queueStatus, queueOffset, loadQueue]);

  // Handle ?connected=1 or ?error=... redirected from OAuth callback
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected") === "1") {
      window.history.replaceState({}, "", window.location.pathname.startsWith("/test") ? "/test/folders" : "/folders");
      apiClient.syncDriveFiles().then(() => load()).catch(() => load());
    } else if (params.get("error")) {
      toastApiError(`Drive connection failed: ${params.get("error")}`);
      window.history.replaceState({}, "", window.location.pathname.startsWith("/test") ? "/test/folders" : "/folders");
    }
  }, []);

  async function toggleShortcutFolders(enabled: boolean) {
    if (!settings) return;
    const previous = settings.follow_shortcut_folders;
    shortcutsBusyRef.current = true;
    setSettings({ ...settings, follow_shortcut_folders: enabled });
    try {
      const updated = await apiClient.updateSettings({ follow_shortcut_folders: enabled });
      setSettings((s) =>
        s ? { ...s, follow_shortcut_folders: updated.follow_shortcut_folders } : updated
      );
      void apiClient.syncDriveFiles().catch(() => {});
    } catch {
      setSettings((s) => (s ? { ...s, follow_shortcut_folders: previous } : s));
      /* api() already toasted */
    } finally {
      shortcutsBusyRef.current = false;
    }
  }

  async function openPicker() {
    setPickerBusy(true);
    try {
      const { accessToken, apiKey, appId } = await apiClient.driveToken();
      if (!apiKey) {
        toastApiError(
          "GOOGLE_API_KEY is missing on the backend. Set a Browser API key (Drive + Picker APIs, HTTP referrer localhost:3001) in backend/.env and restart."
        );
        return;
      }
      const FOLDER_MIME = "application/vnd.google-apps.folder";

      if (!window._pickerApiLoaded) {
        await new Promise<void>((resolve) => window.gapi.load("picker", resolve));
        window._pickerApiLoaded = true;
      }

      // My Drive media tab + Shared drives (folders selectable in both).
      const myDriveMediaView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS_IMAGES_AND_VIDEOS)
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

      const builder = new window.google.picker.PickerBuilder()
        .setTitle("Choose a folder to index")
        .addView(myDriveMediaView)
        .addView(sharedDriveView)
        .setOAuthToken(accessToken)
        .setDeveloperKey(apiKey)
        .enableFeature(window.google.picker.Feature.SUPPORT_DRIVES)
        .setCallback(async (data: any) => {
          if (data.action !== window.google.picker.Action.PICKED) return;
          const doc = data.docs[0];
          if (doc.mimeType && doc.mimeType !== FOLDER_MIME) {
            toastApiError(
              `"${doc.name}" is a file. Browse images/videos to preview media, then use Select folder (top-right) to choose the folder to index.`
            );
            return;
          }
          setFolderBusy(true);
          try {
            await apiClient.saveDriveFolder(doc.id, doc.name);
            await apiClient.syncDriveFiles().catch(() => {});
            await load();
          } catch {
            /* saveDriveFolder already toasted */
          } finally {
            setFolderBusy(false);
          }
        });

      if (appId) {
        builder.setAppId(appId);
      }

      builder.build().setVisible(true);
    } catch {
      /* driveToken already toasted; local picker errors toasted above */
    } finally {
      setPickerBusy(false);
    }
  }

  async function disconnectDrive() {
    await apiClient.driveLogout();
    await load();
  }

  useEffect(() => {
    if (sharedStatus) setStatus(sharedStatus);
  }, [sharedStatus]);

  async function retryFile(id: string, name?: string, source?: string) {
    setBusy(true);
    try {
      await apiClient.retryDriveFile(id);
      toast.success("Retry queued", { description: name || id });
      await load();
      if (queueOpen) await loadQueue(queueStatus, queueOffset);
    } catch {
      /* api() already toasted */
    } finally {
      setBusy(false);
    }
  }

  async function removeFile(id: string) {
    setBusy(true);
    try {
      await apiClient.removeDriveFile(id);
      await load();
      if (queueOpen) await loadQueue(queueStatus, queueOffset);
    } catch {
      /* api() already toasted */
    } finally {
      setBusy(false);
    }
  }



  async function saveFolderContext(folderPath: string) {
    setSavingFolder(folderPath);
    try {
      await apiClient.upsertFolderContext(folderPath, editDescription);
      setEditingFolder(null);
      await load();
    } catch {
      /* api() already toasted */
    } finally {
      setSavingFolder(null);
    }
  }

  async function deleteContext(folderPath: string) {
    await apiClient.deleteFolderContext(folderPath);
    await load();
  }

  const uniqueFolders = useMemo(() => {
    const paths = new Set<string>();
    for (const p of libraryFolderPaths) {
      if (p && p !== "/") paths.add(p);
    }
    for (const fc of folderContexts) {
      if (fc.folder_path && fc.folder_path !== "/") paths.add(fc.folder_path);
    }
    return Array.from(paths).sort();
  }, [folderContexts, libraryFolderPaths]);

  const contextByPath = useMemo(() => {
    const map: Record<string, FolderContext> = {};
    for (const fc of folderContexts) map[fc.folder_path] = fc;
    return map;
  }, [folderContexts]);

  const counts = status?.counts_by_status ?? {};
  const queuePageStart = queueTotal === 0 ? 0 : queueOffset + 1;
  const queuePageEnd = Math.min(queueOffset + QUEUE_PAGE_SIZE, queueTotal);

  function openQueue(statusFilter: string = "") {
    setQueueStatus(statusFilter);
    setQueueOffset(0);
    setQueueOpen(true);
  }

  return (
    <div className="space-y-6">
      <Script src="https://apis.google.com/js/api.js" strategy="lazyOnload" />

      {!embedded && (
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold">Folders</h2>
          <p className="text-sm text-muted-foreground">Drive files tracked from your connected folder</p>
        </div>
        {isAdmin ? (
          <Link
            href="/admin"
            className="text-sm font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
          >
            Indexing controls → Admin
          </Link>
        ) : null}
      </div>
      )}

      <Card>
        <div className="mb-3 flex items-center justify-between gap-2">
          <h3 className="font-medium">{indexedLayout === "cards" ? "Indexed folders" : "Previously indexed folders"}</h3>
          {indexedLayout === "cards" && indexedFolders.length > 0 && (
            <span className="text-xs text-muted-foreground">{indexedFolders.length} folders</span>
          )}
        </div>
        {indexedFolders.length === 0 ? (
          <p className="text-sm text-muted-foreground">No folders indexed yet.</p>
        ) : indexedLayout === "cards" ? (
          <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {indexedFolders.map((f) => (
              <li key={f.id} className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
                <div className="relative h-28 bg-gradient-to-br from-emerald-500/20 via-muted to-sky-500/20">
                  <span className="absolute left-1/2 top-1/2 flex h-12 w-12 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full bg-card text-blue-700 shadow dark:text-blue-300">
                    📁
                  </span>
                </div>
                <div className="space-y-2 p-4">
                  <div className="flex items-start justify-between gap-2">
                    <p className="truncate font-semibold">{f.name}</p>
                    {f.is_active && (
                      <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                        Active
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {f.last_file_count != null ? `${formatCount(f.last_file_count)} assets` : "—"}
                  </p>
                  <a
                    href={f.drive_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
                    title={f.last_indexed_at ? `Synced ${new Date(f.last_indexed_at).toLocaleString()}` : "Not synced yet"}
                  >
                    Open in Drive
                  </a>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <ul className="divide-y divide-border/50 overflow-hidden rounded-lg border border-border/60">
            {indexedFolders.map((f) => (
              <li
                key={f.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 hover:bg-muted/20"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-sm font-medium">{f.name}</p>
                    {f.is_active && (
                      <span className="rounded-md bg-emerald-500/15 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
                        Active
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {f.last_file_count != null ? `${formatCount(f.last_file_count)} files · ` : ""}
                    {f.drive_user_email ? `${f.drive_user_email} · ` : ""}
                    {f.last_indexed_at
                      ? `last synced ${new Date(f.last_indexed_at).toLocaleString()}`
                      : null}
                  </p>
                </div>
                <a
                  href={f.drive_url}
                  target="_blank"
                  rel="noreferrer"
                  className="shrink-0 text-xs font-medium text-blue-600 underline-offset-2 hover:underline dark:text-blue-400"
                >
                  Open in Drive
                </a>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card className={driveSession?.connected ? "border-green-800/50 bg-green-950/10" : "border-yellow-800/50 bg-yellow-950/10"}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium">
              {driveSession?.connected ? "Google Drive connected" : "Google Drive not connected"}
            </p>
            {driveSession?.connected ? (
              <p className="text-xs text-muted-foreground">
                {folderBusy
                  ? "Saving connected folder and syncing…"
                  : `${driveSession.email}${
                      driveSession.selected_folder
                        ? ` · ${driveSession.selected_folder.name}`
                        : " · No folder selected"
                    }`}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">Connect Google to index Drive files.</p>
            )}
          </div>
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            {driveSession?.connected ? (
              <>
                <Button
                  className="w-full sm:w-auto"
                  onClick={openPicker}
                  disabled={pickerBusy || folderBusy}
                >
                  {folderBusy ? (
                    <LoadingLabel>Switching folder…</LoadingLabel>
                  ) : pickerBusy ? (
                    <LoadingLabel>Opening…</LoadingLabel>
                  ) : driveSession.selected_folder ? (
                    "Change folder"
                  ) : (
                    "Choose folder"
                  )}
                </Button>
                <Button
                  className="w-full sm:w-auto"
                  variant="secondary"
                  onClick={disconnectDrive}
                  disabled={folderBusy || pickerBusy}
                >
                  Disconnect
                </Button>
              </>
            ) : (
              <Button className="w-full sm:w-auto" onClick={() => window.location.href = `${API_BASE}/auth/google`}>
                Connect Google Drive
              </Button>
            )}
          </div>
        </div>
        {driveSession?.connected && settings && (
          <label className="mt-4 flex cursor-pointer items-center gap-3 border-t border-border pt-4 text-sm">
            <input
              type="checkbox"
              checked={settings.follow_shortcut_folders}
              onChange={(e) => toggleShortcutFolders(e.target.checked)}
              className="h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
            />
            <span className="text-foreground">Pull folder shortcuts</span>
          </label>
        )}
      </Card>

      <Card className={status?.is_running ? "border-blue-800 bg-blue-950/20" : ""}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-medium">
              {status?.is_running && <Spinner size={14} />}
              {status?.is_running ? "Indexing" : "Indexer idle"}
            </p>
            {status?.is_running ? (
              <p className="mt-1 text-xs text-muted-foreground">
                Images {(status.image_slots?.active ?? status.active_image_jobs ?? 0)}/{(status.image_slots?.max ?? "—")}
                {" · "}
                Videos {(status.video_slots?.active ?? status.active_video_jobs ?? 0)}/{(status.video_slots?.max ?? "—")}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                {status?.auto_index_enabled ? `Auto-sync ${status.auto_index_interval_seconds}s` : "Manual indexing"}
                {status?.last_run_at ? ` · ${formatDate(status.last_run_at)}` : ""}
              </p>
            )}
            {status?.go_indexer_enabled && (
              <p className="mt-1 text-xs text-muted-foreground">
                Go {status.go_indexer_alive ? "active" : "idle"}
                {status.go_files_per_sec != null && status.go_files_per_sec > 0
                  ? ` · ${status.go_files_per_sec.toFixed(2)}/s`
                  : ""}
              </p>
            )}
          </div>
          <Button variant="secondary" onClick={() => openQueue()}>
            Queue
          </Button>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 text-sm sm:grid-cols-5">
          {(
            [
              { key: "pending", label: "Pending", className: "text-amber-600 dark:text-yellow-400", count: counts.pending ?? 0 },
              { key: "processing", label: "Active", className: "text-blue-600 dark:text-blue-400", count: counts.processing ?? 0 },
              { key: "processed", label: "Completed", className: "text-emerald-600 dark:text-green-400", count: counts.processed ?? 0 },
              { key: "error", label: "Failed", className: "text-red-600 dark:text-red-400", count: counts.error ?? 0 },
              {
                key: "skipped",
                label: "Skipped",
                className: "text-muted-foreground",
                count: counts.skipped ?? 0,
              },
            ] as const
          ).map((card) => (
            <button
              key={card.key}
              type="button"
              onClick={() => openQueue(card.key)}
              title={`Open ${card.label} in indexing queue`}
              className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2 text-left transition-colors hover:border-border hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <p className="text-xs text-muted-foreground">{card.label}</p>
              <p className={`text-lg font-semibold ${card.className}`}>{card.count}</p>
            </button>
          ))}
        </div>
        {status?.last_run && (
          <div className="mt-4 border-t border-border/50 pt-4">
            <p className="text-xs text-muted-foreground">
              Last run: {formatCount(status.last_run.discovered)} found ·{" "}
              {formatCount(status.last_run.processed)} done · {formatCount(status.last_run.errored)} errors
            </p>
          </div>
        )}
      </Card>

      {uniqueFolders.length > 0 && (
        <Card>
          <h3 className="mb-3 font-medium text-sm">Folder context</h3>
          <div className="space-y-2">
            {uniqueFolders.map((fp) => {
              const ctx = contextByPath[fp];
              const isEditing = editingFolder === fp;
              const isSaving = savingFolder === fp;
              const folderName = fp === "/"
                ? (driveSession?.selected_folder?.name ?? "Connected folder (root)")
                : (fp.split("/").filter(Boolean).pop() ?? fp);

              return (
                <div key={fp} className="rounded-md border border-border/50 p-3">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">📁 {folderName}</span>
                        {ctx?.description && (
                          <span className="rounded bg-primary/20 px-1.5 py-0.5 text-xs text-primary">
                            context set
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground truncate">{fp}</p>
                      {ctx?.description && !isEditing && (
                        <p className="mt-1 text-xs text-muted-foreground italic">&ldquo;{ctx.description}&rdquo;</p>
                      )}
                    </div>
                    {!isEditing && (
                      <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:gap-1 shrink-0">
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setEditingFolder(fp);
                            setEditDescription(ctx?.description ?? "");
                          }}
                        >
                          {ctx?.description ? "Edit" : "Add context"}
                        </Button>
                        {ctx?.description && (
                          <Button variant="secondary" onClick={() => deleteContext(fp)}>
                            Remove
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                  {isEditing && (
                    <div className="mt-3 space-y-2">
                      <Input
                        placeholder="Folder description"
                        value={editDescription}
                        onChange={(e) => setEditDescription(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && saveFolderContext(fp)}
                      />
                      <div className="flex gap-2">
                        <Button onClick={() => saveFolderContext(fp)} disabled={isSaving}>
                          {isSaving ? <LoadingLabel>Saving…</LoadingLabel> : "Save"}
                        </Button>
                        <Button variant="secondary" onClick={() => setEditingFolder(null)}>
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      <ModalOverlay
        open={queueOpen}
        onClose={() => setQueueOpen(false)}
        contentClassName="max-w-[min(96vw,56rem)]"
      >
        <Card className="max-h-[min(90dvh,44rem)] overflow-hidden p-0">
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <h3 className="text-sm font-semibold">Indexing queue</h3>
              <p className="text-xs text-muted-foreground">
                {queueTotal} file{queueTotal === 1 ? "" : "s"}
                {queueStatus ? ` · ${queueStatus}` : ""}
              </p>
            </div>
            <Button variant="secondary" onClick={() => setQueueOpen(false)}>
              Close
            </Button>
          </div>
          <div className="flex flex-wrap gap-1 border-b border-border px-3 py-2" role="tablist" aria-label="Queue status">
            {QUEUE_STATUS_TABS.map((tab) => {
              const tabCount =
                tab.value === ""
                  ? Object.values(counts).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0)
                  : (counts[tab.value] ?? 0);
              return (
                <button
                  key={tab.value || "all"}
                  type="button"
                  role="tab"
                  aria-selected={queueStatus === tab.value}
                  onClick={() => {
                    setQueueStatus(tab.value);
                    setQueueOffset(0);
                  }}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    queueStatus === tab.value
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  }`}
                >
                  {tab.label}
                  <span className="ml-1 tabular-nums opacity-70">{tabCount}</span>
                </button>
              );
            })}
          </div>
          <div className="max-h-[min(60dvh,28rem)] overflow-auto">
            {queueLoading ? (
              <p className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <Spinner size={14} /> Loading…
              </p>
            ) : queueItems.length === 0 ? (
              <div className="space-y-1 p-4 text-sm text-muted-foreground">
                <p>
                  {queueStatus === "processing"
                    ? "No files are actively indexing right now."
                    : "No files in this filter."}
                </p>
                {queueStatus === "processing" && (
                  <p className="text-xs">
                    Active only lists files with status <code className="text-[11px]">processing</code>
                    {" "}(in flight). When the indexer is idle, this tab is empty — check Pending or start indexing.
                  </p>
                )}
              </div>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="sticky top-0 border-b border-border bg-card text-muted-foreground">
                  <tr>
                    <th className="p-3">Name</th>
                    <th className="p-3">Source</th>
                    <th className="p-3">Status</th>
                    <th className="p-3">Error</th>
                    <th className="p-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {queueItems.map((f) => {
                    const canRetry = f.status === "error" || f.status === "skipped";
                    const canRemove =
                      f.status === "error" || f.status === "skipped" || !!f.error_message?.includes("404");
                    const friendlyErr = f.error_message ? humanizeIndexError(f.error_message) : null;
                    return (
                      <tr key={f.id} className="border-b border-border/50 hover:bg-muted/30">
                        <td className="max-w-[12rem] truncate p-3 font-medium" title={f.name}>
                          {f.name}
                        </td>
                        <td className="p-3">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs ${
                              f.source === "youtube"
                                ? "bg-blue-600/15 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                                : "bg-muted text-muted-foreground"
                            }`}
                          >
                            {f.source === "youtube" ? "Other" : "Drive"}
                          </span>
                        </td>
                        <td className={`p-3 ${statusColor[f.status] ?? ""}`}>{f.status}</td>
                        <td
                          className="max-w-[16rem] p-3 text-xs text-red-700 dark:text-red-300"
                          title={friendlyErr?.summary ?? ""}
                        >
                          {friendlyErr ? (
                            <span className="line-clamp-2 leading-snug">{friendlyErr.summary}</span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="p-3">
                          <div className="flex flex-wrap gap-1">
                            {canRetry && (
                              <Button
                                variant="secondary"
                                onClick={() => retryFile(f.id, f.name, f.source)}
                                disabled={busy}
                              >
                                Retry
                              </Button>
                            )}
                            {canRemove && (
                              <Button variant="secondary" onClick={() => removeFile(f.id)} disabled={busy}>
                                Remove
                              </Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-3">
            <p className="text-xs text-muted-foreground">
              {queueTotal === 0 ? "0" : `${queuePageStart}–${queuePageEnd}`} of {queueTotal}
            </p>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                disabled={queueOffset <= 0 || queueLoading}
                onClick={() => setQueueOffset(Math.max(0, queueOffset - QUEUE_PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                disabled={queueOffset + QUEUE_PAGE_SIZE >= queueTotal || queueLoading}
                onClick={() => setQueueOffset(queueOffset + QUEUE_PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </Card>
      </ModalOverlay>
    </div>
  );
}
