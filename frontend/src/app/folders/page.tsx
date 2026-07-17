"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import Script from "next/script";
import { apiClient, type FolderContext, type DriveFile, type IndexStatus, type DriveSession, type Settings, API_BASE } from "@/lib/api";
import { Button, Card, Input, LoadingLabel, Spinner } from "@/components/ui";
import { formatDate } from "@/lib/utils";

declare global {
  interface Window {
    gapi: any;
    google: any;
    _pickerApiLoaded?: boolean;
  }
}

export default function FoldersPage() {
  const [files, setFiles] = useState<DriveFile[]>([]);
  const [status, setStatus] = useState<IndexStatus | null>(null);
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

  async function load() {
    try {
      const [f, s, fc, ds, st] = await Promise.all([
        apiClient.driveFiles(),
        apiClient.indexStatus(),
        apiClient.folderContexts().catch(() => [] as FolderContext[]),
        apiClient.driveSession().catch(() => null as DriveSession | null),
        apiClient.settings().catch(() => null as Settings | null),
      ]);
      setFiles(f);
      setStatus(s);
      setFolderContexts(Array.isArray(fc) ? fc : []);
      setDriveSession(ds);
      setSettings(st);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }

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

      // setEnableDrives(true) on a view shows ONLY shared drives (hides My Drive).
      // Use separate tabs so both My Drive folders and shared drives are available.
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

  async function retryFile(id: string) {
    setBusy(true);
    try {
      await apiClient.retryDriveFile(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Retry failed");
    } finally {
      setBusy(false);
    }
  }

  async function removeFile(id: string) {
    setBusy(true);
    try {
      await apiClient.removeDriveFile(id);
      await load();
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
    try {
      const res = await apiClient.addYoutubeVideos(urls, true);
      const ok = res.registered.filter((r) => r.drive_file_id).length;
      const downloads = res.registered.filter((r) => r.download_queued).length;
      setYoutubeMsg(
        res.index_scheduled
          ? downloads > 0
            ? `Downloading ${downloads} video(s) to shared library + indexing.`
            : `Queued ${ok} video(s) for full index.`
          : `Registered ${ok} video(s).`
      );
      setYoutubeInput("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "YouTube feed failed");
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
    const paths = new Set<string>();
    // Always include the root (connected folder)
    paths.add("/");
    for (const f of files) {
      const parts = f.path.split("/").filter(Boolean);
      if (parts.length > 1) {
        parts.pop();
        paths.add("/" + parts.join("/"));
      }
    }
    return Array.from(paths).sort();
  }, [files]);

  const contextByPath = useMemo(() => {
    const map: Record<string, FolderContext> = {};
    for (const fc of folderContexts) map[fc.folder_path] = fc;
    return map;
  }, [folderContexts]);

  const statusColor: Record<string, string> = {
    pending: "text-amber-600 dark:text-yellow-400",
    processing: "text-blue-600 dark:text-blue-400",
    processed: "text-emerald-600 dark:text-green-400",
    error: "text-red-600 dark:text-red-400",
    skipped: "text-muted-foreground",
  };

  return (
    <div className="space-y-6">
      {/* Load Google Picker API */}
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

      {/* Drive connection card */}
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

      {error && <Card className="border-destructive/50 text-destructive">{error}</Card>}

      <Card className={status?.is_running ? "border-blue-800 bg-blue-950/20" : ""}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-medium">
              {status?.is_running && <Spinner size={14} />}
              {status?.is_running ? "Indexing in progress" : "Indexer status"}
            </p>
            {status?.is_running && status.current_file ? (
              <p className="text-sm text-muted-foreground">Now processing: {status.current_file}</p>
            ) : (
              <p className="text-sm text-muted-foreground">
                {status?.auto_index_enabled
                  ? `Auto-sync every ${status.auto_index_interval_seconds}s`
                  : "Click Start Index or enable auto-index in Settings"}
                {status?.last_run_at ? ` · last sync ${formatDate(status.last_run_at)}` : ""}
              </p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 text-sm sm:flex sm:gap-4">
            <span className="text-emerald-600 dark:text-green-400">{status?.counts_by_status.processed ?? 0} processed</span>
            <span className="text-yellow-400">{status?.counts_by_status.pending ?? 0} pending</span>
            <span className="text-blue-400">{status?.counts_by_status.processing ?? 0} active</span>
            <span className="text-red-400">{status?.counts_by_status.error ?? 0} errors</span>
          </div>
        </div>
        {status?.last_run && (
          <p className="mt-3 text-xs text-muted-foreground">
            Last completed run: {status.last_run.discovered} discovered · {status.last_run.processed} processed ·{" "}
            {status.last_run.skipped} skipped · {status.last_run.errored} errors
          </p>
        )}
      </Card>

      <Card className="border-blue-900/40 bg-blue-950/10">
        <h3 className="mb-1 font-medium text-sm">YouTube videos</h3>
        <p className="mb-4 text-xs text-muted-foreground">
          Paste YouTube URLs or video IDs. Missing videos are downloaded with yt-dlp to the shared
          Railway volume (team library — not your personal Drive). Then indexed: transcript, frames,
          and visual search. Videos already in the company Drive folder are linked automatically.
        </p>
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
        {youtubeMsg && <p className="mt-3 text-xs text-blue-300">{youtubeMsg}</p>}
      </Card>

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

      <Card className="overflow-x-auto p-0">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-border text-muted-foreground">
            <tr>
              <th className="p-3">Name</th>
              <th className="p-3">Source</th>
              <th className="p-3">Path</th>
              <th className="p-3">Type</th>
              <th className="p-3">Status</th>
              <th className="p-3">Error</th>
              <th className="p-3">Synced</th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => {
              const canRetry = f.status === "error";
              const canRemove = f.status === "error" || f.error_message?.includes("404");
              return (
              <tr key={f.id} className="border-b border-border/50 hover:bg-muted/30">
                <td className="p-3 font-medium">{f.name}</td>
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
                <td className="p-3 text-muted-foreground">{f.path}</td>
                <td className="p-3 text-muted-foreground">{f.mime_type}</td>
                <td className={`p-3 ${statusColor[f.status] ?? ""}`}>{f.status}</td>
                <td className="max-w-xs p-3 text-xs">
                  {f.error_message ? (
                    <div className="space-y-2">
                      <p className="truncate text-destructive" title={f.error_message}>
                        {f.error_message}
                      </p>
                      {(canRetry || canRemove) && (
                        <div className="flex flex-wrap gap-2">
                          {canRetry && (
                            <Button variant="secondary" onClick={() => retryFile(f.id)} disabled={busy}>
                              Retry
                            </Button>
                          )}
                          {canRemove && (
                            <Button variant="secondary" onClick={() => removeFile(f.id)} disabled={busy}>
                              Remove
                            </Button>
                          )}
                        </div>
                      )}
                    </div>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="p-3 text-muted-foreground">{f.last_synced_at ? formatDate(f.last_synced_at) : "—"}</td>
              </tr>
            );
            })}
          </tbody>
        </table>
        {files.length === 0 && (
          <p className="p-4 text-sm text-muted-foreground">
            No files synced yet. Connect Drive, choose a folder, and files will appear automatically within ~30 seconds.
          </p>
        )}
      </Card>
    </div>
  );
}
