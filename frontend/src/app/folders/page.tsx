"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import Link from "next/link";
import Script from "next/script";
import {
  apiClient,
  type FolderContext,
  type DriveFile,
  type IndexStatus,
  type DriveSession,
  type Settings,
  type SkipStats,
  API_BASE,
} from "@/lib/api";
import { Button, Card, ConfirmDialog, Input, LoadingLabel, Spinner } from "@/components/ui";
import { IndexErrorCard } from "@/components/index-error-card";
import { ModalOverlay } from "@/components/modal";
import { formatCount, humanizeIndexError, skipReasonMeta } from "@/lib/index-errors";
import { formatDate } from "@/lib/utils";
import { trackYoutubeDownloads } from "@/lib/youtube-download-toast";
import { toast } from "sonner";

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

export default function FoldersPage() {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [skipStats, setSkipStats] = useState<SkipStats | null>(null);
  const [indexErrorItems, setIndexErrorItems] = useState<DriveFile[]>([]);
  const [indexErrorTotal, setIndexErrorTotal] = useState(0);
  const [folderContexts, setFolderContexts] = useState<FolderContext[]>([]);
  const [driveSession, setDriveSession] = useState<DriveSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerBusy, setPickerBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingFolder, setEditingFolder] = useState<string | null>(null);
  const [editDescription, setEditDescription] = useState("");
  const [savingFolder, setSavingFolder] = useState<string | null>(null);
  const [youtubeInput, setYoutubeInput] = useState("");
  const [youtubeBusy, setYoutubeBusy] = useState(false);
  const [youtubeMsg, setYoutubeMsg] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);

  const [queueOpen, setQueueOpen] = useState(false);
  const [queueStatus, setQueueStatus] = useState("");
  const [queueOffset, setQueueOffset] = useState(0);
  const [queueTotal, setQueueTotal] = useState(0);
  const [queueItems, setQueueItems] = useState<DriveFile[]>([]);
  const [queueLoading, setQueueLoading] = useState(false);
  const [retryingReason, setRetryingReason] = useState<string | null>(null);
  const [confirmRetry, setConfirmRetry] = useState<{ reason: string; count: number; label: string } | null>(
    null
  );

  async function load() {
    try {
      const [s, skips, errs, fc, ds, st] = await Promise.all([
        apiClient.indexStatus(),
        apiClient.skipStats().catch(() => null as SkipStats | null),
        apiClient.indexErrors(30, 0).catch(() => null),
        apiClient.folderContexts().catch(() => [] as FolderContext[]),
        apiClient.driveSession().catch(() => null as DriveSession | null),
        apiClient.settings().catch(() => null as Settings | null),
      ]);
      setStatus(s);
      setSkipStats(skips);
      if (errs) {
        setIndexErrorItems(errs.items);
        setIndexErrorTotal(errs.total);
      } else {
        setIndexErrorItems([]);
        setIndexErrorTotal(0);
      }
      setFolderContexts(Array.isArray(fc) ? fc : []);
      setDriveSession(ds);
      setSettings(st);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }

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
    } catch (e) {
      toast.error("Failed to load queue", {
        description: e instanceof Error ? e.message : "Unknown error",
      });
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
      window.history.replaceState({}, "", "/folders");
      apiClient.syncDriveFiles().then(() => load()).catch(() => load());
    } else if (params.get("error")) {
      setError(`Drive connection failed: ${params.get("error")}`);
      window.history.replaceState({}, "", "/folders");
    }
  }, []);

  async function toggleShortcutFolders(enabled: boolean) {
    if (!settings) return;
    setSettingsSaving(true);
    try {
      const updated = await apiClient.updateSettings({ follow_shortcut_folders: enabled });
      setSettings(updated);
      await apiClient.syncDriveFiles();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update shortcut setting");
    } finally {
      setSettingsSaving(false);
    }
  }

  async function openPicker() {
    setPickerBusy(true);
    try {
      const { accessToken, apiKey, appId } = await apiClient.driveToken();
      const FOLDER_MIME = "application/vnd.google-apps.folder";

      if (!window._pickerApiLoaded) {
        await new Promise<void>((resolve) => window.gapi.load("picker", resolve));
        window._pickerApiLoaded = true;
      }

      const myDriveFolderView = new window.google.picker.DocsView(window.google.picker.ViewId.FOLDERS)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setMimeTypes(FOLDER_MIME)
        .setLabel("My Drive folders");

      const sharedDriveView = new window.google.picker.DocsView()
        .setEnableDrives(true)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("Shared drives");

      const myDriveMediaView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS_IMAGES_AND_VIDEOS)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("My Drive images & videos");

      const sharedDriveMediaView = new window.google.picker.DocsView(window.google.picker.ViewId.DOCS_IMAGES_AND_VIDEOS)
        .setEnableDrives(true)
        .setIncludeFolders(true)
        .setSelectFolderEnabled(true)
        .setLabel("Shared drive images & videos");

      const builder = new window.google.picker.PickerBuilder()
        .setTitle("Choose a folder to index")
        .addView(myDriveFolderView)
        .addView(sharedDriveView)
        .addView(myDriveMediaView)
        .addView(sharedDriveMediaView)
        .setOAuthToken(accessToken)
        .setDeveloperKey(apiKey)
        .enableFeature(window.google.picker.Feature.SUPPORT_DRIVES)
        .setCallback(async (data: any) => {
          if (data.action !== window.google.picker.Action.PICKED) return;
          const doc = data.docs[0];
          if (doc.mimeType && doc.mimeType !== FOLDER_MIME) {
            setError(
              `"${doc.name}" is a file. Browse images/videos to preview media, then use Select folder (top-right) to choose the folder to index.`
            );
            return;
          }
          await apiClient.saveDriveFolder(doc.id, doc.name);
          await apiClient.syncDriveFiles().catch(() => {});
          await load();
        });

      if (appId) {
        builder.setAppId(appId);
      }

      builder.build().setVisible(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open folder picker");
    } finally {
      setPickerBusy(false);
    }
  }

  async function disconnectDrive() {
    await apiClient.driveLogout();
    await load();
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);

  async function retryFile(id: string, name?: string, source?: string) {
    setBusy(true);
    try {
      await apiClient.retryDriveFile(id);
      if (source === "youtube") {
        trackYoutubeDownloads([{ driveFileId: id, name: name || id }]);
      } else {
        toast.success("Retry queued", { description: name || id });
      }
      await load();
      if (queueOpen) await loadQueue(queueStatus, queueOffset);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Retry failed";
      setError(msg);
      toast.error("Retry failed", { description: msg });
    } finally {
      setBusy(false);
    }
  }

  async function retryAllForReason(reason: string) {
    setRetryingReason(reason);
    setConfirmRetry(null);
    try {
      const res = await apiClient.retrySkippedByReason(reason);
      if (res.action === "unsupported") {
        toast.message(skipReasonMeta(reason).label, {
          description: res.message || "These files cannot be indexed.",
        });
      } else if (res.requeued > 0) {
        toast.success(
          res.action === "resume_paused" ? "Folders resumed" : "Requeued for indexing",
          {
            description:
              res.message ||
              `${formatCount(res.requeued)} file${res.requeued === 1 ? "" : "s"} queued`,
          }
        );
      } else {
        toast.message("Nothing to retry", {
          description: res.message || "No eligible files for this skip reason.",
        });
      }
      await load();
      if (queueOpen) await loadQueue(queueStatus, queueOffset);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Retry all failed";
      toast.error("Retry all failed", { description: msg });
    } finally {
      setRetryingReason(null);
    }
  }

  function requestRetryAll(reason: string, count: number) {
    const meta = skipReasonMeta(reason);
    if (!meta.retryable) {
      toast.message(meta.label, {
        description: "These skips cannot be requeued for indexing.",
      });
      return;
    }
    if (count > 100) {
      setConfirmRetry({ reason, count, label: meta.label });
      return;
    }
    void retryAllForReason(reason);
  }

  async function removeFile(id: string) {
    setBusy(true);
    try {
      await apiClient.removeDriveFile(id);
      await load();
      if (queueOpen) await loadQueue(queueStatus, queueOffset);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      setBusy(false);
    }
  }

  async function runIndex(reindex = false) {
    setBusy(true);
    try {
      await (reindex ? apiClient.triggerReindex() : apiClient.triggerIndex());
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Index failed");
    } finally {
      setBusy(false);
    }
  }

  async function addYoutubeVideos(urls: string[]) {
    setYoutubeBusy(true);
    setYoutubeMsg(null);
    const toastId = `yt-add-${Date.now()}`;
    toast.loading("Registering YouTube…", {
      id: toastId,
      description: `${urls.length} link(s)`,
      duration: Infinity,
    });
    try {
      const res = await apiClient.addYoutubeVideos(urls, true);
      const registered = res.registered.filter((r) => r.drive_file_id);
      const failed = res.registered.filter((r) => !r.drive_file_id);
      const downloads = registered.filter((r) => r.download_queued);

      if (!registered.length) {
        const why =
          failed.map((r) => r.message).filter(Boolean).join("; ") ||
          "No videos registered";
        toast.error("YouTube register failed", {
          id: toastId,
          description: why,
          duration: 12_000,
        });
        setYoutubeMsg(why);
        return;
      }

      if (downloads.length > 0 || res.index_scheduled) {
        trackYoutubeDownloads(
          registered.map((r) => ({
            driveFileId: r.drive_file_id,
            name: r.name || r.youtube_video_id || r.drive_file_id,
          })),
          { toastId },
        );
        setYoutubeMsg(null);
      } else {
        toast.success(`Registered ${registered.length} video(s)`, {
          id: toastId,
          description:
            failed.length > 0 ? `${failed.length} link(s) failed` : undefined,
          duration: 8_000,
        });
        setYoutubeMsg(null);
      }

      if (failed.length > 0 && registered.length > 0) {
        toast.warning(`${failed.length} link(s) failed`, {
          description: failed
            .map((r) => r.message || "unknown")
            .slice(0, 2)
            .join("; "),
          duration: 10_000,
        });
      }

      setYoutubeInput("");
      await load();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "YouTube feed failed";
      setError(msg);
      toast.error("YouTube feed failed", {
        id: toastId,
        description: msg,
        duration: 12_000,
      });
    } finally {
      setYoutubeBusy(false);
    }
  }

  async function feedYoutubeFromInput() {
    const urls = youtubeInput
      .split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!urls.length) return;
    await addYoutubeVideos(urls);
  }

  async function saveFolderContext(folderPath: string) {
    setSavingFolder(folderPath);
    try {
      await apiClient.upsertFolderContext(folderPath, editDescription);
      setEditingFolder(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSavingFolder(null);
    }
  }

  async function deleteContext(folderPath: string) {
    await apiClient.deleteFolderContext(folderPath);
    await load();
  }

  const uniqueFolders = useMemo(() => {
    const paths = new Set<string>(["/"]);
    for (const fc of folderContexts) {
      if (fc.folder_path) paths.add(fc.folder_path);
    }
    return Array.from(paths).sort();
  }, [folderContexts]);

  const contextByPath = useMemo(() => {
    const map: Record<string, FolderContext> = {};
    for (const fc of folderContexts) map[fc.folder_path] = fc;
    return map;
  }, [folderContexts]);

  const counts = status?.counts_by_status ?? {};
  const topSkipReasons = (skipStats?.by_reason ?? []).slice(0, 8);
  const maxSkipCount = Math.max(1, ...topSkipReasons.map((r) => r.count));
  const queuePageStart = queueTotal === 0 ? 0 : queueOffset + 1;
  const queuePageEnd = Math.min(queueOffset + QUEUE_PAGE_SIZE, queueTotal);

  return (
    <div className="space-y-6">
      <Script src="https://apis.google.com/js/api.js" strategy="lazyOnload" />

      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold">Folders</h2>
          <p className="text-sm text-muted-foreground">Drive files tracked from your connected folder</p>
        </div>
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          <Button className="w-full sm:w-auto" onClick={() => runIndex(false)} disabled={busy || status?.is_running}>
            {status?.is_running || busy ? <LoadingLabel>Indexing…</LoadingLabel> : "Start Index"}
          </Button>
          <Button className="w-full sm:w-auto" variant="secondary" onClick={() => runIndex(true)} disabled={busy || status?.is_running}>
            Reindex All
          </Button>
        </div>
      </div>

      <Card className={driveSession?.connected ? "border-green-800/50 bg-green-950/10" : "border-yellow-800/50 bg-yellow-950/10"}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium">
              {driveSession?.connected ? "Google Drive connected" : "Google Drive not connected"}
            </p>
            {driveSession?.connected ? (
              <p className="text-xs text-muted-foreground">
                {driveSession.email}
                {driveSession.selected_folder
                  ? ` · Folder: ${driveSession.selected_folder.name}`
                  : " · No folder selected yet"}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">Connect your Google account to start indexing Drive files.</p>
            )}
          </div>
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            {driveSession?.connected ? (
              <>
                <Button className="w-full sm:w-auto" onClick={openPicker} disabled={pickerBusy}>
                  {pickerBusy ? (
                    <LoadingLabel>Opening…</LoadingLabel>
                  ) : driveSession.selected_folder ? (
                    "Change folder"
                  ) : (
                    "Choose folder"
                  )}
                </Button>
                <Button className="w-full sm:w-auto" variant="secondary" onClick={disconnectDrive}>Disconnect</Button>
              </>
            ) : (
              <Button className="w-full sm:w-auto" onClick={() => window.location.href = `${API_BASE}/auth/google`}>
                Connect Google Drive
              </Button>
            )}
          </div>
        </div>
        {driveSession?.connected && settings && (
          <label className="mt-4 flex cursor-pointer items-start gap-3 border-t border-border pt-4 text-sm">
            <input
              type="checkbox"
              checked={settings.follow_shortcut_folders}
              disabled={settingsSaving}
              onChange={(e) => toggleShortcutFolders(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-background accent-blue-500"
            />
            <span>
              <span className="text-foreground">Pull folder shortcuts</span>
              <span className="mt-1 block text-xs text-muted-foreground">
                Index files inside shortcut folders like &quot;Master Folder&quot; and &quot;UG iPhone Data&quot; —
                not just physical subfolders.
              </span>
            </span>
          </label>
        )}
      </Card>

      <Card className="border-blue-900/40 bg-blue-950/10">
        <h3 className="mb-1 font-medium text-sm">YouTube videos</h3>
        <p className="mb-4 text-xs text-muted-foreground">
          Paste YouTube URLs or video IDs. Missing videos are downloaded with yt-dlp to the shared
          Railway volume (team library — not your personal Drive). Then indexed: transcript, frames,
          and visual search. Videos already in the company Drive folder are linked automatically.
        </p>

        <div className="mb-4 grid gap-2 sm:grid-cols-3">
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">1 · Add URL</p>
            <p className="mt-1 text-xs text-muted-foreground">Paste links below and download to the library.</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">2 · Index</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {status?.is_running ? "Indexing now…" : "Use Start Index or wait for auto-sync."}
            </p>
          </div>
          <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">3 · Search</p>
            <div className="mt-1 flex flex-wrap gap-2 text-xs">
              <Link href="/search" className="text-sky-600 underline-offset-2 hover:underline dark:text-sky-400">
                Search
              </Link>
              <span className="text-muted-foreground">·</span>
              <Link href="/search/carousel" className="text-sky-600 underline-offset-2 hover:underline dark:text-sky-400">
                Video Carousel
              </Link>
            </div>
          </div>
        </div>

        <textarea
          className="min-h-[88px] w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          placeholder="https://youtube.com/watch?v=VIDEO_ID"
          value={youtubeInput}
          onChange={(e) => setYoutubeInput(e.target.value)}
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button onClick={feedYoutubeFromInput} disabled={youtubeBusy || !youtubeInput.trim()}>
            {youtubeBusy ? "Working…" : "Download to library & index"}
          </Button>
        </div>
        {youtubeMsg && <p className="mt-3 text-xs text-zinc-400">{youtubeMsg}</p>}
      </Card>

      {error && <Card className="border-destructive/50 text-destructive">{error}</Card>}

      <Card className={status?.is_running ? "border-blue-800 bg-blue-950/20" : ""}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-medium">
              {status?.is_running && <Spinner size={14} />}
              {status?.is_running ? "Indexing in progress" : "Indexer status"}
            </p>
            {status?.is_running ? (
              <div className="mt-1 space-y-0.5 text-sm text-muted-foreground">
                <p>
                  Images {(status.image_slots?.active ?? status.active_image_jobs ?? 0)}/
                  {(status.image_slots?.max ?? "—")}
                  {" · "}
                  {(status.current_image_files?.length
                    ? status.current_image_files.slice(0, 3).join(" · ")
                    : "idle")}
                  {(status.current_image_files?.length ?? 0) > 3
                    ? ` (+${(status.current_image_files?.length ?? 0) - 3})`
                    : ""}
                </p>
                <p>
                  Videos {(status.video_slots?.active ?? status.active_video_jobs ?? 0)}/
                  {(status.video_slots?.max ?? "—")}
                  {" · "}
                  {(status.current_video_files?.length
                    ? status.current_video_files.slice(0, 3).join(" · ")
                    : "idle")}
                  {(status.current_video_files?.length ?? 0) > 3
                    ? ` (+${(status.current_video_files?.length ?? 0) - 3})`
                    : ""}
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                {status?.auto_index_enabled
                  ? `Auto-sync every ${status.auto_index_interval_seconds}s`
                  : "Click Start Index or enable auto-index in Settings"}
                {status?.last_run_at ? ` · last sync ${formatDate(status.last_run_at)}` : ""}
              </p>
            )}
            {status?.go_indexer_enabled && (
              <p className="mt-1 text-xs text-muted-foreground">
                Go canary:{" "}
                <span className={status.go_indexer_alive ? "text-emerald-600 dark:text-emerald-400" : ""}>
                  {status.go_indexer_alive ? "active" : "waiting for sidecar"}
                </span>
                {status.go_files_per_sec != null && status.go_files_per_sec > 0
                  ? ` · ${status.go_files_per_sec.toFixed(2)} files/sec last run`
                  : ""}
              </p>
            )}
          </div>
          <Button
            variant="secondary"
            onClick={() => {
              setQueueOffset(0);
              setQueueOpen(true);
            }}
          >
            View Queue
          </Button>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 text-sm sm:grid-cols-5">
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-xs text-muted-foreground">Pending</p>
            <p className="text-lg font-semibold text-amber-600 dark:text-yellow-400">{counts.pending ?? 0}</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-xs text-muted-foreground">Active</p>
            <p className="text-lg font-semibold text-blue-600 dark:text-blue-400">{counts.processing ?? 0}</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-xs text-muted-foreground">Completed</p>
            <p className="text-lg font-semibold text-emerald-600 dark:text-green-400">{counts.processed ?? 0}</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-xs text-muted-foreground">Failed</p>
            <p className="text-lg font-semibold text-red-600 dark:text-red-400">{counts.error ?? 0}</p>
          </div>
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-xs text-muted-foreground">Skipped</p>
            <p className="text-lg font-semibold text-muted-foreground">{counts.skipped ?? skipStats?.total_skipped ?? 0}</p>
          </div>
        </div>
        {(topSkipReasons.length > 0 || status?.last_run) && (
          <div className="mt-4 border-t border-border/50 pt-4">
            {topSkipReasons.length > 0 && (
              <>
                <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium text-foreground">Top skip reasons</p>
                    <p className="text-xs text-muted-foreground">
                      Why files were skipped
                      {skipStats?.total_skipped != null
                        ? ` · ${formatCount(skipStats.total_skipped)} total`
                        : ""}
                      {" · "}retry a reason to requeue those files
                    </p>
                  </div>
                </div>
                <ul className="divide-y divide-border/50 overflow-hidden rounded-lg border border-border/60 bg-muted/10">
                  {topSkipReasons.map((r) => {
                    const meta = skipReasonMeta(r.reason);
                    const pct = Math.max(6, Math.round((r.count / maxSkipCount) * 100));
                    const rowBusy = retryingReason === r.reason;
                    const anyBusy = retryingReason != null;
                    return (
                      <li
                        key={r.reason}
                        className="relative px-3 py-2.5 transition-colors hover:bg-muted/25"
                      >
                        <div
                          className="pointer-events-none absolute inset-y-0 left-0 bg-muted-foreground/10"
                          style={{ width: `${pct}%` }}
                          aria-hidden
                        />
                        <div className="relative flex flex-wrap items-center gap-2 sm:gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                              <p className="truncate text-sm font-medium text-foreground">
                                {meta.label}
                              </p>
                              <span className="shrink-0 rounded-md bg-background/80 px-1.5 py-0.5 text-xs font-semibold tabular-nums text-foreground ring-1 ring-border/60">
                                {formatCount(r.count)}
                              </span>
                            </div>
                            <p className="mt-0.5 truncate text-xs text-muted-foreground">
                              {meta.hint}
                            </p>
                          </div>
                          {meta.retryable ? (
                            <Button
                              variant="secondary"
                              className="shrink-0 px-2.5 py-1.5 text-xs"
                              disabled={anyBusy || busy}
                              onClick={() => requestRetryAll(r.reason, r.count)}
                            >
                              {rowBusy ? (
                                <LoadingLabel>{meta.retryLabel}…</LoadingLabel>
                              ) : (
                                meta.retryLabel
                              )}
                            </Button>
                          ) : (
                            <span
                              className="shrink-0 rounded-md px-2 py-1 text-[11px] text-muted-foreground"
                              title={meta.hint}
                            >
                              {meta.retryLabel}
                            </span>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
            {status?.last_run && (
              <p className={`text-xs text-muted-foreground ${topSkipReasons.length > 0 ? "mt-3" : ""}`}>
                Last completed run: {formatCount(status.last_run.discovered)} discovered ·{" "}
                {formatCount(status.last_run.processed)} processed · {formatCount(status.last_run.skipped)}{" "}
                skipped · {formatCount(status.last_run.errored)} errors
              </p>
            )}
          </div>
        )}
      </Card>

      {(indexErrorTotal > 0 || indexErrorItems.length > 0) && (
        <Card className="border-red-900/40 bg-red-950/10">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-medium">Indexing Errors</h3>
              <p className="text-xs text-muted-foreground">
                {formatCount(indexErrorTotal)} file{indexErrorTotal === 1 ? "" : "s"} failed · showing{" "}
                {indexErrorItems.length}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => {
                setQueueStatus("error");
                setQueueOffset(0);
                setQueueOpen(true);
              }}
            >
              Open in queue
            </Button>
          </div>
          <div className="max-h-[min(50dvh,22rem)] space-y-2 overflow-y-auto pr-1">
            {indexErrorItems.map((f) => (
              <IndexErrorCard
                key={f.id}
                name={f.name}
                path={f.path}
                errorMessage={f.error_message}
                busy={busy}
                onRetry={() => retryFile(f.id, f.name, f.source)}
                onDismiss={() => removeFile(f.id)}
              />
            ))}
          </div>
        </Card>
      )}

      {uniqueFolders.length > 0 && (
        <Card>
          <h3 className="mb-3 font-medium text-sm">Folder Search Context</h3>
          <p className="mb-4 text-xs text-muted-foreground">
            Add a description to a folder to improve search accuracy. The description is embedded with
            Gemini and used to scope and verify search results.
          </p>
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
                        placeholder="Describe this folder (e.g. 'Birthday party videos 2024, outdoor events')"
                        value={editDescription}
                        onChange={(e) => setEditDescription(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && saveFolderContext(fp)}
                      />
                      <div className="flex gap-2">
                        <Button onClick={() => saveFolderContext(fp)} disabled={isSaving}>
                          {isSaving ? <LoadingLabel>Saving…</LoadingLabel> : "Save & embed"}
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
          <div className="flex flex-wrap gap-1 border-b border-border px-3 py-2">
            {QUEUE_STATUS_TABS.map((tab) => (
              <button
                key={tab.value || "all"}
                type="button"
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
              </button>
            ))}
          </div>
          <div className="max-h-[min(60dvh,28rem)] overflow-auto">
            {queueLoading ? (
              <p className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <Spinner size={14} /> Loading…
              </p>
            ) : queueItems.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">No files in this filter.</p>
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
                            {f.source === "youtube" ? "YouTube" : "Drive"}
                          </span>
                        </td>
                        <td className={`p-3 ${statusColor[f.status] ?? ""}`}>{f.status}</td>
                        <td
                          className="max-w-[16rem] p-3 text-xs text-red-700 dark:text-red-300"
                          title={friendlyErr?.details ?? friendlyErr?.summary ?? ""}
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

      <ConfirmDialog
        open={confirmRetry != null}
        title={`Retry ${confirmRetry?.label ?? "skipped files"}?`}
        message={`This will requeue ${formatCount(confirmRetry?.count ?? 0)} file${
          (confirmRetry?.count ?? 0) === 1 ? "" : "s"
        }. Large batches can keep the indexer busy for a while.`}
        confirmLabel="Retry all"
        variant="primary"
        onCancel={() => setConfirmRetry(null)}
        onConfirm={() => {
          if (confirmRetry) void retryAllForReason(confirmRetry.reason);
        }}
      />
    </div>
  );
}
