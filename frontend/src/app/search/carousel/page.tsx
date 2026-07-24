"use client";

/**
 * Video Carousel — studio flow (on this page):
 * 1 Select captioned video (recents + search) + optional person filter
 * 2 Themes (normal video themes; person only gates presence)
 * 3 Hooks & topics
 * 4 Preview markers + directional intent (no script writing)
 * 5 Generate carousel cards
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Play, Search, X } from "lucide-react";
import {
  apiAssetUrl,
  apiClient,
  driveFileDownloadUrl,
  driveVideoStreamUrl,
  formatApiError,
  type CarouselOutlineResponse,
  type CarouselOutlineSlide,
  type CarouselPipelineExtractResponse,
  type CarouselPipelineTheme,
  type CarouselRecentVideo,
  type CarouselSnapshotContext,
  type CarouselVerbatimItem,
  type Person,
} from "@/lib/api";
import { DownloadButton, LoadingLabel, ServiceErrorCard } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { cn } from "@/lib/utils";
import { formatTimestampRange } from "./utils";

type Phase = 1 | 2 | 3 | 4 | 5;

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

function fmtTs(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Prefer a complete phrase — never leave titles ending mid-clause. */
function completePhrase(raw: string | null | undefined, maxWords = 12): string {
  const text = (raw || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  const sentence = text.split(/(?<=[.!?])\s+/)[0]?.trim() || text;
  const words = sentence.split(" ");
  if (words.length <= maxWords && /[.!?]$/.test(sentence)) return sentence;
  if (words.length <= maxWords && !looksIncomplete(sentence)) return sentence;
  const cut = words.slice(0, maxWords);
  while (cut.length > 4 && looksIncomplete(cut.join(" "))) cut.pop();
  let out = cut.join(" ");
  if (!/[.!?]$/.test(out) && !looksIncomplete(out)) return out;
  if (!/[.!?]$/.test(out)) out = out.replace(/[,:;–—-]+$/, "").trim();
  return out;
}

function looksIncomplete(text: string): boolean {
  const t = text.trim();
  if (!t) return true;
  if (/[.!?]"?$/.test(t)) return false;
  return /\b(to|be|in|on|at|of|for|and|or|the|a|an|with|from|as|is|are|was|were|their|our|my)$/i.test(
    t
  );
}

function toggleText(list: string[], text: string): string[] {
  return list.includes(text) ? list.filter((x) => x !== text) : [...list, text];
}

/** Mid-span frame when end is known; otherwise spoken-window start. */
function framePreviewUrl(
  driveFileId: string,
  startSec: number,
  endSec?: number | null
): string {
  const start = Number(startSec) || 0;
  const end = endSec != null ? Number(endSec) : NaN;
  const ts =
    Number.isFinite(end) && end > start ? Math.round((start + (end - start) * 0.5) * 100) / 100 : start;
  return `/media/video/${driveFileId}/frame?ts=${ts}`;
}

function resolvePick(
  text: string,
  items: CarouselVerbatimItem[]
): CarouselVerbatimItem | undefined {
  const exact = items.find((x) => x.text === text);
  if (exact) return exact;
  const lower = text.toLowerCase();
  return items.find(
    (x) => x.text.toLowerCase() === lower || x.text.includes(text) || text.includes(x.text)
  );
}

function pickToMoment(
  text: string,
  items: CarouselVerbatimItem[],
  kind: "hook" | "topic",
  video: CarouselRecentVideo,
  themes: CarouselPipelineTheme[]
): CarouselSnapshotContext {
  const item = resolvePick(text, items);
  const covering =
    themes.find(
      (t) =>
        item != null &&
        item.start_sec >= t.start_sec - 0.05 &&
        (t.end_sec == null || item.start_sec <= Number(t.end_sec) + 0.25)
    ) ?? themes[0];
  const start = item?.start_sec ?? covering?.start_sec ?? 0;
  const end = item?.end_sec ?? covering?.end_sec ?? null;
  return {
    drive_file_id: video.id,
    name: video.name,
    timestamp_sec: start,
    end_timestamp_sec: end,
    snippet: text,
    match_type: kind,
    preview_url: framePreviewUrl(video.id, start, end),
  };
}

function fallbackTheme(themes: CarouselPipelineTheme[]): CarouselPipelineTheme {
  return (
    themes[0] ?? {
      theme_id: "theme",
      title: "Theme",
      start_sec: 0,
      end_sec: null,
      summary: "",
    }
  );
}

function toggleTheme(
  themes: CarouselPipelineTheme[],
  theme: CarouselPipelineTheme
): CarouselPipelineTheme[] {
  const exists = themes.some((t) => t.theme_id === theme.theme_id);
  const next = exists
    ? themes.filter((t) => t.theme_id !== theme.theme_id)
    : [...themes, theme];
  return next.sort((a, b) => a.start_sec - b.start_sec);
}

export default function CarouselSearchPage() {
  const [phase, setPhase] = useState<Phase>(1);
  const [recent, setRecent] = useState<CarouselRecentVideo[]>([]);
  const [persons, setPersons] = useState<Person[]>([]);
  const [loadingRecent, setLoadingRecent] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [personNotFound, setPersonNotFound] = useState<string | null>(null);

  const [videoScope, setVideoScope] = useState<"recent" | "all">("recent");
  const [videoQuery, setVideoQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [allVideos, setAllVideos] = useState<CarouselRecentVideo[]>([]);
  const [loadingAll, setLoadingAll] = useState(false);
  const [allVideosError, setAllVideosError] = useState<string | null>(null);

  const [selectedVideo, setSelectedVideo] = useState<CarouselRecentVideo | null>(null);
  const [searchEntity, setSearchEntity] = useState("");
  const [personPick, setPersonPick] = useState("");
  const [objectQuery, setObjectQuery] = useState("");

  const [themes, setThemes] = useState<CarouselPipelineTheme[]>([]);
  const [loadingThemes, setLoadingThemes] = useState(false);
  /** True while waiting for selection to settle before calling themes API. */
  const [themesWaiting, setThemesWaiting] = useState(false);
  const [selectedThemes, setSelectedThemes] = useState<CarouselPipelineTheme[]>([]);
  const themesAbortRef = useRef<AbortController | null>(null);
  const themesRequestKeyRef = useRef<string>("");

  const [extract, setExtract] = useState<CarouselPipelineExtractResponse | null>(null);
  const [loadingExtract, setLoadingExtract] = useState(false);
  const [selectedHooks, setSelectedHooks] = useState<string[]>([]);
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [phaseIntent, setPhaseIntent] = useState<string | null>(null);
  const [phaseIntentScore, setPhaseIntentScore] = useState<number | null>(null);
  const [loadingIntent, setLoadingIntent] = useState(false);

  const [previewCue, setPreviewCue] = useState<{ start_sec: number; text: string } | null>(null);
  const [building, setBuilding] = useState(false);
  const [outline, setOutline] = useState<CarouselOutlineResponse | null>(null);
  const [outlineError, setOutlineError] = useState<string | null>(null);
  const outlineRef = useRef<HTMLDivElement>(null);

  const entityLabel = useMemo(() => {
    const fromPerson = personPick.trim();
    const fromObject = objectQuery.trim();
    if (fromPerson && fromObject) return `${fromPerson} / ${fromObject}`;
    return fromPerson || fromObject || searchEntity.trim();
  }, [personPick, objectQuery, searchEntity]);

  /** Phase 4 markers: selected hooks/topics (+ theme anchors), never raw first-theme dumps. */
  const selectionPreviewMarkers = useMemo(() => {
    if (!extract) return [];
    type Marker = { start_sec: number; end_sec?: number | null; text: string; label: string };
    const markers: Marker[] = [];
    for (const text of selectedHooks) {
      const item = resolvePick(text, extract.hooks);
      if (!item) continue;
      markers.push({
        start_sec: item.start_sec,
        end_sec: item.end_sec,
        text: item.text,
        label: "Hook",
      });
    }
    for (const text of selectedTopics) {
      const item = resolvePick(text, extract.topics);
      if (!item) continue;
      markers.push({
        start_sec: item.start_sec,
        end_sec: item.end_sec,
        text: item.text,
        label: "Topic",
      });
    }
    // Theme anchors for selected themes that have no hook/topic yet (context only).
    for (const theme of selectedThemes) {
      const covered = markers.some(
        (m) =>
          m.start_sec >= theme.start_sec - 0.05 &&
          (theme.end_sec == null || m.start_sec <= Number(theme.end_sec) + 0.25)
      );
      if (covered) continue;
      markers.push({
        start_sec: theme.start_sec,
        end_sec: theme.end_sec,
        text: completePhrase(theme.title) || theme.summary || "Theme",
        label: "Theme",
      });
    }
    return markers.sort((a, b) => a.start_sec - b.start_sec);
  }, [extract, selectedHooks, selectedTopics, selectedThemes]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoadingRecent(true);
      try {
        const [vids, people] = await Promise.all([
          apiClient.carouselRecentVideos(5, true),
          apiClient.persons().catch(() => [] as Person[]),
        ]);
        if (cancelled) return;
        setRecent(vids.items ?? []);
        setPersons(people);
      } catch (e) {
        if (!cancelled) setError(formatApiError(e, "Could not load recent videos"));
      } finally {
        if (!cancelled) setLoadingRecent(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(videoQuery.trim()), 250);
    return () => window.clearTimeout(t);
  }, [videoQuery]);

  useEffect(() => {
    if (videoScope !== "all") return;
    let cancelled = false;
    (async () => {
      setLoadingAll(true);
      setAllVideosError(null);
      try {
        const res = await apiClient.carouselVideos({
          q: debouncedQuery || undefined,
          limit: 30,
          captionedOnly: true,
        });
        if (!cancelled) setAllVideos(res.items ?? []);
      } catch (e) {
        if (!cancelled) {
          setAllVideos([]);
          setAllVideosError(formatApiError(e, "Could not load captioned videos"));
        }
      } finally {
        if (!cancelled) setLoadingAll(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [debouncedQuery, videoScope]);

  const resetFromPhase2 = useCallback(() => {
    setSelectedThemes([]);
    setExtract(null);
    setSelectedHooks([]);
    setSelectedTopics([]);
    setPhaseIntent(null);
    setPhaseIntentScore(null);
    setPreviewCue(null);
    setOutline(null);
    setOutlineError(null);
    setPersonNotFound(null);
  }, []);

  /** Debounced theme load: wait ~4s after video/person/object settle; cancel on change. */
  useEffect(() => {
    if (!selectedVideo) {
      setThemesWaiting(false);
      setLoadingThemes(false);
      themesAbortRef.current?.abort();
      themesAbortRef.current = null;
      return;
    }

    const video = selectedVideo;
    const personName = personPick.trim();
    const fromObject = objectQuery.trim();
    const entity =
      personName && fromObject
        ? `${personName} / ${fromObject}`
        : personName || fromObject || "";
    const requestKey = `${video.id}|${personName}|${entity}`;

    // Selection changed — drop stale themes immediately; keep Phase 1 usable.
    themesAbortRef.current?.abort();
    themesAbortRef.current = null;
    setThemes([]);
    resetFromPhase2();
    setLoadingThemes(false);
    setThemesWaiting(true);
    setError(null);
    setPhase(1);
    setSearchEntity(entity);

    const timer = window.setTimeout(() => {
      const ac = new AbortController();
      themesAbortRef.current = ac;
      themesRequestKeyRef.current = requestKey;
      setThemesWaiting(false);
      setLoadingThemes(true);
      setPersonNotFound(null);

      void (async () => {
        try {
          const res = await apiClient.carouselPipelineThemes(video.id, {
            personName: personName || undefined,
            searchEntity: entity || undefined,
            signal: ac.signal,
          });
          if (ac.signal.aborted || themesRequestKeyRef.current !== requestKey) return;

          if (res.error === "person_not_found" || res.person_found === false) {
            const msg =
              res.message ||
              res.warning ||
              "Person not found in this video. Try without that person or change video.";
            setPersonNotFound(msg);
            setThemes([]);
            setPhase(1);
            return;
          }
          setThemes(res.themes ?? []);
          if (res.warning) setError(res.warning);
          setPhase(2);
        } catch (e) {
          if (ac.signal.aborted || themesRequestKeyRef.current !== requestKey) return;
          if (e instanceof Error && e.name === "AbortError") return;
          setError(formatApiError(e, "Theme segmentation failed"));
          setThemes([]);
        } finally {
          if (!ac.signal.aborted && themesRequestKeyRef.current === requestKey) {
            setLoadingThemes(false);
          }
        }
      })();
    }, 4000);

    return () => {
      window.clearTimeout(timer);
      themesAbortRef.current?.abort();
      themesAbortRef.current = null;
    };
  }, [selectedVideo, personPick, objectQuery, resetFromPhase2]);

  function selectVideo(video: CarouselRecentVideo) {
    // Always allow switching; debounce effect handles themes when selection settles.
    setSelectedVideo(video);
  }

  function onToggleTheme(theme: CarouselPipelineTheme) {
    setSelectedThemes((prev) => toggleTheme(prev, theme));
    setExtract(null);
    setSelectedHooks([]);
    setSelectedTopics([]);
    setPhaseIntent(null);
    setPhaseIntentScore(null);
    setOutline(null);
    setOutlineError(null);
    if (phase > 2) setPhase(2);
  }

  async function extractFromSelectedThemes() {
    if (!selectedVideo) return;
    if (!selectedThemes.length) {
      setError("Select at least one theme.");
      return;
    }
    setLoadingExtract(true);
    setError(null);
    setOutline(null);
    setPhaseIntent(null);
    setPhaseIntentScore(null);
    try {
      const ordered = [...selectedThemes].sort((a, b) => a.start_sec - b.start_sec);
      const res = await apiClient.carouselPipelineExtract({
        drive_file_id: selectedVideo.id,
        search_entity: searchEntity || undefined,
        themes: ordered.map((t) => ({
          theme_id: t.theme_id,
          title: t.title,
          start_sec: t.start_sec,
          end_sec: t.end_sec,
          summary: t.summary,
        })),
      });
      setExtract(res);
      setSelectedHooks((res.hooks ?? []).slice(0, 3).map((h) => h.text));
      setSelectedTopics((res.topics ?? []).slice(0, 3).map((t) => t.text));
      setPhaseIntent(res.intent ?? null);
      setPhaseIntentScore(res.intent_score ?? null);
      setPhase(3);
    } catch (e) {
      setError(formatApiError(e, "Hook & topic extract failed"));
      setExtract(null);
    } finally {
      setLoadingExtract(false);
    }
  }

  async function goToPreviewIntent() {
    if (!selectedHooks.length && !selectedTopics.length) {
      setError("Select at least one hook or topic.");
      return;
    }
    setError(null);
    setPhase(4);
    setLoadingIntent(true);
    try {
      const intentRes = await apiClient.carouselPipelineIntent({
        theme_titles: selectedThemes.map((t) => t.title),
        theme_summaries: selectedThemes.map((t) => t.summary),
        theme_title: selectedThemes.map((t) => t.title).join(" → "),
        theme_summary: selectedThemes.map((t) => t.summary).filter(Boolean).join(" "),
        hooks: selectedHooks,
        topics: selectedTopics,
        search_entity: searchEntity || undefined,
      });
      setPhaseIntent(intentRes.intent ?? null);
      setPhaseIntentScore(intentRes.intent_score ?? null);
    } catch {
      if (!phaseIntent && extract?.intent) setPhaseIntent(extract.intent);
    } finally {
      setLoadingIntent(false);
    }
  }

  async function generateCarousel() {
    if (!selectedVideo || !selectedThemes.length || !extract) return;
    setBuilding(true);
    setOutlineError(null);
    try {
      const moments: CarouselSnapshotContext[] = [
        ...selectedHooks.map((text) =>
          pickToMoment(text, extract.hooks, "hook", selectedVideo, selectedThemes)
        ),
        ...selectedTopics.map((text) =>
          pickToMoment(text, extract.topics, "topic", selectedVideo, selectedThemes)
        ),
      ].sort((a, b) => a.timestamp_sec - b.timestamp_sec);

      if (!moments.length) {
        const first = fallbackTheme(selectedThemes);
        moments.push({
          drive_file_id: selectedVideo.id,
          name: selectedVideo.name,
          timestamp_sec: first.start_sec,
          end_timestamp_sec: first.end_sec ?? null,
          snippet: completePhrase(first.summary) || first.title,
          match_type: "theme",
          preview_url: framePreviewUrl(selectedVideo.id, first.start_sec, first.end_sec),
        });
      }

      const intentLine = phaseIntent || extract.intent;
      const scriptParts = [
        intentLine ? `Intent: ${intentLine}` : "",
        ...selectedThemes.map(
          (t) => `Theme: ${completePhrase(t.title) || t.title} — ${t.summary || ""}`.trim()
        ),
        ...selectedHooks.map((h) => `Hook: ${h}`),
        ...selectedTopics.map((t) => `Topic: ${t}`),
      ].filter(Boolean);

      const slideCount = Math.min(8, Math.max(1, moments.length));
      const videoBase = selectedVideo.name
        .replace(/\.[^.]+$/, "")
        .replace(/\s*\[[^\]]+\]\s*$/, "")
        .trim();
      const themeLabel =
        selectedThemes
          .map((t) => completePhrase(t.title) || completePhrase(t.summary) || t.title)
          .filter(Boolean)
          .join(" · ") || "Carousel";
      const res = await apiClient.generateCarouselOutline({
        script: scriptParts.join("\n"),
        moments,
        hooks: selectedHooks,
        topics: selectedTopics,
        slide_count: slideCount,
        title: `${videoBase} — ${themeLabel}`.slice(0, 180),
      });
      setOutline(res);
      setPhase(5);
      requestAnimationFrame(() => {
        outlineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } catch (e) {
      setOutlineError(formatApiError(e, "Carousel generation failed"));
      setOutline(null);
    } finally {
      setBuilding(false);
    }
  }

  return (
    <div className="carousel-studio mx-auto max-w-5xl space-y-6 pb-16">
      <header>
        <p className="studio-eyebrow">Search · Video carousel</p>
        <h1 className="studio-title">Video carousel</h1>
        <p className="studio-lede">
          Pick a captioned video, segment themes, pull hooks, preview intent, then build slides.
          Optional person filter only checks whether they appear — themes stay normal for the video.
        </p>
        <PhaseRail phase={phase} />
      </header>

      {error && <ServiceErrorCard message={error} onDismiss={() => setError(null)} />}

      <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-1">
        <p className="studio-section-label">1 · Select video</p>
        <h2 className="mt-1 text-base font-semibold text-foreground">Captioned videos</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Pick a captioned video (recent or search all). Optional person filter checks presence in
          that video only.
        </p>

        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          <select
            className="studio-select"
            value={personPick}
            onChange={(e) => setPersonPick(e.target.value)}
            disabled={persons.length === 0}
          >
            <option value="">Person (optional)</option>
            {persons.map((p) => (
              <option key={p.id} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          <input
            className="studio-input"
            placeholder="Object / topic context (optional)"
            value={objectQuery}
            onChange={(e) => setObjectQuery(e.target.value)}
          />
        </div>

        {personNotFound && (
          <div
            className="mt-4 rounded-lg border border-border bg-muted/50 px-3 py-3 text-sm text-foreground"
            role="status"
          >
            {personNotFound}
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <div
            className="inline-flex rounded-[calc(var(--radius)-2px)] border border-border bg-background p-0.5"
            role="group"
            aria-label="Video list filter"
          >
            {(
              [
                { id: "recent", label: "Recent" },
                { id: "all", label: "All" },
              ] as const
            ).map((opt) => {
              const active = videoScope === opt.id;
              return (
                <button
                  key={opt.id}
                  type="button"
                  className={cn(
                    "h-7 rounded-[calc(var(--radius)-4px)] px-3 text-xs font-semibold transition",
                    active
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                  aria-pressed={active}
                  onClick={() => setVideoScope(opt.id)}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          {videoScope === "all" && (
            <div className="relative min-w-[10rem] flex-1">
              <Search
                size={14}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
              />
              <input
                className="studio-input w-full pl-8"
                placeholder="Search by title…"
                value={videoQuery}
                onChange={(e) => setVideoQuery(e.target.value)}
                aria-label="Search captioned videos by title"
              />
            </div>
          )}
        </div>

        <div className="mt-2">
          {videoScope === "recent" ? (
            loadingRecent ? (
              <p className="text-sm text-muted-foreground">
                <LoadingLabel>Loading videos…</LoadingLabel>
              </p>
            ) : recent.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No captioned videos yet — index a video with transcript cues, then refresh.
              </p>
            ) : (
              <VideoPickList
                videos={recent}
                selectedId={selectedVideo?.id}
                onSelect={selectVideo}
                maxHeightClass="max-h-56"
              />
            )
          ) : loadingAll ? (
            <p className="text-sm text-muted-foreground">
              <LoadingLabel>Searching…</LoadingLabel>
            </p>
          ) : allVideosError ? (
            <p className="text-sm text-destructive" role="alert">
              {allVideosError}
            </p>
          ) : allVideos.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {debouncedQuery
                ? "No captioned videos match that title."
                : "No captioned videos available."}
            </p>
          ) : (
            <VideoPickList
              videos={allVideos}
              selectedId={selectedVideo?.id}
              onSelect={selectVideo}
              maxHeightClass="max-h-56"
            />
          )}
        </div>

        {selectedVideo && (
          <p className="mt-2 truncate text-xs text-muted-foreground">
            Selected:{" "}
            <span className="font-medium text-foreground">{selectedVideo.name}</span>
            {themesWaiting ? " · themes start in a few seconds…" : ""}
          </p>
        )}
      </section>

      {selectedVideo && !personNotFound && (
        <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-2">
          <div>
            <p className="studio-section-label">2 · Themes</p>
            <h2 className="mt-1 text-base font-semibold text-foreground">Narrative themes</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Non-overlapping segments from this video
              {personPick.trim() ? ` · “${personPick.trim()}” appears here` : ""}. Select one or more
              themes, then extract hooks & topics from the combined set.
            </p>
          </div>

          {themesWaiting ? (
            <p className="mt-4 text-sm text-muted-foreground">
              Waiting for your selection to settle — you can still change video or person.
            </p>
          ) : loadingThemes ? (
            <p className="mt-4 text-sm text-muted-foreground">
              <LoadingLabel>
                {personPick.trim()
                  ? `Checking “${personPick.trim()}” in video, then segmenting themes…`
                  : "Segmenting themes…"}
              </LoadingLabel>
            </p>
          ) : themes.length === 0 ? (
            <p className="mt-4 text-sm text-muted-foreground">No themes for this video.</p>
          ) : (
            <ul className="mt-4 space-y-2">
              {themes.map((t) => {
                const active = selectedThemes.some((x) => x.theme_id === t.theme_id);
                return (
                  <li key={t.theme_id}>
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={active}
                      className={cn(
                        "flex w-full items-start gap-3 rounded-lg border px-3 py-3 text-left transition",
                        active
                          ? "border-foreground bg-muted"
                          : "border-border hover:border-muted-foreground/40"
                      )}
                      onClick={() => onToggleTheme(t)}
                      disabled={loadingExtract}
                    >
                      <span
                        className={cn(
                          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded border",
                          active
                            ? "border-foreground bg-foreground text-background"
                            : "border-border"
                        )}
                      >
                        {active ? <Check size={12} /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="text-sm font-semibold text-foreground">{t.title}</span>
                          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                            {fmtTs(t.start_sec)}
                            {t.end_sec != null ? `–${fmtTs(t.end_sec)}` : ""}
                          </span>
                        </span>
                        <span className="mt-1 block line-clamp-2 text-xs text-muted-foreground">
                          {t.summary}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="studio-btn studio-btn-primary"
              onClick={() => void extractFromSelectedThemes()}
              disabled={
                loadingExtract ||
                loadingThemes ||
                themesWaiting ||
                selectedThemes.length === 0
              }
            >
              {loadingExtract ? (
                <LoadingLabel>Extracting hooks & generating topics…</LoadingLabel>
              ) : selectedThemes.length > 1 ? (
                `Extract from ${selectedThemes.length} themes`
              ) : (
                "Extract hooks & topics"
              )}
            </button>
            {selectedThemes.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {selectedThemes.length} theme{selectedThemes.length === 1 ? "" : "s"} selected
              </p>
            )}
          </div>
        </section>
      )}

      {extract && selectedThemes.length > 0 && phase >= 3 && (
        <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-3">
          <p className="studio-section-label">3 · Hooks & topics</p>
          <h2 className="mt-1 text-base font-semibold text-foreground">
            Full-context hooks · theme-generated topics
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Merged from{" "}
            {selectedThemes.length === 1
              ? `“${selectedThemes[0].title}”`
              : `${selectedThemes.length} selected themes`}{" "}
            · toggle any combination to continue.
            {extract.any_translated ? " Some hooks were translated for display." : ""}
          </p>
          <div className="mt-4 grid gap-6 sm:grid-cols-2">
            <VerbatimList
              label="Hooks (spoken, full context)"
              items={extract.hooks}
              selected={selectedHooks}
              onToggle={(text) => setSelectedHooks((prev) => toggleText(prev, text))}
              onPreview={(item) => setPreviewCue({ start_sec: item.start_sec, text: item.text })}
            />
            <VerbatimList
              label="Topics (from selected themes)"
              items={extract.topics}
              selected={selectedTopics}
              onToggle={(text) => setSelectedTopics((prev) => toggleText(prev, text))}
              onPreview={(item) => setPreviewCue({ start_sec: item.start_sec, text: item.text })}
              quote={false}
            />
          </div>
          <div className="mt-4">
            <button
              type="button"
              className="studio-btn studio-btn-primary"
              onClick={() => void goToPreviewIntent()}
              disabled={loadingIntent}
            >
              {loadingIntent ? (
                <LoadingLabel>Updating intent…</LoadingLabel>
              ) : (
                "Continue to preview & intent"
              )}
            </button>
          </div>
        </section>
      )}

      {extract && selectedThemes.length > 0 && phase >= 4 && (
        <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-4">
          <p className="studio-section-label">4 · Preview & intent</p>
          <h2 className="mt-1 text-base font-semibold text-foreground">Where it happens</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Markers from your selected hooks & topics
            {selectedThemes.length > 1
              ? ` across ${selectedThemes.length} themes`
              : ` in “${selectedThemes[0]?.title ?? "theme"}”`}
            . Intent is directional only — no script is written here.
          </p>

          {(phaseIntent || extract.intent) && (
            <div className="mt-4 rounded-lg border border-border bg-muted/40 px-3 py-3">
              <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                Directional intent
                {(phaseIntentScore ?? extract.intent_score) != null
                  ? ` · score ${Math.round(Number(phaseIntentScore ?? extract.intent_score) * 100)}%`
                  : ""}
                {loadingIntent ? " · updating…" : ""}
              </p>
              <p className="mt-1 text-sm font-medium text-foreground">
                {phaseIntent || extract.intent}
              </p>
            </div>
          )}

          <ul className="mt-4 max-h-56 space-y-1 overflow-y-auto rounded-lg border border-border">
            {selectionPreviewMarkers.length === 0 ? (
              <li className="px-3 py-2 text-xs text-muted-foreground">
                No selected hooks or topics yet.
              </li>
            ) : (
              selectionPreviewMarkers.map((p) => (
                <li key={`${p.label}-${p.start_sec}-${p.text.slice(0, 40)}`}>
                  <button
                    type="button"
                    className="flex w-full items-start gap-2 px-3 py-2 text-left text-sm hover:bg-muted/60"
                    onClick={() => setPreviewCue({ start_sec: p.start_sec, text: p.text })}
                  >
                    <Play size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0">
                      <span className="font-semibold tabular-nums text-foreground">
                        {fmtTs(p.start_sec)}
                      </span>
                      <span className="ml-2 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
                        {p.label}
                      </span>
                      <span className="mt-0.5 block line-clamp-2 text-xs text-muted-foreground">
                        {p.text}
                      </span>
                    </span>
                  </button>
                </li>
              ))
            )}
          </ul>

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              className="studio-btn studio-btn-accent"
              onClick={() => void generateCarousel()}
              disabled={building || (!selectedHooks.length && !selectedTopics.length)}
            >
              {building ? <LoadingLabel>Building carousel…</LoadingLabel> : "Generate carousel"}
            </button>
          </div>
        </section>
      )}

      <div ref={outlineRef}>
        {(phase >= 5 || outline) && (
          <section className="studio-panel space-y-4 p-4 sm:p-6" data-testid="carousel-phase-5">
            <div>
              <p className="studio-section-label">5 · Carousel</p>
              <h2 className="mt-1 text-base font-semibold text-foreground">
                {outline?.title || "Carousel cards"}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Instagram-style pager — swipe or use arrows; each card keeps exact hook text and its
                frame.
              </p>
            </div>

            {outlineError && (
              <p className="text-xs font-medium text-destructive" role="alert">
                {outlineError}
              </p>
            )}

            {outline && (
              <InstagramCarouselPost
                title={outline.title}
                slides={outline.slides}
                onOpenSlide={(slide) =>
                  setPreviewCue({
                    start_sec: slide.timestamp_sec,
                    text: slide.hook_line,
                  })
                }
              />
            )}
          </section>
        )}
      </div>

      <ModalOverlay open={!!previewCue && !!selectedVideo} onClose={() => setPreviewCue(null)}>
        {previewCue && selectedVideo && (
          <ThemePreviewModal
            videoId={selectedVideo.id}
            videoName={selectedVideo.name}
            startSec={previewCue.start_sec}
            text={previewCue.text}
            onClose={() => setPreviewCue(null)}
          />
        )}
      </ModalOverlay>
    </div>
  );
}

function InstagramCarouselPost({
  title,
  slides,
  onOpenSlide,
}: {
  title: string;
  slides: CarouselOutlineSlide[];
  onOpenSlide: (slide: CarouselOutlineSlide) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const n = slides.length;
  const current = slides[Math.min(Math.max(active, 0), Math.max(n - 1, 0))];

  useEffect(() => {
    setActive(0);
    const el = trackRef.current;
    if (el) el.scrollTo({ left: 0, behavior: "auto" });
  }, [slides]);

  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const onScroll = () => {
      const w = el.clientWidth || 1;
      const idx = Math.round(el.scrollLeft / w);
      setActive(Math.min(Math.max(idx, 0), Math.max(n - 1, 0)));
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [n]);

  function goTo(index: number) {
    const el = trackRef.current;
    if (!el || n <= 0) return;
    const clamped = Math.min(Math.max(index, 0), n - 1);
    el.scrollTo({ left: clamped * el.clientWidth, behavior: "smooth" });
    setActive(clamped);
  }

  if (!n || !current) {
    return <p className="text-sm text-muted-foreground">No slides to show.</p>;
  }

  return (
    <div className="ig-post studio-fade-in" data-testid="ig-carousel-post">
      <div className="ig-post-header">
        <p className="ig-post-title" title={title}>
          {title}
        </p>
        <span className="ig-post-count" aria-live="polite">
          {active + 1}/{n}
        </span>
      </div>

      <div className="ig-stage">
        <div
          ref={trackRef}
          className="ig-track"
          role="region"
          aria-roledescription="carousel"
          aria-label="Carousel slides"
        >
          {slides.map((slide, i) => (
            <article
              key={`${slide.index}-${slide.drive_file_id}-${slide.timestamp_sec}`}
              className="ig-slide"
              aria-label={`Slide ${i + 1} of ${n}`}
              aria-hidden={i !== active}
            >
              {slide.preview_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={apiAssetUrl(slide.preview_url)} alt="" draggable={false} />
              ) : (
                <div className="ig-slide-empty">No frame</div>
              )}
              <div className="ig-slide-scrim" aria-hidden />
              <div className="ig-slide-body">
                <p className="ig-slide-hook">{slide.hook_line}</p>
                <p className="ig-slide-meta">
                  {formatTimestampRange(slide.timestamp_sec, slide.end_timestamp_sec)}
                  {slide.match_type ? ` · ${slide.match_type}` : ""}
                  {slide.frame_source === "ai" ? " · AI frame" : ""}
                  {slide.frame_source === "fallback" ? " · fallback" : ""}
                </p>
              </div>
            </article>
          ))}
        </div>

        {n > 1 && (
          <>
            <button
              type="button"
              className="ig-nav ig-nav-prev"
              aria-label="Previous slide"
              disabled={active <= 0}
              onClick={() => goTo(active - 1)}
            >
              <ChevronLeft size={18} strokeWidth={2.25} />
            </button>
            <button
              type="button"
              className="ig-nav ig-nav-next"
              aria-label="Next slide"
              disabled={active >= n - 1}
              onClick={() => goTo(active + 1)}
            >
              <ChevronRight size={18} strokeWidth={2.25} />
            </button>
          </>
        )}
      </div>

      {n > 1 && (
        <div className="ig-dots" role="tablist" aria-label="Slide position">
          {slides.map((slide, i) => (
            <button
              key={`dot-${slide.index}-${i}`}
              type="button"
              className="ig-dot"
              role="tab"
              aria-label={`Go to slide ${i + 1}`}
              aria-selected={i === active}
              data-on={i === active ? "true" : "false"}
              onClick={() => goTo(i)}
            />
          ))}
        </div>
      )}

      <div className="ig-caption-row">
        <p>
          <strong>{title.split("—")[0]?.trim() || "Carousel"}</strong>
          {" · "}
          {current.hook_line}
        </p>
      </div>

      {n > 1 && (
        <div className="ig-filmstrip" aria-label="Slide filmstrip">
          {slides.map((slide, i) => (
            <button
              key={`thumb-${slide.index}-${i}`}
              type="button"
              className="ig-thumb"
              data-on={i === active ? "true" : "false"}
              aria-label={`Slide ${i + 1}`}
              aria-current={i === active ? "true" : undefined}
              onClick={() => goTo(i)}
            >
              {slide.preview_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={apiAssetUrl(slide.preview_url)} alt="" draggable={false} />
              ) : null}
              <span className="ig-thumb-num">{i + 1}</span>
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 flex justify-center">
        <button
          type="button"
          className="studio-btn studio-btn-ghost studio-btn-sm"
          onClick={() => onOpenSlide(current)}
        >
          <Play size={14} />
          Open clip at this moment
        </button>
      </div>
    </div>
  );
}

function VideoPickList({
  videos,
  selectedId,
  onSelect,
  disabled,
  maxHeightClass = "max-h-72",
}: {
  videos: CarouselRecentVideo[];
  selectedId?: string;
  onSelect: (v: CarouselRecentVideo) => void;
  disabled?: boolean;
  maxHeightClass?: string;
}) {
  return (
    <ul
      className={cn(
        "mt-2 divide-y divide-border overflow-y-auto rounded-lg border border-border",
        maxHeightClass
      )}
    >
      {videos.map((v) => {
        const active = selectedId === v.id;
        return (
          <li key={v.id}>
            <button
              type="button"
              className={cn(
                "flex w-full items-start gap-3 px-3 py-2.5 text-left transition hover:bg-muted/60",
                active && "bg-muted"
              )}
              onClick={() => onSelect(v)}
              disabled={disabled}
            >
              <span
                className={cn(
                  "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded border",
                  active ? "border-foreground bg-foreground text-background" : "border-border"
                )}
              >
                {active ? <Check size={12} /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-semibold text-foreground">{v.name}</span>
                <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                  {v.has_captions !== false ? `${v.cue_count ?? "…"} cues · ` : "No captions · "}
                  {v.path || v.mime_type}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function PhaseRail({ phase }: { phase: Phase }) {
  const steps = [
    { n: 1, label: "Video" },
    { n: 2, label: "Themes" },
    { n: 3, label: "Hooks" },
    { n: 4, label: "Intent" },
    { n: 5, label: "Carousel" },
  ] as const;
  return (
    <ol className="mt-4 flex flex-wrap gap-2">
      {steps.map((s) => (
        <li
          key={s.n}
          className={cn(
            "rounded-md border px-2.5 py-1 text-xs font-semibold",
            phase >= s.n
              ? "border-foreground/30 bg-muted text-foreground"
              : "border-border text-muted-foreground"
          )}
        >
          {s.n}. {s.label}
        </li>
      ))}
    </ol>
  );
}

function VerbatimList({
  label,
  items,
  selected,
  onToggle,
  onPreview,
  quote = true,
}: {
  label: string;
  items: CarouselVerbatimItem[];
  selected: string[];
  onToggle: (text: string) => void;
  onPreview: (item: CarouselVerbatimItem) => void;
  quote?: boolean;
}) {
  return (
    <div>
      <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">{label}</p>
      <ul className="mt-2 space-y-1.5">
        {items.length === 0 && (
          <li className="text-xs text-muted-foreground">None in this theme window.</li>
        )}
        {items.map((item) => {
          const on = selected.includes(item.text);
          const display = quote ? `\u201C${item.text}\u201D` : item.text;
          return (
            <li key={item.id} className="flex gap-1">
              <button
                type="button"
                className={cn(
                  "min-w-0 flex-1 rounded-md border px-2.5 py-2 text-left text-xs transition",
                  on ? "border-foreground bg-muted font-semibold" : "border-border"
                )}
                onClick={() => onToggle(item.text)}
              >
                <span className="block tabular-nums text-[10px] text-muted-foreground">
                  {fmtTs(item.start_sec)}
                </span>
                <span className="mt-0.5 block text-foreground">{display}</span>
                {item.translated ? (
                  <span className="mt-1 block text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                    Translated
                  </span>
                ) : null}
              </button>
              <button
                type="button"
                className="studio-btn studio-btn-ghost shrink-0 px-2"
                aria-label="Preview"
                onClick={() => onPreview(item)}
              >
                <Play size={14} />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function ThemePreviewModal({
  videoId,
  videoName,
  startSec,
  text,
  onClose,
}: {
  videoId: string;
  videoName: string;
  startSec: number;
  text: string;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamUrl = `${driveVideoStreamUrl(videoId)}#t=${Math.floor(startSec)}`;

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    seekVideoTo(video, startSec);
  }, [videoId, startSec]);

  return (
    <div className="relative flex max-h-[min(88dvh,720px)] flex-col overflow-hidden rounded-lg bg-card shadow-lg">
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute right-3 top-3 z-10 rounded-[8px] bg-foreground/70 p-2 text-white hover:bg-foreground"
      >
        <X size={16} />
      </button>
      <div className="shrink-0 bg-foreground">
        <video
          ref={videoRef}
          src={streamUrl}
          controls
          playsInline
          preload="metadata"
          className="max-h-[min(48dvh,420px)] w-full object-contain"
        />
      </div>
      <div className="border-t border-border px-4 py-4">
        <p className="text-sm font-semibold text-foreground">{videoName}</p>
        <p className="mt-1 text-xs tabular-nums text-muted-foreground">{fmtTs(startSec)}</p>
        <p className="mt-2 text-xs text-muted-foreground">&ldquo;{text}&rdquo;</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <DownloadButton
            url={driveFileDownloadUrl(videoId)}
            filename={videoName}
            label="Video"
            variant="ghost"
          />
        </div>
      </div>
    </div>
  );
}
