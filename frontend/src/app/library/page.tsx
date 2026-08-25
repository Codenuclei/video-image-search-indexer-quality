"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileImage,
  FileVideo,
  Folder,
  Pause,
  Play,
  RefreshCw,
  XCircle,
} from "lucide-react";
import {
  apiClient,
  driveFileDownloadUrl,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  driveVideoStreamUrl,
  type LibraryFile,
  type LibraryFolder,
  type LibraryResponse,
} from "@/lib/api";
import { Button, Card, DownloadButton, IconLink, Input, LoadingLabel, Spinner, StatCard } from "@/components/ui";
import { ManualFaceTagger } from "@/components/manual-face-tagger";
import { humanizeIndexError } from "@/lib/index-errors";
import { cn } from "@/lib/utils";
import { readCache, writeCache, hydrateKeyFromDisk } from "@/lib/data-cache";

type FilterMode = "all" | "processed" | "failed" | "skipped" | "archived" | "missing_caption" | "missing_embed";

const SHELL_CACHE_KEY = "driveLibraryShell";
const FOLDER_CACHE_PREFIX = "driveLibrary:folder:";
const FOLDER_PAGE_SIZE = 150;

function folderCacheKey(path: string) {
  return `${FOLDER_CACHE_PREFIX}${path || "/"}`;
}

function isFailedLibraryFile(f: LibraryFile): boolean {
  if (f.status === "error") return true;
  const msg = (f.error_message || "").toLowerCase();
  return (
    msg.includes("corrupt") ||
    msg.includes("decode") ||
    msg.includes("decode_exhausted") ||
    msg.includes("corrupt_file")
  );
}

/** Shared header + row template so Name / Index / Caption / Embed / Size stay aligned. */
const FILE_TABLE_COLS =
  "grid w-full grid-cols-[minmax(0,1fr)_7rem] items-center gap-x-2 sm:grid-cols-[minmax(0,1fr)_7.5rem_5rem_5rem_5.5rem]";

function folderDisplayName(folder: LibraryFolder | null, path: string): string {
  if (!folder || path === "/") return "Drive root";
  return folder.name;
}

function formatBytes(n: number | null) {
  if (n == null || n <= 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    processed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
    pending: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
    processing: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
    error: "bg-red-500/15 text-red-700 dark:text-red-400",
    skipped: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400",
    archived: "bg-slate-500/15 text-slate-600 dark:text-slate-400",
  };
  return map[status] ?? "bg-muted text-muted-foreground";
}

function findFolder(node: LibraryFolder, path: string): LibraryFolder | null {
  if (node.path === path) return node;
  for (const child of node.folders) {
    const hit = findFolder(child, path);
    if (hit) return hit;
  }
  return null;
}

function FolderTreeItem({
  folder,
  selectedPath,
  expanded,
  onSelect,
  onToggle,
  onPause,
  onResume,
  actionBusy,
  depth = 0,
}: {
  folder: LibraryFolder;
  selectedPath: string;
  expanded: Set<string>;
  onSelect: (path: string) => void;
  onToggle: (path: string) => void;
  onPause: (path: string) => void;
  onResume: (path: string) => void;
  actionBusy: string | null;
  depth?: number;
}) {
  const isOpen = expanded.has(folder.path);
  const isSelected = selectedPath === folder.path;

  return (
    <div>
      <div
        className={cn(
          "group flex w-full items-center gap-1 rounded-md px-2 py-1.5 text-sm transition-colors",
          isSelected ? "bg-primary/15 text-primary font-medium" : "hover:bg-muted/60 text-foreground"
        )}
        style={{ paddingLeft: `${8 + depth * 14}px` }}
      >
        {folder.folders.length > 0 ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggle(folder.path);
            }}
            className="shrink-0 text-muted-foreground"
          >
            {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <span className="w-3.5 shrink-0" />
        )}
        <button
          type="button"
          onClick={() => onSelect(folder.path)}
          className="flex min-w-0 flex-1 items-center gap-1 text-left"
        >
          <Folder size={14} className={cn("shrink-0", folder.indexing_paused ? "text-muted-foreground" : "text-amber-500")} />
          <span className="min-w-0 flex-1 truncate">{folder.name}</span>
          <span className="shrink-0 text-[10px] text-muted-foreground">{folder.file_count}</span>
        </button>
        {folder.path !== "/" && (
          <button
            type="button"
            title={folder.indexing_paused ? "Resume indexing" : "Stop indexing this folder"}
            disabled={actionBusy === folder.path}
            onClick={(e) => {
              e.stopPropagation();
              if (folder.indexing_paused) onResume(folder.path);
              else onPause(folder.path);
            }}
            className="shrink-0 rounded p-0.5 text-muted-foreground transition-opacity hover:bg-muted hover:text-foreground"
          >
            {actionBusy === folder.path ? (
              <Spinner size={12} />
            ) : folder.indexing_paused ? (
              <Play size={12} />
            ) : (
              <Pause size={12} />
            )}
          </button>
        )}
      </div>
      {folder.indexing_paused && (
        <p
          className="px-2 pb-1 text-[10px] text-amber-600 dark:text-amber-400"
          style={{ paddingLeft: `${22 + depth * 14}px` }}
        >
          Indexing paused
        </p>
      )}
      {isOpen &&
        folder.folders.map((child) => (
          <FolderTreeItem
            key={child.path}
            folder={child}
            selectedPath={selectedPath}
            expanded={expanded}
            onSelect={onSelect}
            onToggle={onToggle}
            onPause={onPause}
            onResume={onResume}
            actionBusy={actionBusy}
            depth={depth + 1}
          />
        ))}
    </div>
  );
}

function ExpandableLibraryImage({
  fileId,
  mimeType,
  name,
}: {
  fileId: string;
  mimeType: string;
  name: string;
}) {
  void mimeType;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={driveFileThumbnailUrl(fileId)}
      alt={name}
      className="w-full rounded-lg border border-border object-cover"
    />
  );
}

function FileRow({
  file,
  selected,
  onSelect,
}: {
  file: LibraryFile;
  selected: boolean;
  onSelect: () => void;
}) {
  const Icon = file.is_video ? FileVideo : file.is_image ? FileImage : Folder;

  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        FILE_TABLE_COLS,
        "border-b border-border px-3 py-2 text-left text-sm transition-colors",
        selected ? "bg-primary/10" : "hover:bg-muted/40"
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <Icon size={14} className="shrink-0 text-muted-foreground" />
        <span className="truncate font-medium">{file.name}</span>
      </span>
      <span className="flex min-w-0 items-center justify-center">
        <span className={cn("max-w-full truncate rounded px-1.5 py-0.5 text-center text-[10px] font-medium uppercase", statusBadge(file.status))}>
          {file.status}
        </span>
      </span>
      <span className="hidden items-center justify-center sm:flex">
        {file.is_image ? (
          file.has_caption ? (
            <CheckCircle2 size={14} className="text-emerald-500" />
          ) : file.status === "processed" ? (
            <XCircle size={14} className="text-amber-500" />
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        ) : (
          <span className="text-muted-foreground">n/a</span>
        )}
      </span>
      <span className="hidden items-center justify-center sm:flex">
        {file.is_image ? (
          file.has_embedding ? (
            <CheckCircle2 size={14} className="text-emerald-500" />
          ) : file.status === "processed" ? (
            <XCircle size={14} className="text-amber-500" />
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        ) : (
          <span className="text-muted-foreground">n/a</span>
        )}
      </span>
      <span className="hidden items-center justify-center text-xs text-muted-foreground sm:flex">{formatBytes(file.size)}</span>
    </button>
  );
}

export default function LibraryPage() {
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedFolderPath, setSelectedFolderPath] = useState("/");
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["/"]));
  const [filter, setFilter] = useState<FilterMode>("all");
  const [search, setSearch] = useState("");
  const [folderActionBusy, setFolderActionBusy] = useState<string | null>(null);
  const [manualFaceTag, setManualFaceTag] = useState(false);
  const [folderFiles, setFolderFiles] = useState<LibraryFile[]>([]);
  const [folderFilesLoading, setFolderFilesLoading] = useState(false);
  const [folderNextCursor, setFolderNextCursor] = useState<string | null>(null);
  const [folderTotal, setFolderTotal] = useState(0);

  const loadShell = useCallback(async (force = false) => {
    try {
      if (!force) {
        const revRes = await apiClient.driveLibraryRevision().catch(() => null);
        const rev = revRes?.revision ?? null;
        const prev = readCache<LibraryResponse>(SHELL_CACHE_KEY);
        if (
          rev &&
          prev?.revision === rev &&
          prev.data &&
          prev.data.summary?.caption_stats_ready
        ) {
          setData(prev.data);
          setLoading(false);
          return;
        }
      }
      const shell = await apiClient.driveLibraryShell();
      const writeRev = shell.revision ?? String(Date.now());
      writeCache(SHELL_CACHE_KEY, shell, writeRev, true);
      setData(shell);
    } catch {
      /* api() already toasted */
    } finally {
      setLoading(false);
    }
  }, []);

  const loadFolderFiles = useCallback(
    async (path: string, opts?: { append?: boolean; cursor?: string | null; force?: boolean }) => {
      if (!path || path === "/") {
        setFolderFiles([]);
        setFolderNextCursor(null);
        setFolderTotal(0);
        setFolderFilesLoading(false);
        return;
      }
      const key = folderCacheKey(path);
      if (!opts?.append && !opts?.force) {
        const fromDisk = hydrateKeyFromDisk(key) as
          | import("@/lib/data-cache").CacheEntry<{
              files: LibraryFile[];
              next_cursor: string | null;
              total: number;
            }>
          | null;
        const cached =
          fromDisk ??
          readCache<{ files: LibraryFile[]; next_cursor: string | null; total: number }>(key);
        const shellRev = readCache<LibraryResponse>(SHELL_CACHE_KEY)?.revision;
        if (cached?.data && shellRev && cached.revision === shellRev) {
          setFolderFiles(cached.data.files);
          setFolderNextCursor(cached.data.next_cursor);
          setFolderTotal(cached.data.total);
          setFolderFilesLoading(false);
          return;
        }
      }
      setFolderFilesLoading(true);
      try {
        const page = await apiClient.driveLibraryFolder(path, {
          limit: FOLDER_PAGE_SIZE,
          cursor: opts?.append ? opts.cursor ?? null : null,
        });
        setFolderFiles((prev) => {
          const next = opts?.append ? [...prev, ...page.files] : page.files;
          writeCache(
            key,
            { files: next, next_cursor: page.next_cursor, total: page.total },
            page.revision ?? String(Date.now()),
            true
          );
          return next;
        });
        setFolderNextCursor(page.next_cursor);
        setFolderTotal(page.total);
      } catch {
        if (!opts?.append) setFolderFiles([]);
      } finally {
        setFolderFilesLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    const cached = hydrateKeyFromDisk(SHELL_CACHE_KEY) as import("@/lib/data-cache").CacheEntry<LibraryResponse> | null;
    if (cached?.data) {
      setData(cached.data);
      setLoading(false);
    }

    let cancelled = false;
    async function tick(force = false) {
      try {
        const revRes = await apiClient.driveLibraryRevision().catch(() => null);
        if (cancelled) return;
        const rev = revRes?.revision ?? null;
        const prev = readCache<LibraryResponse>(SHELL_CACHE_KEY);
        if (!force && rev && prev?.revision === rev && prev.data && prev.data.summary?.caption_stats_ready) {
          setData(prev.data);
          setLoading(false);
          return;
        }
        const shell = await apiClient.driveLibraryShell();
        if (cancelled) return;
        const writeRev = shell.revision ?? rev ?? String(Date.now());
        writeCache(SHELL_CACHE_KEY, shell, writeRev, true);
        setData(shell);
        setLoading(false);
      } catch {
        if (!cancelled) setLoading(false);
      }
    }

    void tick(false);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setSelectedFileId(null);
    void loadFolderFiles(selectedFolderPath, { force: false });
  }, [selectedFolderPath, loadFolderFiles]);

  useEffect(() => {
    apiClient
      .settings()
      .then((s) => setManualFaceTag(Boolean(s.experimental_manual_face_tag)))
      .catch(() => setManualFaceTag(false));
  }, []);

  const selectedFolder = useMemo(() => {
    if (!data?.tree) return null;
    return findFolder(data.tree, selectedFolderPath) ?? data.tree;
  }, [data, selectedFolderPath]);

  const filteredFiles = useMemo(() => {
    let files = folderFiles;
    const q = search.trim().toLowerCase();
    if (q) files = files.filter((f) => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));
    if (filter === "missing_caption") {
      files = files.filter((f) => f.is_image && f.status === "processed" && !f.has_caption);
    } else if (filter === "missing_embed") {
      files = files.filter((f) => f.is_image && f.status === "processed" && !f.has_embedding);
    } else if (filter === "failed") {
      files = files.filter((f) => isFailedLibraryFile(f));
    } else if (filter === "processed") {
      files = files.filter((f) => f.status === "processed");
    } else if (filter === "skipped") {
      files = files.filter((f) => f.status === "skipped");
    } else if (filter === "archived") {
      files = files.filter((f) => f.status === "archived");
    }
    return files;
  }, [folderFiles, filter, search]);

  const selectedFile = useMemo(() => {
    if (!selectedFileId) return null;
    return folderFiles.find((f) => f.id === selectedFileId) ?? null;
  }, [selectedFileId, folderFiles]);

  const maintenance = data?.maintenance;
  const summary = data?.summary;
  const captionStatsReady = Boolean(summary?.caption_stats_ready);
  const backfillActive = maintenance?.caption_backfill_running || maintenance?.embed_backfill_running;

  function toggleExpand(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function selectFolder(path: string) {
    if (path === selectedFolderPath) return;
    setSelectedFileId(null);
    setFolderFiles([]);
    setFolderNextCursor(null);
    setFolderTotal(0);
    setFolderFilesLoading(path !== "/");
    setSelectedFolderPath(path);
  }

  async function pauseFolder(path: string) {
    setFolderActionBusy(path);
    try {
      await apiClient.pauseFolderIndexing(path);
      await loadShell(true);
      await loadFolderFiles(selectedFolderPath, { force: true });
    } catch {
      /* api() already toasted */
    } finally {
      setFolderActionBusy(null);
    }
  }

  async function resumeFolder(path: string) {
    setFolderActionBusy(path);
    try {
      await apiClient.resumeFolderIndexing(path);
      await loadShell(true);
      await loadFolderFiles(selectedFolderPath, { force: true });
    } catch {
      /* api() already toasted */
    } finally {
      setFolderActionBusy(null);
    }
  }

  return (
    <div className="space-y-4 pb-20 md:pb-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Media Library</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Folder-wise view of all historically indexed files (global — not limited to the
            current Drive session)
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              setLoading(true);
              void loadShell(true);
              void loadFolderFiles(selectedFolderPath, { force: true });
            }}
            disabled={loading}
          >
            <RefreshCw size={14} className={cn("mr-1.5 inline", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>
      </div>

      {backfillActive && (
        <Card className="border-primary/30 bg-primary/5">
          <div className="flex items-center gap-2 text-sm text-primary">
            <LoadingLabel size={16} className="text-primary">
              Auto backfill running
              {maintenance?.caption_backfill_running && " · captions"}
              {maintenance?.embed_backfill_running && " · embeddings"}
              {" — resumes automatically after deploys"}
            </LoadingLabel>
          </div>
        </Card>
      )}

      {summary && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <StatCard label="Total files" value={summary.total_files} />
          <StatCard label="Images" value={summary.images} hint={`${summary.videos} videos`} />
          {captionStatsReady ? (
            <>
              <StatCard
                label="Captioned"
                value={`${summary.captioned}/${summary.images}`}
                hint={`${summary.caption_pct}% complete`}
              />
              <StatCard label="Embedded" value={`${summary.embedded}/${summary.images}`} />
            </>
          ) : (
            <>
              <StatCard label="Pending" value={summary.pending} hint="Open a folder for file names" />
              <StatCard label="Errors" value={summary.errors} />
            </>
          )}
          <StatCard
            label="Failed"
            value={
              selectedFolderPath !== "/"
                ? folderFiles.filter((f) => isFailedLibraryFile(f)).length
                : summary.errors
            }
            hint="Corrupt + failed to decode (merged)"
          />
        </div>
      )}

      <div className="flex min-h-[520px] flex-col lg:flex-row">
        <aside className="w-full shrink-0 border-b border-border lg:w-64 lg:border-b-0 lg:border-r">
          <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Folders
          </div>
          <div className="scrollbar-hidden max-h-48 overflow-y-auto p-2 lg:max-h-[calc(100vh-18rem)]">
            {data?.tree && (
              <FolderTreeItem
                folder={data.tree}
                selectedPath={selectedFolderPath}
                expanded={expanded}
                onSelect={selectFolder}
                onToggle={toggleExpand}
                onPause={pauseFolder}
                onResume={resumeFolder}
                actionBusy={folderActionBusy}
              />
            )}
          </div>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col">
          <div className="space-y-2 border-b border-border px-3 py-2">
            <p className="min-w-0 text-sm font-medium">
              <span className="text-foreground">
                {selectedFolderPath === "/"
                  ? "Select a folder"
                  : folderDisplayName(selectedFolder, selectedFolderPath)}
              </span>
              {selectedFolderPath !== "/" && (
                <span className="ml-2 text-muted-foreground">
                  ({filteredFiles.length}
                  {folderTotal > filteredFiles.length ? ` of ${folderTotal}` : ""} file
                  {filteredFiles.length === 1 ? "" : "s"})
                </span>
              )}
            </p>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
              <select
                value={filter}
                onChange={(e) => setFilter(e.target.value as FilterMode)}
                aria-label="Filter by status"
                className="h-9 w-full min-w-0 basis-1/2 rounded-md border border-input bg-background px-2.5 text-xs sm:flex-1"
              >
                <option value="all">All files</option>
                <option value="processed">Ready</option>
                <option value="failed">Failed (corrupt / decode)</option>
                <option value="skipped">Skipped</option>
                <option value="archived">Archived</option>
                <option value="missing_caption">Missing caption</option>
                <option value="missing_embed">Missing embed</option>
              </select>
              <Input
                placeholder="Search in folder…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-9 w-full min-w-0 basis-1/2 py-0 text-xs sm:flex-1"
                disabled={selectedFolderPath === "/"}
              />
            </div>
          </div>

          <div className={cn(FILE_TABLE_COLS, "hidden border-b border-border bg-muted/30 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground sm:grid")}>
            <span className="min-w-0">Name</span>
            <span className="flex items-center justify-center">Index</span>
            <span className="flex items-center justify-center">Caption</span>
            <span className="flex items-center justify-center">Embed</span>
            <span className="flex items-center justify-center">Size</span>
          </div>

          <div className="scrollbar-hidden min-h-0 flex-1 overflow-y-auto">
            {selectedFolderPath === "/" ? (
              <p className="py-16 text-center text-sm text-muted-foreground">
                Choose a folder on the left to load file names
              </p>
            ) : loading && !data ? (
              <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
                <LoadingLabel size={18}>Loading library…</LoadingLabel>
              </div>
            ) : folderFilesLoading && folderFiles.length === 0 ? (
              <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
                <LoadingLabel size={18}>Loading files…</LoadingLabel>
              </div>
            ) : filteredFiles.length === 0 ? (
              <p className="py-16 text-center text-sm text-muted-foreground">No files in this view</p>
            ) : (
              <>
                {filteredFiles.map((file) => (
                  <FileRow
                    key={file.id}
                    file={file}
                    selected={selectedFileId === file.id}
                    onSelect={() => setSelectedFileId(file.id)}
                  />
                ))}
                {folderNextCursor && (
                  <div className="flex justify-center border-t border-border p-3">
                    <Button
                      variant="secondary"
                      disabled={folderFilesLoading}
                      onClick={() =>
                        void loadFolderFiles(selectedFolderPath, {
                          append: true,
                          cursor: folderNextCursor,
                        })
                      }
                    >
                      {folderFilesLoading ? <LoadingLabel>Loading…</LoadingLabel> : "Load more"}
                    </Button>
                  </div>
                )}
              </>
            )}
          </div>
        </section>

        <aside className="flex w-full shrink-0 flex-col border-t border-border bg-muted/10 lg:w-72 lg:border-l lg:border-t-0">
          <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Details
          </div>
          {selectedFile ? (
            <div className="scrollbar-hidden max-h-64 space-y-3 overflow-y-auto p-3 text-sm lg:max-h-[calc(100vh-18rem)]">
              {selectedFile.is_image && (
                manualFaceTag ? (
                  <ManualFaceTagger
                    driveFileId={selectedFile.id}
                    mimeType={selectedFile.mime_type}
                    fileName={selectedFile.name}
                  />
                ) : (
                  <ExpandableLibraryImage
                    fileId={selectedFile.id}
                    mimeType={selectedFile.mime_type}
                    name={selectedFile.name}
                  />
                )
              )}
              {selectedFile.is_video && (
                <video
                  src={driveVideoStreamUrl(selectedFile.id)}
                  controls
                  playsInline
                  className="w-full rounded-lg border border-border bg-black"
                />
              )}
              <div className="flex flex-wrap gap-2">
                <DownloadButton
                  url={driveFileDownloadUrl(selectedFile.id)}
                  filename={selectedFile.name}
                  variant="primary"
                />
                <IconLink
                  href={driveGoogleViewUrl(selectedFile.id)}
                  icon={ExternalLink}
                  label="Open in Drive"
                  target="_blank"
                  rel="noopener noreferrer"
                />
              </div>
              <div>
                <p className="font-medium break-all">{selectedFile.name}</p>
                <p className="mt-1 text-xs text-muted-foreground break-all">{selectedFile.path}</p>
              </div>
              <dl className="space-y-1.5 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd className={cn("rounded px-1.5 py-0.5 font-medium uppercase", statusBadge(selectedFile.status))}>
                    {isFailedLibraryFile(selectedFile) ? "failed" : selectedFile.status}
                  </dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Type</dt>
                  <dd className="truncate">{selectedFile.mime_type}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Size</dt>
                  <dd>{formatBytes(selectedFile.size)}</dd>
                </div>
              </dl>
              {isFailedLibraryFile(selectedFile) && selectedFile.error_message && (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                  {humanizeIndexError(selectedFile.error_message).summary}
                </div>
              )}
            </div>
          ) : (
            <p className="p-4 text-xs text-muted-foreground">Select a file for details</p>
          )}
        </aside>
      </div>
    </div>
  );
}
