"use client";

import { useEffect, useRef, useState } from "react";
import { ExternalLink, FileText, Image as ImageIcon, Play, Search, Sparkles, X } from "lucide-react";
import {
  apiAssetUrl,
  driveFileDownloadUrl,
  driveFileThumbnailUrl,
  driveGoogleViewUrl,
  driveVideoStreamUrl,
  type SearchMoment,
} from "@/lib/api";
import { Button, Card, DownloadButton, FilePreview, IconButton, IconLink, Input, LoadingLabel, PersonTags } from "@/components/ui";
import { FilterDropdown } from "@/components/filter-dropdown";
import { ModalOverlay } from "@/components/modal";
import {
  hydrateSearchCatalogs,
  hydrateSearchSettings,
  patchSearchSession,
  persistSearchCaptions,
  persistSearchRerank,
  resetSearchResults,
  runSearch,
  useSearchSession,
} from "@/lib/search-session";

const SEARCH_RESULTS_PAGE_SIZE = 30;

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function isTranscriptMatch(matchType: string): boolean {
  return (
    matchType === "transcript" ||
    matchType === "transcript_regex" ||
    matchType.startsWith("svs_transcript")
  );
}

function formatTimestampRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

function isVideoMoment(moment: SearchMoment): boolean {
  if (moment.mime_type.startsWith("video/")) return true;
  return /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(moment.name);
}

function seekVideoTo(video: HTMLVideoElement, timestampSec: number) {
  const seek = () => {
    try {
      video.currentTime = timestampSec;
      void video.play().catch(() => {
        /* autoplay may be blocked until user interacts */
      });
    } catch {
      /* metadata not ready yet */
    }
  };
  if (video.readyState >= 1) {
    seek();
  } else {
    video.addEventListener("loadedmetadata", seek, { once: true });
  }
}

export function SearchPage({
  embedded = false,
  hideSearchBar = false,
}: {
  embedded?: boolean;
  hideSearchBar?: boolean;
} = {}) {
  const {
    q,
    person,
    mime,
    folderPath,
    rerank,
    useCaptions,
    persons,
    folderContexts,
    libraryFolders,
    results,
    lastSearchMode,
    loading,
    previewFile,
    previewMoment,
    linkedinMap,
  } = useSearchSession();
  const [visibleFileCount, setVisibleFileCount] = useState(SEARCH_RESULTS_PAGE_SIZE);
  const loadMoreRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    hydrateSearchCatalogs();
    hydrateSearchSettings();
  }, []);

  function search() {
    void runSearch();
  }

  const files = (results?.files ?? []).filter(
    (f) => f.score != null || !f.mime_type.startsWith("image/")
  );
  const visibleFiles = files.slice(0, visibleFileCount);

  useEffect(() => {
    setVisibleFileCount(SEARCH_RESULTS_PAGE_SIZE);
  }, [results]);

  useEffect(() => {
    const sentinel = loadMoreRef.current;
    if (!sentinel || visibleFileCount >= files.length) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        setVisibleFileCount((current) =>
          Math.min(current + SEARCH_RESULTS_PAGE_SIZE, files.length)
        );
      },
      { rootMargin: "600px 0px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [files.length, visibleFileCount]);

  return (
    <div className="space-y-6">
      {!embedded && (
        <div>
          <h2 className="text-xl font-semibold sm:text-2xl">Search</h2>
          <p className="text-sm text-muted-foreground">
            Visual search via Gemini embeddings. Toggle captions for text-description matching
            (slower, stricter).
          </p>
        </div>
      )}

      {!hideSearchBar && (
      <div className="max-w-3xl space-y-2">
        <Input
          className="w-full"
          placeholder="Search (e.g. wine glass, smiling, party, people)..."
          value={q}
          onChange={(e) => patchSearchSession({ q: e.target.value })}
          onKeyDown={(e) => e.key === "Enter" && search()}
        />
        <div className="flex flex-wrap items-center gap-2">
          <FilterDropdown
            icon={ImageIcon}
            title="File type"
            value={mime === "video" ? "all" : mime}
            onChange={(v) => patchSearchSession({ mime: v })}
            options={[
              { value: "all", label: "All types" },
              { value: "image", label: "Images" },
              { value: "pdf", label: "PDFs" },
            ]}
          />
          <FilterDropdown
            title="Person"
            value={person}
            disabled={persons.length === 0}
            onChange={(v) => patchSearchSession({ person: v })}
            options={[
              { value: "", label: "All people" },
              ...persons.map((p) => ({
                value: p.name,
                label: p.name,
                faceId: p.representative_face_id,
              })),
            ]}
          />
          <FilterDropdown
            title={
              folderPath
                ? folderContexts.find((f) => f.folder_path === folderPath)?.description ||
                  libraryFolders.find((f) => f.value === folderPath)?.label ||
                  "Folder"
                : "Folder"
            }
            value={folderPath}
            onChange={(v) => patchSearchSession({ folderPath: v })}
            options={[
              { value: "", label: "All folders" },
              ...libraryFolders.map((f) => ({
                value: f.value,
                label: f.label,
                hint: folderContexts.find((c) => c.folder_path === f.value)?.description,
              })),
            ]}
          />
          <Button className="h-9 rounded-full px-4" onClick={search} disabled={loading}>
            {loading ? <LoadingLabel>Searching…</LoadingLabel> : <span className="inline-flex items-center gap-1.5"><Search size={14} />Search</span>}
          </Button>
          <button
            type="button"
            onClick={() => void persistSearchCaptions(!useCaptions)}
            title={useCaptions ? "Caption search ON" : "Caption search OFF"}
            className={`inline-flex h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors ${
              useCaptions
                ? "border-blue-400/60 bg-blue-500/10 text-blue-600 dark:border-blue-400/50 dark:bg-blue-400/15 dark:text-blue-300"
                : "border-border bg-card text-muted-foreground"
            }`}
          >
            <FileText size={13} />
            Captions
          </button>
          <button
            type="button"
            onClick={() => void persistSearchRerank(!rerank)}
            title={rerank ? "AI re-ranking ON" : "AI re-ranking OFF"}
            className={`inline-flex h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors ${
              rerank
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border bg-card text-muted-foreground"
            }`}
          >
          <Sparkles size={13} />
          Re-rank
          </button>
        </div>
      </div>
      )}

      {folderPath && folderContexts.find(f => f.folder_path === folderPath)?.description && (
        <p className="text-xs text-muted-foreground">
          📁 Folder context: <span className="italic">{folderContexts.find(f => f.folder_path === folderPath)?.description}</span>
        </p>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Searching…</LoadingLabel>
        </p>
      )}

      {results && (
        <section className="relative">
          <div className="mb-4 flex items-start justify-between gap-2">
            <h3 className="font-medium">Matching files ({files.length})</h3>
            <button
              type="button"
              onClick={resetSearchResults}
              title="Clear results"
              aria-label="Clear results"
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <X size={16} aria-hidden />
            </button>
          </div>
          {files.length === 0 ? (
            <p className="text-sm text-muted-foreground">No matching files in your Drive index.</p>
          ) : (
            <>
              <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {visibleFiles.map((file) => {
                const driveUrl = `https://drive.google.com/file/d/${file.drive_file_id}/view`;
                const downloadUrl = driveFileDownloadUrl(file.drive_file_id);
                const isImage = file.mime_type.startsWith("image/");
                const pathParts = file.path.split("/").filter(Boolean);
                const topLevelFolder = pathParts.length > 1 ? pathParts[0] : null;
                return (
                  <li
                    key={file.drive_file_id}
                    className="overflow-hidden rounded-md border border-border bg-muted/30"
                  >
                    <div className="aspect-[4/3] w-full overflow-hidden bg-black/20">
                      <FilePreview
                        driveFileId={file.drive_file_id}
                        name={file.name}
                        mimeType={file.mime_type}
                        onClick={isImage ? () => patchSearchSession({ previewFile: file }) : undefined}
                      />
                    </div>
                    <div className="flex flex-col gap-2 px-3 py-3 text-sm">
                      <div className="flex items-start justify-between gap-2">
                        <a
                          href={driveUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="min-w-0 flex-1 font-medium leading-snug text-primary hover:underline"
                        >
                          {file.name}
                        </a>
                        {file.score != null && (
                          <span className="shrink-0 rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-amber-800 dark:bg-amber-400/15 dark:text-amber-200">
                            {Math.round(file.score * 100)}%
                          </span>
                        )}
                      </div>
                      {topLevelFolder && (
                        <p className="truncate text-xs" title={file.path}>
                          <span className="inline-block max-w-full truncate rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 font-medium text-cyan-700 dark:text-cyan-300">
                            {topLevelFolder}
                          </span>
                        </p>
                      )}
                      {file.caption && (
                        <p className="line-clamp-2 text-xs text-muted-foreground/90" title={file.caption}>
                          {file.caption}
                        </p>
                      )}
                      {(file.person_names ?? []).length > 0 && (
                        <PersonTags names={file.person_names ?? []} links={linkedinMap} />
                      )}
                      <div className="flex flex-wrap items-center gap-2 pt-0.5">
                        <DownloadButton
                          url={downloadUrl}
                          filename={file.name}
                          variant="primary"
                        />
                        <IconLink
                          href={driveUrl}
                          icon={ExternalLink}
                          label="Open in Drive"
                          target="_blank"
                          rel="noopener noreferrer"
                        />
                      </div>
                    </div>
                  </li>
                );
              })}
              </ul>
              {visibleFileCount < files.length && (
                <div
                  ref={loadMoreRef}
                  className="flex min-h-20 items-center justify-center text-sm text-muted-foreground"
                  aria-label="Loading more search results"
                >
                  <LoadingLabel size={14}>
                    Showing {visibleFiles.length} of {files.length} — scroll for more
                  </LoadingLabel>
                </div>
              )}
            </>
          )}
        </section>
      )}

      <ModalOverlay open={!!previewFile} onClose={() => patchSearchSession({ previewFile: null })}>
        {previewFile && (
          <div className="relative flex max-h-[min(88dvh,720px)] flex-col overflow-hidden rounded-lg bg-card shadow-2xl">
            <div className="relative flex shrink-0 items-center justify-center bg-black">
              <IconButton
                icon={X}
                label="Close"
                onClick={() => patchSearchSession({ previewFile: null })}
                className="absolute right-3 top-3 z-10 bg-black/60 text-white hover:bg-black/80 hover:text-white"
              />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={driveFileThumbnailUrl(previewFile.drive_file_id)}
                alt={previewFile.name}
                className="block max-h-[min(48dvh,420px)] w-full object-contain"
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto border-t border-border px-4 py-3">
              <p className="break-all text-sm font-medium text-foreground">{previewFile.name}</p>
              {previewFile.caption && (
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{previewFile.caption}</p>
              )}
              {(previewFile.person_names ?? []).length > 0 && (
                <PersonTags names={previewFile.person_names ?? []} className="mt-2" links={linkedinMap} />
              )}
              <div className="mt-3 flex flex-wrap gap-2 pb-1">
                <DownloadButton
                  url={driveFileDownloadUrl(previewFile.drive_file_id)}
                  filename={previewFile.name}
                  variant="primary"
                />
                <IconLink
                  href={driveGoogleViewUrl(previewFile.drive_file_id)}
                  icon={ExternalLink}
                  label="Open in Drive"
                  target="_blank"
                  rel="noopener noreferrer"
                />
              </div>
            </div>
          </div>
        )}
      </ModalOverlay>
      <ModalOverlay open={!!previewMoment} onClose={() => patchSearchSession({ previewMoment: null })}>
        {previewMoment && (
          <MomentPreviewPanel moment={previewMoment} onClose={() => patchSearchSession({ previewMoment: null })} />
        )}
      </ModalOverlay>
    </div>
  );
}

function matchBadgeStyle(matchType: string): string {
  if (matchType === "face_detected") return "bg-amber-500/15 text-amber-800 dark:bg-amber-950 dark:text-amber-300";
  if (isTranscriptMatch(matchType)) return "bg-sky-500/15 text-sky-800 dark:bg-sky-950 dark:text-sky-300";
  if (matchType === "gemini_visual") return "bg-emerald-500/15 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300";
  if (matchType.startsWith("svs_visual")) return "bg-emerald-500/15 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300";
  return "bg-muted text-muted-foreground";
}

function matchLabel(matchType: string, score: number | null): string {
  const pct = score != null ? ` ${Math.round(score * 100)}%` : "";
  if (matchType === "face_detected") return `face${pct}`;
  if (isTranscriptMatch(matchType)) return `transcript${pct}`;
  if (matchType === "gemini_visual") return `visual${pct}`;
  if (matchType === "svs_visual") return `visual${pct}`;
  return `${matchType}${pct}`;
}

function MomentCard({ moment, onPreview }: { moment: SearchMoment; onPreview: () => void }) {
  const isFace = moment.match_type === "face_detected";
  const isTranscript = isTranscriptMatch(moment.match_type);
  const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);
  const driveUrl = driveGoogleViewUrl(moment.drive_file_id);
  const downloadUrl = driveFileDownloadUrl(moment.drive_file_id);

  return (
    <li className="overflow-hidden rounded-md border border-border bg-muted/30">
      <button
        type="button"
        onClick={onPreview}
        className="group relative aspect-video w-full overflow-hidden bg-black/40"
        aria-label={`Preview ${moment.name} at ${timeLabel}`}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={apiAssetUrl(moment.preview_url)}
          alt={moment.name}
          className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
        />
        <span className="absolute inset-0 flex items-center justify-center bg-black/0 transition-colors group-hover:bg-black/25">
          <span className="rounded-full bg-black/70 p-2.5 text-white opacity-0 transition-opacity group-hover:opacity-100">
            <Play size={22} fill="currentColor" aria-hidden />
          </span>
        </span>
        <span className="absolute bottom-2 left-2 rounded bg-black/70 px-2 py-0.5 text-xs text-white">
          {timeLabel}
        </span>
        {isTranscript && (
          <span className="absolute right-2 top-2 rounded bg-blue-600/90 px-2 py-0.5 text-xs text-white">
            transcript
          </span>
        )}
        {isFace && (
          <span className="absolute right-2 top-2 rounded bg-amber-600/80 px-2 py-0.5 text-xs text-white">
            face match
          </span>
        )}
      </button>
      <div className="px-3 py-3 text-sm">
        <p className="font-medium">{moment.name}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground" title={moment.path}>
          {moment.path}
        </p>
        {isTranscript && moment.snippet && (
          <p
            className="mt-2 line-clamp-3 rounded bg-blue-600/10 px-2 py-1.5 text-xs text-blue-900 dark:bg-blue-950/30 dark:text-blue-100"
            title={moment.snippet}
          >
            &ldquo;{moment.snippet}&rdquo;
          </p>
        )}
        {!isTranscript && moment.snippet && (
          <p className="mt-2 line-clamp-2 text-xs text-muted-foreground/80" title={moment.snippet}>
            {moment.snippet}
          </p>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-2 py-0.5 text-xs ${matchBadgeStyle(moment.match_type)}`}>
            {matchLabel(moment.match_type, moment.score ?? null)}
          </span>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <IconButton icon={Play} label={`Play at ${timeLabel}`} variant="secondary" onClick={onPreview} />
          <DownloadButton url={downloadUrl} filename={moment.name} variant="primary" />
          <IconLink href={driveUrl} icon={ExternalLink} label="Open in Drive" target="_blank" rel="noopener noreferrer" />
        </div>
        {(moment.person_names ?? []).length > 0 && (
          <PersonTags names={moment.person_names ?? []} className="mt-2" />
        )}
      </div>
    </li>
  );
}

function MomentPreviewPanel({ moment, onClose }: { moment: SearchMoment; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const isVideo = isVideoMoment(moment);
  const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);
  const streamUrl = `${driveVideoStreamUrl(moment.drive_file_id)}#t=${Math.floor(moment.timestamp_sec)}`;
  const driveUrl = driveGoogleViewUrl(moment.drive_file_id);
  const downloadUrl = driveFileDownloadUrl(moment.drive_file_id);
  const [videoError, setVideoError] = useState<string | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !isVideo) return;
    setVideoError(null);
    seekVideoTo(video, moment.timestamp_sec);
  }, [moment.drive_file_id, moment.timestamp_sec, isVideo]);

  return (
    <div className="relative flex max-h-[min(88dvh,720px)] flex-col overflow-hidden rounded-lg bg-card shadow-2xl">
      <IconButton
        icon={X}
        label="Close"
        onClick={onClose}
        className="absolute right-3 top-3 z-10 bg-black/60 text-white hover:bg-black/80 hover:text-white"
      />
      {isVideo ? (
        <div className="shrink-0 bg-black">
          <video
            ref={videoRef}
            src={streamUrl}
            controls
            playsInline
            preload="metadata"
            className="max-h-[min(48dvh,420px)] w-full object-contain"
            onError={() => setVideoError("Video preview unavailable — try Open in Drive.")}
          />
          {videoError && <p className="px-4 py-2 text-xs text-destructive">{videoError}</p>}
        </div>
      ) : (
        <div className="flex shrink-0 items-center justify-center bg-black">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={apiAssetUrl(moment.preview_url)}
            alt={moment.name}
            className="max-h-[min(48dvh,420px)] w-full object-contain"
          />
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto border-t border-border px-4 py-3">
        <p className="break-all text-sm font-medium text-foreground">{moment.name}</p>
        <p className="mt-1 truncate text-xs text-muted-foreground" title={moment.path}>
          {moment.path}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">Moment at {timeLabel}</p>
        {moment.snippet && (
          <p className="mt-2 line-clamp-3 text-xs text-muted-foreground" title={moment.snippet}>
            {moment.snippet}
          </p>
        )}
        {(moment.person_names ?? []).length > 0 && (
          <PersonTags names={moment.person_names ?? []} className="mt-2" />
        )}
        <div className="mt-3 flex flex-wrap gap-2 pb-1">
          {isVideo && (
            <IconButton
              icon={Play}
              label={`Jump to ${timeLabel}`}
              variant="secondary"
              onClick={() => {
                const video = videoRef.current;
                if (video) seekVideoTo(video, moment.timestamp_sec);
              }}
            />
          )}
          <DownloadButton url={downloadUrl} filename={moment.name} variant="primary" />
          <IconLink href={driveUrl} icon={ExternalLink} label="Open in Drive" target="_blank" rel="noopener noreferrer" />
        </div>
      </div>
    </div>
  );
}
