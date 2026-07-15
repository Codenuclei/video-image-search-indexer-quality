"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileImage,
  FileVideo,
  Folder,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  XCircle,
} from "lucide-react";
import {
  apiClient,
  driveFilePreviewUrl,
  type LibraryFile,
  type LibraryFolder,
  type LibraryResponse,
} from "@/lib/api";
import { Button, Card, Input, ServiceErrorCard, StatCard } from "@/components/ui";
import { cn } from "@/lib/utils";

type FilterMode = "all" | "processed" | "skipped" | "missing_caption" | "missing_embed" | "pending" | "error";

const FILE_TABLE_COLS =
  "grid w-full grid-cols-[minmax(0,1fr)_5.5rem_4.5rem_4.5rem_4.5rem] items-center gap-2 sm:grid-cols-[minmax(0,1.4fr)_5.5rem_4.5rem_4.5rem_4.5rem]";

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
  const missingCaps = folder.image_count - folder.captioned_count;

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
          <Folder size={14} className={cn("shrink-0", folder.indexing_paused ? "text-zinc-500" : "text-amber-500")} />
          <span className="min-w-0 flex-1 truncate">{folder.name}</span>
          <span className="shrink-0 text-[10px] text-muted-foreground">
            {folder.image_count > 0 ? (
              <>
                {folder.captioned_count}/{folder.image_count}
                {missingCaps > 0 && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">·{missingCaps}</span>
                )}
              </>
            ) : (
              folder.file_count
            )}
          </span>
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
              <Loader2 size={12} className="animate-spin" />
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
      <span className={cn("justify-self-center rounded px-1.5 py-0.5 text-center text-[10px] font-medium uppercase", statusBadge(file.status))}>
        {file.status}
      </span>
      <span className="hidden justify-self-center sm:block">
        {file.is_image ? (
          file.has_caption ? (
            <CheckCircle2 size={14} className="mx-auto text-emerald-500" />
          ) : file.status === "processed" ? (
            <XCircle size={14} className="mx-auto text-amber-500" />
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        ) : (
          <span className="text-muted-foreground">n/a</span>
        )}
      </span>
      <span className="hidden justify-self-center sm:block">
        {file.is_image ? (
          file.has_embedding ? (
            <CheckCircle2 size={14} className="mx-auto text-emerald-500" />
          ) : file.status === "processed" ? (
            <XCircle size={14} className="mx-auto text-amber-500" />
          ) : (
            <span className="text-muted-foreground">—</span>
          )
        ) : (
          <span className="text-muted-foreground">n/a</span>
        )}
      </span>
      <span className="hidden justify-self-end text-xs text-muted-foreground sm:block">{formatBytes(file.size)}</span>
    </button>
  );
}

export default function LibraryPage() {
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFolderPath, setSelectedFolderPath] = useState("/");
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["/"]));
  const [filter, setFilter] = useState<FilterMode>("all");
  const [search, setSearch] = useState("");
  const [folderActionBusy, setFolderActionBusy] = useState<string | null>(null);
  const [skipBusy, setSkipBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const lib = await apiClient.driveLibrary();
      setData(lib);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load library");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [load]);

  const selectedFolder = useMemo(() => {
    if (!data?.tree) return null;
    return findFolder(data.tree, selectedFolderPath) ?? data.tree;
  }, [data, selectedFolderPath]);

  const filteredFiles = useMemo(() => {
    if (!selectedFolder) return [];
    let files = selectedFolder.files;
    const q = search.trim().toLowerCase();
    if (q) files = files.filter((f) => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));
    if (filter === "missing_caption") {
      files = files.filter((f) => f.is_image && f.status === "processed" && !f.has_caption);
    } else if (filter === "missing_embed") {
      files = files.filter((f) => f.is_image && f.status === "processed" && !f.has_embedding);
    } else if (filter === "pending") {
      files = files.filter((f) => f.status === "pending" || f.status === "processing");
    } else if (filter === "processed") {
      files = files.filter((f) => f.status === "processed");
    } else if (filter === "skipped") {
      files = files.filter((f) => f.status === "skipped");
    } else if (filter === "error") {
      files = files.filter((f) => f.status === "error");
    }
    return files;
  }, [selectedFolder, filter, search]);

  const selectedFile = useMemo(() => {
    if (!selectedFileId || !selectedFolder) return null;
    return selectedFolder.files.find((f) => f.id === selectedFileId) ?? null;
  }, [selectedFileId, selectedFolder]);

  const maintenance = data?.maintenance;
  const summary = data?.summary;
  const backfillActive = maintenance?.caption_backfill_running || maintenance?.embed_backfill_running;

  function toggleExpand(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  async function pauseFolder(path: string) {
    setFolderActionBusy(path);
    try {
      await apiClient.pauseFolderIndexing(path);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to pause folder");
    } finally {
      setFolderActionBusy(null);
    }
  }

  async function resumeFolder(path: string) {
    setFolderActionBusy(path);
    try {
      await apiClient.resumeFolderIndexing(path);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to resume folder");
    } finally {
      setFolderActionBusy(null);
    }
  }

  async function skipCorrupt() {
    setSkipBusy(true);
    try {
      await apiClient.skipCorruptFiles();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to skip corrupt files");
    } finally {
      setSkipBusy(false);
    }
  }

  return (
    <div className="space-y-4 pb-20 md:pb-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Media Library</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Folder-wise view of indexed files — caption, embed, and index status
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="secondary" onClick={() => { setLoading(true); load(); }} disabled={loading}>
            <RefreshCw size={14} className={cn("mr-1.5 inline", loading && "animate-spin")} />
            Refresh
          </Button>
          <Button variant="secondary" onClick={skipCorrupt} disabled={skipBusy}>
            {skipBusy ? "Skipping…" : "Skip corrupt files"}
          </Button>
        </div>
      </div>

      {backfillActive && (
        <Card className="border-primary/30 bg-primary/5">
          <div className="flex items-center gap-2 text-sm text-primary">
            <Loader2 size={16} className="animate-spin" />
            <span>
              Auto backfill running
              {maintenance?.caption_backfill_running && " · captions"}
              {maintenance?.embed_backfill_running && " · embeddings"}
              {" — resumes automatically after deploys"}
            </span>
          </div>
        </Card>
      )}

      {error && (
        <ServiceErrorCard
          message={error}
          onRetry={() => {
            setLoading(true);
            load();
          }}
          onDismiss={() => setError(null)}
          retryLabel="Refresh"
        />
      )}

      {summary && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <StatCard label="Total files" value={summary.total_files} />
          <StatCard label="Images" value={summary.images} hint={`${summary.videos} videos`} />
          <StatCard
            label="Captioned"
            value={`${summary.captioned}/${summary.images}`}
            hint={`${summary.caption_pct}% complete`}
          />
          <StatCard label="Embedded" value={`${summary.embedded}/${summary.images}`} />
          <StatCard
            label="Needs work"
            value={summary.pending + (summary.images - summary.captioned)}
            hint={`${summary.pending} pending · ${summary.errors} errors`}
          />
        </div>
      )}

      <div className="flex min-h-[520px] flex-col overflow-hidden rounded-xl border border-border bg-card lg:flex-row">
        {/* Folder tree — FTP-style left pane */}
        <aside className="w-full shrink-0 border-b border-border bg-muted/20 lg:w-64 lg:border-b-0 lg:border-r">
          <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Folders
          </div>
          <div className="max-h-48 overflow-y-auto p-2 lg:max-h-[calc(100vh-18rem)]">
            {data?.tree && (
              <FolderTreeItem
                folder={data.tree}
                selectedPath={selectedFolderPath}
                expanded={expanded}
                onSelect={setSelectedFolderPath}
                onToggle={toggleExpand}
                onPause={pauseFolder}
                onResume={resumeFolder}
                actionBusy={folderActionBusy}
              />
            )}
          </div>
        </aside>

        {/* File list — center pane */}
        <section className="flex min-w-0 flex-1 flex-col">
          <div className="grid gap-2 border-b border-border px-3 py-2 sm:grid-cols-[minmax(0,1fr)_11rem_11rem] sm:items-center">
            <p className="min-w-0 text-sm font-medium">
              <span className="text-foreground">{folderDisplayName(selectedFolder, selectedFolderPath)}</span>
              <span className="ml-2 text-muted-foreground">
                ({filteredFiles.length} file{filteredFiles.length === 1 ? "" : "s"})
              </span>
            </p>
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as FilterMode)}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-xs"
            >
              <option value="all">All files</option>
              <option value="processed">Processed</option>
              <option value="skipped">Skipped</option>
              <option value="missing_caption">Missing caption</option>
              <option value="missing_embed">Missing embed</option>
              <option value="pending">Pending / processing</option>
              <option value="error">Errors</option>
            </select>
            <Input
              placeholder="Search in folder…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full py-1.5 text-xs"
            />
          </div>

          <div className={cn(FILE_TABLE_COLS, "hidden border-b border-border bg-muted/30 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground sm:grid")}>
            <span>Name</span>
            <span className="text-center">Index</span>
            <span className="text-center">Caption</span>
            <span className="text-center">Embed</span>
            <span className="text-right">Size</span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && !data ? (
              <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
                <Loader2 size={18} className="animate-spin" />
                Loading library…
              </div>
            ) : filteredFiles.length === 0 ? (
              <p className="py-16 text-center text-sm text-muted-foreground">No files in this view</p>
            ) : (
              filteredFiles.map((file) => (
                <FileRow
                  key={file.id}
                  file={file}
                  selected={selectedFileId === file.id}
                  onSelect={() => setSelectedFileId(file.id)}
                />
              ))
            )}
          </div>
        </section>

        {/* Detail pane — right */}
        <aside className="w-full shrink-0 border-t border-border bg-muted/10 lg:w-72 lg:border-l lg:border-t-0">
          <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Details
          </div>
          {selectedFile ? (
            <div className="space-y-3 p-3 text-sm">
              {selectedFile.is_image && (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={driveFilePreviewUrl(selectedFile.id, selectedFile.mime_type)}
                  alt={selectedFile.name}
                  className="w-full rounded-lg border border-border object-cover"
                />
              )}
              <div>
                <p className="font-medium break-all">{selectedFile.name}</p>
                <p className="mt-1 text-xs text-muted-foreground break-all">{selectedFile.path}</p>
              </div>
              <dl className="space-y-1.5 text-xs">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd className={cn("rounded px-1.5 py-0.5 font-medium uppercase", statusBadge(selectedFile.status))}>
                    {selectedFile.status}
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
                {selectedFile.is_image && (
                  <>
                    <div className="flex justify-between gap-2">
                      <dt className="text-muted-foreground">Caption</dt>
                      <dd>{selectedFile.has_caption ? "Yes" : "Missing"}</dd>
                    </div>
                    <div className="flex justify-between gap-2">
                      <dt className="text-muted-foreground">Embedding</dt>
                      <dd>{selectedFile.has_embedding ? "Yes" : "Missing"}</dd>
                    </div>
                  </>
                )}
              </dl>
              {selectedFile.caption_preview && (
                <div className="rounded-lg border border-border bg-background p-2 text-xs leading-relaxed text-muted-foreground">
                  <p className="mb-1 font-medium text-foreground">Caption</p>
                  {selectedFile.caption_preview}
                </div>
              )}
              {selectedFile.error_message && (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                  {selectedFile.error_message}
                </div>
              )}
            </div>
          ) : (
            <p className="p-4 text-xs text-muted-foreground">Select a file to see caption and index details</p>
          )}
        </aside>
      </div>
    </div>
  );
}
