"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight,
  FileImage,
  FileVideo,
  Folder,
  Home,
  Pause,
  RefreshCw,
} from "lucide-react";
import {
  apiClient,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  type LibraryFile,
  type LibraryFolder,
  type LibraryFolderPage,
  type LibraryResponse,
} from "@/lib/api";
import { LoadingLabel } from "@/components/ui";
import { DriveSessionBar } from "@/components/drive-session-bar";
import { cn } from "@/lib/utils";
import { formatCount, skipReasonMeta } from "@/lib/index-errors";
import { hydrateKeyFromDisk, readCache, writeCache } from "@/lib/data-cache";
import { findLibraryFolder, librarySubfoldersAtPath } from "@/lib/library-folders";
import { useRegisterTestShellChrome } from "@/lib/test-shell-chrome";

const PAGE_SIZE = 120;
const SHELL_CACHE_KEY = "driveLibraryShell";
const FOLDER_CACHE_PREFIX = "driveLibrary:folder:";

function folderCacheKey(path: string) {
  return `${FOLDER_CACHE_PREFIX}${path}`;
}

function indexedCount(f: LibraryFolder): number {
  return Math.max(
    0,
    f.file_count - f.pending_count - f.error_count - f.skipped_count - f.archived_count
  );
}

type SkipReasonCount = { reason: string; count: number };

function FolderTile({
  folder,
  onOpen,
  loadSkipReasons,
}: {
  folder: LibraryFolder;
  onOpen: (path: string) => void;
  loadSkipReasons: (path: string) => Promise<SkipReasonCount[]>;
}) {
  const indexed = indexedCount(folder);
  const pct = folder.file_count > 0 ? Math.round((indexed / folder.file_count) * 100) : 0;
  const [reasonsOpen, setReasonsOpen] = useState(false);
  const [reasonsLoading, setReasonsLoading] = useState(false);
  const [reasons, setReasons] = useState<SkipReasonCount[] | null>(null);
  const reasonRootRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const openReasons = useCallback(() => {
    if (folder.skipped_count <= 0) return;
    clearCloseTimer();
    setReasonsOpen(true);
    if (reasons !== null || reasonsLoading) return;
    setReasonsLoading(true);
    void loadSkipReasons(folder.path)
      .then(setReasons)
      .finally(() => setReasonsLoading(false));
  }, [
    clearCloseTimer,
    folder.path,
    folder.skipped_count,
    loadSkipReasons,
    reasons,
    reasonsLoading,
  ]);

  const scheduleClose = useCallback(() => {
    clearCloseTimer();
    closeTimerRef.current = setTimeout(() => {
      if (!reasonRootRef.current?.contains(document.activeElement)) {
        setReasonsOpen(false);
      }
    }, 180);
  }, [clearCloseTimer]);

  useEffect(() => {
    return clearCloseTimer;
  }, [clearCloseTimer]);

  useEffect(() => {
    if (!reasonsOpen) return;
    function onDocumentPointerDown(event: MouseEvent) {
      if (!reasonRootRef.current?.contains(event.target as Node)) {
        setReasonsOpen(false);
      }
    }
    function onDocumentKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setReasonsOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocumentPointerDown);
    document.addEventListener("keydown", onDocumentKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocumentPointerDown);
      document.removeEventListener("keydown", onDocumentKeyDown);
    };
  }, [reasonsOpen]);

  return (
    <div className="group relative min-w-0">
      <button
        type="button"
        onClick={() => onOpen(folder.path)}
        className="relative flex w-full flex-col items-center gap-1.5 rounded-xl border border-transparent p-3 text-center transition-colors hover:border-border hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Folder size={56} strokeWidth={1.2} className="fill-amber-400/30 text-amber-500" />
        <span className="w-full truncate text-xs font-medium text-foreground">{folder.name}</span>
        <span className="text-[10px] text-muted-foreground">
          {formatCount(folder.file_count)} items
        </span>

        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-0.5 rounded-xl bg-card/95 opacity-0 backdrop-blur-sm transition-opacity group-hover:opacity-100">
          <p className="text-xs font-semibold text-foreground">{pct}% indexed</p>
          <p className="text-[10px] text-muted-foreground">
            {formatCount(indexed)}/{formatCount(folder.file_count)} files
          </p>
          {folder.image_count > 0 && (
            <p className="text-[10px] text-muted-foreground">
              {formatCount(folder.captioned_count)}/{formatCount(folder.image_count)} captioned
            </p>
          )}
          {folder.error_count > 0 && (
            <p className="text-[10px] font-medium text-red-600 dark:text-red-400">
              {formatCount(folder.error_count)} failed
            </p>
          )}
          {folder.indexing_paused && (
            <p className="flex items-center gap-1 text-[10px] font-medium text-amber-600 dark:text-amber-400">
              <Pause size={9} /> paused
            </p>
          )}
        </div>
      </button>

      {folder.skipped_count > 0 && (
        <div
          ref={reasonRootRef}
          className="absolute left-1/2 top-1.5 z-30 translate-x-3"
          onMouseEnter={openReasons}
          onMouseLeave={scheduleClose}
        >
          <button
            type="button"
            aria-haspopup="dialog"
            aria-expanded={reasonsOpen}
            aria-controls={`skip-reasons-${folder.path.replace(/[^a-z0-9]+/gi, "-")}`}
            aria-label={`${formatCount(folder.skipped_count)} skipped files in ${folder.name}`}
            onFocus={openReasons}
            onClick={() => {
              if (reasonsOpen) {
                setReasonsOpen(false);
              } else {
                openReasons();
              }
            }}
            className="rounded-full border border-amber-500/40 bg-card px-1.5 py-0.5 text-[9px] font-semibold tabular-nums text-amber-700 shadow-sm transition-colors hover:bg-amber-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-amber-300"
          >
            {folder.skipped_count > 99 ? "99+" : formatCount(folder.skipped_count)} skipped
          </button>

          {reasonsOpen && (
            <div
              id={`skip-reasons-${folder.path.replace(/[^a-z0-9]+/gi, "-")}`}
              role="dialog"
              aria-label={`Skip reasons for ${folder.name}`}
              className="absolute left-1/2 top-full z-50 mt-2 w-64 max-w-[78vw] -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-card text-left shadow-lg"
            >
              <div className="border-b border-border/70 px-3 py-2.5">
                <p className="text-xs font-semibold text-foreground">
                  Skipped files · {formatCount(folder.skipped_count)}
                </p>
              </div>
              <div className="py-1">
                {reasonsLoading ? (
                  <p className="px-3 py-3 text-xs text-muted-foreground">
                    <LoadingLabel size={13}>Loading reasons…</LoadingLabel>
                  </p>
                ) : reasons && reasons.length > 0 ? (
                  reasons.slice(0, 3).map((item) => {
                    const meta = skipReasonMeta(item.reason);
                    return (
                      <div key={item.reason} className="px-3 py-2">
                        <div className="flex items-baseline justify-between gap-3">
                          <p className="truncate text-xs font-medium text-foreground">
                            {meta.label}
                          </p>
                          <span className="shrink-0 text-xs font-semibold tabular-nums text-foreground">
                            {formatCount(item.count)}
                          </span>
                        </div>
                        <p className="mt-0.5 line-clamp-1 text-[10px] text-muted-foreground">
                          {meta.hint}
                        </p>
                      </div>
                    );
                  })
                ) : (
                  <p className="px-3 py-3 text-xs text-muted-foreground">
                    No detailed reasons available.
                  </p>
                )}
              </div>
              <a
                href="/admin"
                className="block border-t border-border/70 px-3 py-2 text-[11px] font-medium text-primary hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              >
                View skipped files
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FileTile({ file }: { file: LibraryFile }) {
  const [imgFailed, setImgFailed] = useState(false);
  const Icon = file.is_video ? FileVideo : FileImage;
  return (
    <a
      href={driveGoogleViewUrl(file.id)}
      target="_blank"
      rel="noopener noreferrer"
      title={file.name}
      className="group relative flex flex-col items-center gap-1.5 rounded-xl border border-transparent p-3 text-center transition-colors hover:border-border hover:bg-accent/60"
    >
      {file.is_image && !imgFailed ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={driveFileThumbnailUrl(file.id)}
          alt={file.name}
          loading="lazy"
          onError={() => setImgFailed(true)}
          className="h-14 w-14 rounded-md object-cover"
        />
      ) : (
        <Icon size={48} strokeWidth={1.2} className="text-sky-500" />
      )}
      <span className="w-full truncate text-xs font-medium text-foreground">{file.name}</span>
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          file.status === "processed"
            ? "bg-emerald-500"
            : file.status === "error"
              ? "bg-red-500"
              : "bg-amber-500"
        )}
        title={file.status}
      />
    </a>
  );
}

export default function TestFoldersPage() {
  const [shell, setShell] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [path, setPath] = useState("/");
  const [files, setFiles] = useState<LibraryFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const reasonLibraryRef = useRef<LibraryResponse | null>(null);
  const reasonLibraryRequestRef = useRef<Promise<LibraryResponse | null> | null>(null);

  const loadSkipReasons = useCallback(async (folderPath: string): Promise<SkipReasonCount[]> => {
    let library = reasonLibraryRef.current;
    if (!library) {
      if (!reasonLibraryRequestRef.current) {
        reasonLibraryRequestRef.current = apiClient
          .driveLibrary()
          .then((response) => {
            reasonLibraryRef.current = response;
            return response;
          })
          .catch(() => null)
          .finally(() => {
            reasonLibraryRequestRef.current = null;
          });
      }
      library = await reasonLibraryRequestRef.current;
    }
    if (!library) return [];
    return (findLibraryFolder(library.tree, folderPath)?.top_skip_reasons ?? []).slice(0, 3);
  }, []);

  const loadShell = useCallback(async (force = false) => {
    try {
      if (force) {
        reasonLibraryRef.current = null;
        reasonLibraryRequestRef.current = null;
      }
      if (!force) {
        const revRes = await apiClient.driveLibraryRevision().catch(() => null);
        const rev = revRes?.revision ?? null;
        const prev = readCache<LibraryResponse>(SHELL_CACHE_KEY);
        // Refetch when shell lacked Qdrant caption/embed stats (zeros on hover).
        if (
          rev &&
          prev?.revision === rev &&
          prev.data &&
          prev.data.summary?.caption_stats_ready
        ) {
          setShell(prev.data);
          setLoading(false);
          return;
        }
      }
      const next = await apiClient.driveLibraryShell();
      const writeRev = next.revision ?? String(Date.now());
      writeCache(SHELL_CACHE_KEY, next, writeRev, true);
      setShell(next);
    } catch {
      /* toasted by api() */
    } finally {
      setLoading(false);
    }
  }, []);

  const loadFiles = useCallback(
    async (folderPath: string, opts?: { append?: boolean; force?: boolean }) => {
      if (folderPath === "/") {
        setFiles([]);
        setNextCursor(null);
        setTotal(0);
        setFilesLoading(false);
        return;
      }
      const key = folderCacheKey(folderPath);
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
          setFiles(cached.data.files);
          setNextCursor(cached.data.next_cursor);
          setTotal(cached.data.total);
          setFilesLoading(false);
          return;
        }
      }
      setFilesLoading(true);
      try {
        const page: LibraryFolderPage = await apiClient.driveLibraryFolder(folderPath, {
          limit: PAGE_SIZE,
          cursor: opts?.append ? nextCursor : null,
        });
        setFiles((prev) => {
          const next = opts?.append ? [...prev, ...page.files] : page.files;
          writeCache(
            key,
            { files: next, next_cursor: page.next_cursor, total: page.total },
            page.revision ?? readCache<LibraryResponse>(SHELL_CACHE_KEY)?.revision ?? String(Date.now()),
            true
          );
          return next;
        });
        setNextCursor(page.next_cursor);
        setTotal(page.total);
      } catch {
        if (!opts?.append) setFiles([]);
      } finally {
        setFilesLoading(false);
      }
    },
    [nextCursor]
  );

  useEffect(() => {
    const cached = hydrateKeyFromDisk(SHELL_CACHE_KEY) as
      | import("@/lib/data-cache").CacheEntry<LibraryResponse>
      | null;
    if (cached?.data) {
      setShell(cached.data);
      setLoading(false);
    }

    void loadShell(false);
  }, [loadShell]);

  useEffect(() => {
    setFiles([]);
    setNextCursor(null);
    void loadFiles(path, { force: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const subfolders = useMemo(
    () => librarySubfoldersAtPath(shell?.tree, path),
    [shell, path]
  );
  const totalFiles = shell?.summary?.total_files ?? 0;

  useRegisterTestShellChrome(
    <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
      <h1 className="shrink-0 text-lg font-semibold tracking-tight sm:text-xl">Indexed Folders</h1>
      <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end sm:gap-3">
        <DriveSessionBar compact />
        {shell && (
          <span className="tabular-nums text-sm font-medium text-foreground">
            {formatCount(totalFiles)} files
          </span>
        )}
        <button
          type="button"
          title="Refresh"
          onClick={() => {
            void loadShell(true);
            void loadFiles(path, { force: true });
          }}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
        >
          <RefreshCw size={15} className={cn(loading && "animate-spin")} />
        </button>
      </div>
    </div>,
    [shell, totalFiles, loading, path, loadShell, loadFiles]
  );

  const crumbs = useMemo(() => {
    const parts = path.split("/").filter(Boolean);
    const out: { label: string; path: string }[] = [{ label: "Library", path: "/" }];
    let acc = "";
    for (const part of parts) {
      acc += `/${part}`;
      out.push({ label: part, path: acc });
    }
    return out;
  }, [path]);

  return (
    <div className="space-y-4">
      <nav className="flex min-w-0 items-center gap-1 overflow-x-auto rounded-xl border border-border bg-card px-3 py-2 text-sm">
        {crumbs.map((c, i) => (
          <span key={c.path} className="flex shrink-0 items-center gap-1">
            {i > 0 && <ChevronRight size={13} className="text-muted-foreground" />}
            <button
              type="button"
              onClick={() => setPath(c.path)}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-1.5 py-0.5 transition-colors hover:bg-accent",
                c.path === path ? "font-semibold text-foreground" : "text-muted-foreground"
              )}
            >
              {i === 0 && <Home size={13} />}
              {c.label}
            </button>
          </span>
        ))}
      </nav>

      {loading && !shell ? (
        <p className="py-16 text-center text-sm text-muted-foreground">
          <LoadingLabel size={16}>Loading library…</LoadingLabel>
        </p>
      ) : (
        <div className="rounded-2xl border border-border bg-card p-4 shadow-sm">
          {subfolders.length === 0 && files.length === 0 && !filesLoading ? (
            <p className="py-16 text-center text-sm text-muted-foreground">This folder is empty</p>
          ) : (
            <div className="grid grid-cols-3 gap-1 sm:grid-cols-4 md:grid-cols-6 xl:grid-cols-8">
              {subfolders.map((f) => (
                <FolderTile
                  key={f.path}
                  folder={f}
                  onOpen={setPath}
                  loadSkipReasons={loadSkipReasons}
                />
              ))}
              {files.map((file) => (
                <FileTile key={file.id} file={file} />
              ))}
            </div>
          )}

          {filesLoading && (
            <p className="py-6 text-center text-xs text-muted-foreground">
              <LoadingLabel size={14}>Loading files…</LoadingLabel>
            </p>
          )}
          {nextCursor && !filesLoading && (
            <div className="mt-3 flex justify-center border-t border-border pt-3">
              <button
                type="button"
                onClick={() => void loadFiles(path, { append: true })}
                className="rounded-full border border-border px-4 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-accent"
              >
                Load more ({formatCount(files.length)} of {formatCount(total)})
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
