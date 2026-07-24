"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import {
  apiClient,
  formatApiError,
  type CarouselTranscriptTopic,
  type CarouselTranscriptTopicsResponse,
} from "@/lib/api";
import { cn } from "@/lib/utils";

function formatTimestamp(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatRange(start: number, end?: number | null): string {
  const startLabel = formatTimestamp(start);
  if (end != null && end > start + 0.5) {
    return `${startLabel}–${formatTimestamp(end)}`;
  }
  return startLabel;
}

type CarouselTranscriptTopicsProps = {
  driveFileId: string | null;
  onSeek?: (startSec: number) => void;
  /** Multi-select topic titles for script cohesion */
  selectedTitles?: string[];
  onToggleTitle?: (title: string) => void;
  className?: string;
};

/**
 * Topic / subtopic timeline from the selected video's indexed transcript.
 * Fetches when driveFileId changes; click a timestamp to seek the preview.
 */
export function CarouselTranscriptTopics({
  driveFileId,
  onSeek,
  selectedTitles = [],
  onToggleTitle,
  className,
}: CarouselTranscriptTopicsProps) {
  const [data, setData] = useState<CarouselTranscriptTopicsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!driveFileId) {
      setData(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    setOpenIds(new Set());

    apiClient
      .analyzeCarouselTranscriptTopics(driveFileId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        if (res.topics[0]) {
          setOpenIds(new Set(["0"]));
        }
        if (res.warning && res.topics.length === 0) {
          setError(res.warning);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(formatApiError(e, "Could not analyze transcript topics"));
        setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [driveFileId]);

  if (!driveFileId) return null;

  function toggle(id: string) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function refresh() {
    if (!driveFileId) return;
    setLoading(true);
    setError(null);
    apiClient
      .analyzeCarouselTranscriptTopics(driveFileId)
      .then((res) => {
        setData(res);
        if (res.warning && res.topics.length === 0) setError(res.warning);
      })
      .catch((e) => setError(formatApiError(e, "Could not analyze transcript topics")))
      .finally(() => setLoading(false));
  }

  return (
    <div className={cn("studio-rise-delay", className)} data-testid="carousel-transcript-topics">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className="studio-section-label">Transcript topics</p>
          <p className="mt-1 text-sm font-medium text-muted-foreground">
            Sections from this video — select themes that should shape the script.
          </p>
        </div>
        <button
          type="button"
          className="studio-btn studio-btn-ghost"
          onClick={refresh}
          disabled={loading}
        >
          <RefreshCw size={12} aria-hidden />
          {loading ? "Analyzing…" : "Refresh"}
        </button>
      </div>

      {loading && !data && (
        <p className="text-sm font-medium text-muted-foreground">Analyzing transcript…</p>
      )}

      {error && (
        <p className="text-xs font-medium text-destructive" role="alert">
          {error}
        </p>
      )}

      {data && data.topics.length === 0 && !loading && (
        <p className="text-sm font-medium text-muted-foreground">
          {data.warning || "No transcript topics available for this video."}
        </p>
      )}

      {data && data.topics.length > 0 && (
        <div className="space-y-1">
          <p className="mb-3 text-xs font-medium text-muted-foreground">
            {data.topics.length} topic{data.topics.length === 1 ? "" : "s"} · {data.cue_count} cues ·{" "}
            {data.source}
          </p>
          <ul className="divide-y divide-border border-y border-border">
            {data.topics.map((topic, i) => (
              <TopicRow
                key={`${topic.title}-${i}`}
                id={String(i)}
                topic={topic}
                open={openIds.has(String(i))}
                onToggle={() => toggle(String(i))}
                onSeek={onSeek}
                selected={selectedTitles.includes(topic.title)}
                onSelect={onToggleTitle ? () => onToggleTitle(topic.title) : undefined}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function TopicRow({
  id,
  topic,
  open,
  onToggle,
  onSeek,
  selected,
  onSelect,
}: {
  id: string;
  topic: CarouselTranscriptTopic;
  open: boolean;
  onToggle: () => void;
  onSeek?: (startSec: number) => void;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const hasSubs = (topic.subtopics?.length ?? 0) > 0;
  return (
    <li className={cn(selected && "bg-muted")}>
      <div className="flex gap-1">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-start gap-2 py-3 text-left"
          aria-expanded={open}
          aria-controls={`topic-body-${id}`}
        >
          <span className="mt-0.5 shrink-0 text-muted-foreground">
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-semibold text-foreground">{topic.title}</span>
            <span className="mt-0.5 block text-[11px] font-medium text-muted-foreground">
              {formatRange(topic.start_sec, topic.end_sec)}
            </span>
          </span>
        </button>
        <div className="flex shrink-0 items-start gap-1 py-3">
          {onSelect && (
            <button
              type="button"
              className={cn(
                "studio-btn studio-btn-sm",
                selected ? "studio-btn-accent" : "studio-btn-ghost"
              )}
              onClick={onSelect}
            >
              {selected ? "Selected" : "Use theme"}
            </button>
          )}
          <button
            type="button"
            className="studio-btn studio-btn-ghost tabular-nums"
            onClick={() => onSeek?.(topic.start_sec)}
            title="Jump to this section"
          >
            {formatRange(topic.start_sec, topic.end_sec)}
          </button>
        </div>
      </div>
      {open && (
        <div id={`topic-body-${id}`} className="space-y-2 border-t border-border pb-3 pl-7 pt-2">
          <p className="text-xs font-medium leading-relaxed text-muted-foreground">{topic.explanation}</p>
          {hasSubs && (
            <ul className="space-y-2 border-l-2 border-ring pl-3">
              {topic.subtopics.map((sub, j) => (
                <li key={`${sub.title}-${j}`}>
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <p className="text-xs font-semibold text-foreground">{sub.title}</p>
                    <button
                      type="button"
                      className="text-[10px] font-semibold text-muted-foreground hover:underline"
                      onClick={() => onSeek?.(sub.start_sec)}
                    >
                      {formatRange(sub.start_sec, sub.end_sec)}
                    </button>
                  </div>
                  <p className="mt-0.5 text-[11px] font-medium leading-relaxed text-muted-foreground">
                    {sub.explanation}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}
