"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Play, X } from "lucide-react";
import {
  API_BASE,
  apiAssetUrl,
  apiClient,
  driveVideoStreamUrl,
  type Person,
  type SearchMoment,
} from "@/lib/api";
import { Button, Card, Input, LoadingLabel, PersonTags, ServiceErrorCard } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";

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

function seekVideoTo(video: HTMLVideoElement, timestampSec: number) {
  const seek = () => {
    try {
      video.currentTime = timestampSec;
      void video.play().catch(() => {});
    } catch {
      /* metadata not ready */
    }
  };
  if (video.readyState >= 1) seek();
  else video.addEventListener("loadedmetadata", seek, { once: true });
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

export default function CarouselSearchPage() {
  const [q, setQ] = useState("");
  const [person, setPerson] = useState("");
  const [persons, setPersons] = useState<Person[]>([]);
  const [moments, setMoments] = useState<SearchMoment[]>([]);
  const [active, setActive] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SearchMoment | null>(null);
  const [addedCount, setAddedCount] = useState<number | null>(null);
  const railRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    apiClient.persons().then(setPersons).catch(() => setPersons([]));
    apiClient
      .youtubeVideos()
      .then((v) => setAddedCount(v.length))
      .catch(() => setAddedCount(null));
  }, []);

  useEffect(() => {
    const el = railRef.current?.querySelector<HTMLElement>(`[data-slide="${active}"]`);
    el?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [active]);

  async function search() {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    setPreview(null);
    try {
      const params = new URLSearchParams({
        q: q.trim(),
        mime: "video",
        source: "youtube",
      });
      if (person) params.set("person", person);
      const res = await fetch(`${API_BASE}/search?${params}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const hits: SearchMoment[] = data.moments ?? [];
      setMoments(hits);
      setActive(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Search failed");
      setMoments([]);
    } finally {
      setLoading(false);
    }
  }

  function step(delta: number) {
    if (!moments.length) return;
    setActive((i) => (i + delta + moments.length) % moments.length);
  }

  const current = moments[active] ?? null;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold sm:text-2xl">Carousel Search</h2>
        <p className="text-sm text-muted-foreground">
          Find moments in videos you added (YouTube library only). Results swipe as a carousel.
          {addedCount != null && (
            <span className="ml-1 text-foreground/80">· {addedCount} added video(s)</span>
          )}
        </p>
      </div>

      <div className="max-w-3xl space-y-2">
        <Input
          className="w-full"
          placeholder="Search added videos (e.g. lecture, whiteboard, person speaking)…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
        />
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground outline-none focus:border-ring sm:max-w-xs"
            value={person}
            onChange={(e) => setPerson(e.target.value)}
            disabled={persons.length === 0}
          >
            <option value="">All people</option>
            {persons.map((p) => (
              <option key={p.id} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <Button className="w-full sm:w-auto" onClick={search} disabled={loading || !q.trim()}>
            {loading ? <LoadingLabel>Searching…</LoadingLabel> : "Search carousel"}
          </Button>
        </div>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground">
          <LoadingLabel size={16}>Searching added videos…</LoadingLabel>
        </p>
      )}

      {error && (
        <ServiceErrorCard message={error} onRetry={search} onDismiss={() => setError(null)} />
      )}

      {!loading && moments.length === 0 && !error && (
        <Card>
          <p className="text-sm text-muted-foreground">
            No moments yet. Add YouTube videos on Folders, wait for indexing, then search here.
          </p>
        </Card>
      )}

      {moments.length > 0 && current && (
        <Card className="space-y-4 overflow-hidden p-0 sm:p-0">
          <div className="border-b border-border px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{current.name}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {formatTimestampRange(current.timestamp_sec, current.end_timestamp_sec)} ·{" "}
                  {matchLabel(current.match_type, current.score ?? null)} · {active + 1} /{" "}
                  {moments.length}
                </p>
              </div>
              <div className="flex shrink-0 gap-1">
                <Button variant="secondary" onClick={() => step(-1)} aria-label="Previous">
                  <ChevronLeft size={16} />
                </Button>
                <Button variant="secondary" onClick={() => step(1)} aria-label="Next">
                  <ChevronRight size={16} />
                </Button>
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={() => setPreview(current)}
            className="group relative mx-auto block aspect-video w-full max-w-4xl overflow-hidden bg-black"
            aria-label={`Play ${current.name}`}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={apiAssetUrl(current.preview_url)}
              alt={current.name}
              className="h-full w-full object-contain transition-opacity group-hover:opacity-90"
            />
            <span className="absolute inset-0 flex items-center justify-center">
              <span className="rounded-full bg-black/70 p-3 text-white shadow-lg">
                <Play size={28} fill="currentColor" aria-hidden />
              </span>
            </span>
          </button>

          {(current.snippet || (current.person_names ?? []).length > 0) && (
            <div className="space-y-2 px-4 pb-2">
              {current.snippet && (
                <p className="line-clamp-3 text-xs text-muted-foreground" title={current.snippet}>
                  {current.snippet}
                </p>
              )}
              {(current.person_names ?? []).length > 0 && (
                <PersonTags names={current.person_names ?? []} />
              )}
            </div>
          )}

          <div
            ref={railRef}
            className="flex gap-3 overflow-x-auto scroll-smooth px-4 pb-4 pt-1 snap-x snap-mandatory"
          >
            {moments.map((moment, i) => {
              const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);
              const selected = i === active;
              return (
                <button
                  key={`${moment.drive_file_id}-${moment.timestamp_sec}-${i}`}
                  type="button"
                  data-slide={i}
                  onClick={() => setActive(i)}
                  className={`w-40 shrink-0 snap-center overflow-hidden rounded-lg border text-left transition ${
                    selected
                      ? "border-primary ring-2 ring-primary/40"
                      : "border-border hover:border-foreground/30"
                  }`}
                >
                  <div className="relative aspect-video bg-black/40">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={apiAssetUrl(moment.preview_url)}
                      alt=""
                      className="h-full w-full object-cover"
                    />
                    <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white">
                      {timeLabel}
                    </span>
                  </div>
                  <div className="px-2 py-1.5">
                    <p className="truncate text-[11px] font-medium">{moment.name}</p>
                    <p className="truncate text-[10px] text-muted-foreground">
                      {matchLabel(moment.match_type, moment.score ?? null)}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </Card>
      )}

      <ModalOverlay open={!!preview} onClose={() => setPreview(null)}>
        {preview && (
          <CarouselPreviewPanel moment={preview} onClose={() => setPreview(null)} />
        )}
      </ModalOverlay>
    </div>
  );
}

function CarouselPreviewPanel({ moment, onClose }: { moment: SearchMoment; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const timeLabel = formatTimestampRange(moment.timestamp_sec, moment.end_timestamp_sec);
  const streamUrl = `${driveVideoStreamUrl(moment.drive_file_id)}#t=${Math.floor(moment.timestamp_sec)}`;
  const [videoError, setVideoError] = useState<string | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    setVideoError(null);
    seekVideoTo(video, moment.timestamp_sec);
  }, [moment.drive_file_id, moment.timestamp_sec]);

  return (
    <div className="relative flex max-h-[min(88dvh,720px)] flex-col overflow-hidden rounded-lg bg-card shadow-2xl">
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute right-3 top-3 z-10 rounded-md bg-black/60 p-2 text-white hover:bg-black/80"
      >
        <X size={16} />
      </button>
      <div className="shrink-0 bg-black">
        <video
          ref={videoRef}
          src={streamUrl}
          controls
          playsInline
          preload="metadata"
          className="max-h-[min(48dvh,420px)] w-full object-contain"
          onError={() => setVideoError("Video preview unavailable.")}
        />
        {videoError && <p className="px-4 py-2 text-xs text-destructive">{videoError}</p>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto border-t border-border px-4 py-3">
        <p className="break-all text-sm font-medium">{moment.name}</p>
        <p className="mt-1 text-xs text-muted-foreground">Moment at {timeLabel}</p>
        {moment.snippet && (
          <p className="mt-2 text-xs text-muted-foreground">{moment.snippet}</p>
        )}
        {(moment.person_names ?? []).length > 0 && (
          <PersonTags names={moment.person_names ?? []} className="mt-2" />
        )}
      </div>
    </div>
  );
}
