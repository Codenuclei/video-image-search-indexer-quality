"use client";

import type { ReactNode } from "react";
import { Check, Play } from "lucide-react";
import {
  apiAssetUrl,
  driveFileDownloadUrl,
  type SearchMoment,
} from "@/lib/api";
import { DownloadButton, PersonTags } from "@/components/ui";
import { CarouselTranscriptTopics } from "@/components/carousel-transcript-topics";
import { cn } from "@/lib/utils";

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatTimestampRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

function matchLabel(matchType: string, score: number | null): string {
  const pct = score != null ? ` ${Math.round(score * 100)}%` : "";
  if (matchType === "face_detected") return `face${pct}`;
  if (matchType.startsWith("transcript") || matchType.startsWith("svs_transcript")) {
    return `transcript${pct}`;
  }
  if (matchType === "gemini_visual" || matchType.startsWith("svs_visual")) return `visual${pct}`;
  return `${matchType}${pct}`;
}

export function momentKey(m: { drive_file_id: string; timestamp_sec: number }): string {
  return `${m.drive_file_id}:${m.timestamp_sec}`;
}

type CarouselSearchResultsProps = {
  moments: SearchMoment[];
  loading: boolean;
  error: string | null;
  workingIndex: number | null;
  onSelectForWork: (index: number) => void;
  onClearWork: () => void;
  onOpenPreview: (moment: SearchMoment) => void;
  selectedKeys: string[];
  pickedKey: string | null;
  snapshotKeyOf: (moment: SearchMoment) => string | null;
  onPickSnapshot: (moment: SearchMoment) => void;
  onToggleMomentSelect: (moment: SearchMoment) => void;
  onSelectAllFromVideo: () => void;
  onClearMomentSelection: () => void;
  videoMomentCount: number;
  /** Optional: wire transcript topic titles into script themes */
  transcriptSelectedTitles?: string[];
  onToggleTranscriptTitle?: (title: string) => void;
  /** Slot rendered inside work surface after topics (hooks / script) */
  workSurfaceExtras?: ReactNode;
};

/**
 * Search results + gated work surface.
 * Preview / player appears only after the user explicitly selects a moment to work on.
 */
export function CarouselSearchResults({
  moments,
  loading,
  error,
  workingIndex,
  onSelectForWork,
  onClearWork,
  onOpenPreview,
  selectedKeys,
  pickedKey,
  snapshotKeyOf,
  onPickSnapshot,
  onToggleMomentSelect,
  onSelectAllFromVideo,
  onClearMomentSelection,
  videoMomentCount,
  transcriptSelectedTitles,
  onToggleTranscriptTitle,
  workSurfaceExtras,
}: CarouselSearchResultsProps) {
  const working = workingIndex != null ? moments[workingIndex] ?? null : null;
  const hasResults = moments.length > 0;

  return (
    <section className="studio-panel overflow-hidden" data-testid="carousel-results">
      <div className="border-b border-border px-4 py-5 sm:px-6">
        <p className="studio-section-label">{working ? "Work surface" : "Results"}</p>
        <h3 className="mt-1 text-base font-semibold tracking-tight text-foreground sm:text-lg">
          {working ? working.name : hasResults ? `${moments.length} moments` : "No moments yet"}
        </h3>
        <p className="mt-1 text-sm font-medium text-muted-foreground">
          {working
            ? "Preview, themes, then hooks surface below — no busy wizard steps."
            : "Select a moment to open the studio. Preview stays closed until then."}
        </p>

        {hasResults && working && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" className="studio-btn studio-btn-ghost" onClick={onClearWork}>
              Back to results
            </button>
            <button type="button" className="studio-btn studio-btn-ghost" onClick={onSelectAllFromVideo}>
              Use all from video ({videoMomentCount})
            </button>
            {selectedKeys.length > 0 && (
              <button type="button" className="studio-btn studio-btn-ghost" onClick={onClearMomentSelection}>
                Clear set ({selectedKeys.length})
              </button>
            )}
            <span className="text-xs font-medium text-muted-foreground">
              {selectedKeys.length > 0
                ? `${selectedKeys.length} in carousel set`
                : `${videoMomentCount} moments available from this video`}
            </span>
          </div>
        )}
      </div>

      {!loading && moments.length === 0 && !error && (
        <div className="px-4 py-10 sm:px-6">
          <p className="text-sm font-medium text-muted-foreground">
            Index videos on Folders, then search above.
          </p>
        </div>
      )}

      {hasResults && !working && (
        <ul className="divide-y divide-border" data-testid="carousel-results-list">
          {moments.map((moment, i) => {
            const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);
            const isPicked = pickedKey === snapshotKeyOf(moment);
            const inSet = selectedKeys.includes(momentKey(moment));
            return (
              <li key={`${moment.drive_file_id}-${moment.timestamp_sec}-${i}`} className="studio-fade-in">
                <div className="flex gap-3 px-4 py-3.5 sm:items-center sm:px-6">
                  <div className="relative h-16 w-28 shrink-0 overflow-hidden rounded-[4px] bg-muted sm:h-20 sm:w-36">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={apiAssetUrl(moment.preview_url)}
                      alt=""
                      className="h-full w-full object-cover"
                    />
                    <span className="absolute bottom-1 left-1 bg-foreground/80 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-white">
                      {timeLabel}
                    </span>
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-semibold text-foreground">{moment.name}</p>
                    <p className="mt-0.5 text-xs font-medium text-muted-foreground">
                      {matchLabel(moment.match_type, moment.score ?? null)}
                      {isPicked ? " · Snapshot" : ""}
                      {inSet ? " · In set" : ""}
                    </p>
                    {moment.snippet && (
                      <p className="mt-1 line-clamp-2 text-xs font-medium text-muted-foreground" title={moment.snippet}>
                        {moment.snippet}
                      </p>
                    )}
                    {(moment.person_names ?? []).length > 0 && (
                      <PersonTags names={moment.person_names ?? []} className="mt-1.5" />
                    )}
                  </div>
                  <div className="flex shrink-0 flex-col gap-1.5 self-center">
                    <button
                      type="button"
                      className="studio-btn studio-btn-accent studio-btn-sm"
                      onClick={() => onSelectForWork(i)}
                    >
                      Select to work
                    </button>
                    <button
                      type="button"
                      className="studio-btn studio-btn-ghost"
                      onClick={() => onToggleMomentSelect(moment)}
                    >
                      {inSet ? (
                        <span className="inline-flex items-center gap-1">
                          <Check size={12} aria-hidden /> In set
                        </span>
                      ) : (
                        "Add to set"
                      )}
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {working && (
        <div className="studio-rise space-y-0" data-testid="carousel-work-surface">
          <div className="border-b border-border px-4 py-3 sm:px-6">
            <p className="text-xs font-medium text-muted-foreground">
              {formatTimestampRange(working.timestamp_sec, working.end_timestamp_sec)} ·{" "}
              {matchLabel(working.match_type, working.score ?? null)} ·{" "}
              {(workingIndex ?? 0) + 1} / {moments.length}
            </p>
          </div>

          <button
            type="button"
            onClick={() => onOpenPreview(working)}
            className="group relative mx-auto block aspect-video w-full max-w-4xl overflow-hidden bg-foreground"
            aria-label={`Play ${working.name}`}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={apiAssetUrl(working.preview_url)}
              alt={working.name}
              className="h-full w-full object-contain transition-opacity group-hover:opacity-90"
            />
            <span className="absolute inset-0 flex items-center justify-center">
              <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <Play size={18} fill="currentColor" aria-hidden />
              </span>
            </span>
          </button>

          <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-4 sm:px-6">
            <button
              type="button"
              className={cn(
                "studio-btn studio-btn-sm",
                pickedKey === snapshotKeyOf(working) ? "studio-btn-accent" : "studio-btn-secondary"
              )}
              onClick={() => onPickSnapshot(working)}
            >
              {pickedKey === snapshotKeyOf(working) ? "Snapshot set" : "Use snapshot"}
            </button>
            <button
              type="button"
              className={cn(
                "studio-btn studio-btn-sm",
                selectedKeys.includes(momentKey(working)) ? "studio-btn-primary" : "studio-btn-ghost"
              )}
              onClick={() => onToggleMomentSelect(working)}
            >
              {selectedKeys.includes(momentKey(working)) ? "In carousel set" : "Add to set"}
            </button>
            <DownloadButton
              url={apiAssetUrl(working.preview_url)}
              filename={`${working.name.replace(/\.[^.]+$/, "")}-${Math.floor(working.timestamp_sec)}s.jpg`}
              label="Frame"
              variant="ghost"
              className="!h-9 !text-xs"
            />
            <DownloadButton
              url={driveFileDownloadUrl(working.drive_file_id)}
              filename={working.name}
              label="Video"
              variant="ghost"
              className="!h-9 !text-xs"
            />
          </div>

          {(working.snippet || (working.person_names ?? []).length > 0) && (
            <div className="space-y-2 border-b border-border px-4 py-3 sm:px-6">
              {working.snippet && (
                <p className="line-clamp-3 text-sm font-medium text-muted-foreground" title={working.snippet}>
                  {working.snippet}
                </p>
              )}
              {(working.person_names ?? []).length > 0 && (
                <PersonTags names={working.person_names ?? []} />
              )}
            </div>
          )}

          <div className="border-b border-border px-4 py-5 sm:px-6">
            <CarouselTranscriptTopics
              driveFileId={working.drive_file_id}
              selectedTitles={transcriptSelectedTitles}
              onToggleTitle={onToggleTranscriptTitle}
              onSeek={(startSec) =>
                onOpenPreview({
                  ...working,
                  timestamp_sec: startSec,
                  end_timestamp_sec: null,
                  snippet: working.snippet,
                })
              }
            />
          </div>

          {/* Same-video moment strip */}
          <div className="flex gap-2 overflow-x-auto border-b border-border px-4 py-4 sm:px-6">
            {moments
              .map((moment, i) => ({ moment, i }))
              .filter(({ moment }) => moment.drive_file_id === working.drive_file_id)
              .map(({ moment, i }) => {
                const selected = i === workingIndex;
                const inSet = selectedKeys.includes(momentKey(moment));
                return (
                  <button
                    key={`${moment.drive_file_id}-${moment.timestamp_sec}-${i}`}
                    type="button"
                    onClick={() => onSelectForWork(i)}
                    className={cn(
                      "w-32 shrink-0 overflow-hidden rounded-[8px] border text-left transition",
                      selected
                        ? "border-foreground"
                        : "border-border hover:border-muted-foreground/40",
                      inSet && !selected && "border-ring"
                    )}
                  >
                    <div className="relative aspect-video bg-muted">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={apiAssetUrl(moment.preview_url)}
                        alt=""
                        className="h-full w-full object-cover"
                      />
                      <span className="absolute bottom-1 left-1 bg-foreground/80 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-white">
                        {formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec)}
                      </span>
                    </div>
                  </button>
                );
              })}
          </div>

          {workSurfaceExtras}
        </div>
      )}
    </section>
  );
}
