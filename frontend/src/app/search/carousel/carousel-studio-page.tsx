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
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  History,
  ImageIcon,
  Play,
  RefreshCw,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import {
  cacheOnlyAssetUrl,
  apiClient,
  driveFileDownloadUrl,
  driveVideoStreamUrl,
  formatApiError,
  type CarouselGeneratedItem,
  type CarouselGenerationSaveListItem,
  type CarouselLayouts,
  type CarouselOutlineResponse,
  type CarouselOutlineSlide,
  type CarouselPipelineExtractResponse,
  type CarouselPipelineTheme,
  type CarouselRecentVideo,
  type CarouselTimedPick,
  type CarouselTopicTreeNode,
  type CarouselVerbatimItem,
  type Person,
} from "@/lib/api";
import { DownloadButton, LoadingLabel } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { toastApiError } from "@/lib/toast-api-error";
import { cn } from "@/lib/utils";
import {
  applyHookFrameOverrides,
  focalPointStyle,
  formatTimestampRange,
  slideFrameUrl,
  splitPanelCaptions,
  withReplacedFrame,
  type PickedFrame,
} from "./utils";
import { TopicsHooksTree, TranscriptFramePicker } from "./topics-hooks-tree";

type Phase = 1 | 2 | 3 | 4 | 5;

/** Prefer the dual-layout cache bundle when present so single/split toggles are instant. */
function carouselsForLayout(
  layouts: CarouselLayouts | null | undefined,
  mode: "single_1" | "split_2",
  fallback: CarouselGeneratedItem[]
): CarouselGeneratedItem[] {
  const bundle = layouts?.[mode]?.carousels;
  if (bundle && bundle.length) return bundle;
  return fallback;
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

function resolvePick(
  text: string,
  items: CarouselVerbatimItem[]
): CarouselVerbatimItem | undefined {
  const exact = items.find((x) => x.text === text);
  if (exact) return exact;
  const lower = text.toLowerCase().trim();
  return items.find((x) => x.text.toLowerCase().trim() === lower);
}

function flattenTopicTree(nodes: CarouselTopicTreeNode[] | undefined): CarouselTopicTreeNode[] {
  if (!nodes?.length) return [];
  const out: CarouselTopicTreeNode[] = [];
  for (const n of nodes) {
    out.push(n);
    if (n.subtopics?.length) out.push(...flattenTopicTree(n.subtopics));
  }
  return out;
}

function resolveTopicNode(
  text: string,
  extract: CarouselPipelineExtractResponse
): CarouselTopicTreeNode | CarouselVerbatimItem | undefined {
  const treeNodes = flattenTopicTree(extract.topic_tree);
  const exactTree = treeNodes.find((n) => n.text === text);
  if (exactTree) return exactTree;
  const lower = text.toLowerCase().trim();
  const treeHit = treeNodes.find((n) => n.text.toLowerCase().trim() === lower);
  if (treeHit) return treeHit;
  return resolvePick(text, extract.topics ?? []);
}

function toTopicTimedPick(
  text: string,
  extract: CarouselPipelineExtractResponse
): CarouselTimedPick {
  const node = resolveTopicNode(text, extract);
  const ranges =
    node && "time_ranges" in node && Array.isArray(node.time_ranges)
      ? node.time_ranges.map((r) => ({
          start_sec: Number(r.start_sec) || 0,
          end_sec: r.end_sec ?? null,
        }))
      : undefined;
  return {
    id: node?.id,
    text,
    start_sec: node?.start_sec ?? 0,
    end_sec: node?.end_sec ?? null,
    theme_id: node && "theme_id" in node ? node.theme_id ?? null : null,
    time_ranges: ranges,
  };
}

function toHookTimedPick(
  text: string,
  extract: CarouselPipelineExtractResponse
): CarouselTimedPick {
  const item = resolvePick(text, extract.hooks ?? []);
  return {
    id: item?.id,
    text,
    start_sec: item?.start_sec ?? 0,
    end_sec: item?.end_sec ?? null,
    theme_id: item?.theme_id ?? null,
    topic_id: item?.topic_id ?? item?.parent_topic_id ?? null,
    topic_text: item?.topic_text ?? null,
    original_text: item?.original_text ?? null,
  };
}

/** Ensure one seed topic per selected topic AND per parent of selected hooks. */
function expandTopicSeeds(
  selectedTopics: string[],
  selectedHooks: string[],
  extract: CarouselPipelineExtractResponse
): CarouselTimedPick[] {
  const seeds: CarouselTimedPick[] = [];
  const seen = new Set<string>();
  const add = (pick: CarouselTimedPick) => {
    const key = pick.text.toLowerCase().trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    seeds.push(pick);
  };
  for (const text of selectedTopics) add(toTopicTimedPick(text, extract));
  for (const hookText of selectedHooks) {
    const hook = resolvePick(hookText, extract.hooks ?? []);
    const parent = (hook?.topic_text || "").trim();
    if (!parent) continue;
    add(toTopicTimedPick(parent, extract));
  }
  return seeds;
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
  const [personNotFound, setPersonNotFound] = useState<string | null>(null);

  const [videoScope, setVideoScope] = useState<"recent" | "all">("recent");
  const [videoQuery, setVideoQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [allVideos, setAllVideos] = useState<CarouselRecentVideo[]>([]);
  const [loadingAll, setLoadingAll] = useState(false);

  const [selectedVideo, setSelectedVideo] = useState<CarouselRecentVideo | null>(null);
  const [searchEntity, setSearchEntity] = useState("");
  const [personPick, setPersonPick] = useState("");
  const [objectQuery, setObjectQuery] = useState("");

  const [themes, setThemes] = useState<CarouselPipelineTheme[]>([]);
  const [loadingThemes, setLoadingThemes] = useState(false);
  const [selectedThemes, setSelectedThemes] = useState<CarouselPipelineTheme[]>([]);
  const [themeSaves, setThemeSaves] = useState<CarouselGenerationSaveListItem[]>([]);
  const [themeSaveId, setThemeSaveId] = useState<number | null>(null);
  const [themesFromCache, setThemesFromCache] = useState(false);
  const [themeHistoryOpen, setThemeHistoryOpen] = useState(false);
  const [loadingThemeSaves, setLoadingThemeSaves] = useState(false);
  /** Selection key that currently has loaded themes (null = need Continue). */
  const [themesLoadedKey, setThemesLoadedKey] = useState<string | null>(null);
  const themesAbortRef = useRef<AbortController | null>(null);
  const themesRequestKeyRef = useRef<string>("");

  const themesSelectionKey = useMemo(() => {
    if (!selectedVideo) return "";
    const personName = personPick.trim();
    const fromObject = objectQuery.trim();
    const entity =
      personName && fromObject
        ? `${personName} / ${fromObject}`
        : personName || fromObject || "";
    return `${selectedVideo.id}|${personName}|${entity}`;
  }, [selectedVideo, personPick, objectQuery]);

  /** True until themes are loaded for the current video/person selection. */
  const themesNeedContinue =
    Boolean(selectedVideo) &&
    (themesLoadedKey !== themesSelectionKey || (themes.length === 0 && !loadingThemes));

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
  const [generatedCarousels, setGeneratedCarousels] = useState<CarouselGeneratedItem[]>([]);
  const [activeCarouselId, setActiveCarouselId] = useState<string | null>(null);
  const [imagesReady, setImagesReady] = useState(false);
  const [selectingImages, setSelectingImages] = useState(false);
  const [imageQualityNote, setImageQualityNote] = useState<string | null>(null);
  const [carouselLayout, setCarouselLayout] = useState<"single_1" | "split_2">("single_1");
  const [carouselLayouts, setCarouselLayouts] = useState<CarouselLayouts | null>(null);
  const [pipelineLocked, setPipelineLocked] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState("idle");
  const [carouselSaves, setCarouselSaves] = useState<CarouselGenerationSaveListItem[]>([]);
  // Frames picked against a hook before slides exist, keyed by hook text.
  const [hookFrames, setHookFrames] = useState<Record<string, PickedFrame>>({});
  const outlineRef = useRef<HTMLDivElement>(null);

  // User-driven only: do not auto-open a cached complete carousel (that jumped
  // straight to phase 5). History loads for the Saved list; restore is a click.
  useEffect(() => {
    if (!selectedVideo) {
      setCarouselSaves([]);
      return;
    }
    void apiClient.carouselPipelineSaves(selectedVideo.id, 12, "carousel")
      .then((res) => setCarouselSaves(res.items ?? []))
      .catch(() => setCarouselSaves([]));
  }, [selectedVideo]);

  // Cheap lock polling; this path never invokes Gemini, ffmpeg, or Drive.
  useEffect(() => {
    if (!selectedVideo) {
      setPipelineLocked(false);
      setPipelineStatus("idle");
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const status = await apiClient.carouselPipelineStatus(selectedVideo.id);
        if (cancelled) return;
        setPipelineLocked(Boolean(status.locked));
        setPipelineStatus(status.status || "idle");
        if (status.locked) timer = window.setTimeout(() => void poll(), 1000);
      } catch {
        if (!cancelled) setPipelineLocked(false);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [selectedVideo]);

  const entityLabel = useMemo(() => {
    const fromPerson = personPick.trim();
    const fromObject = objectQuery.trim();
    if (fromPerson && fromObject) return `${fromPerson} / ${fromObject}`;
    return fromPerson || fromObject || searchEntity.trim();
  }, [personPick, objectQuery, searchEntity]);

  const activeGeneratedCarousel = useMemo(() => {
    if (!generatedCarousels.length) return null;
    return (
      generatedCarousels.find((c) => c.id === activeCarouselId) ?? generatedCarousels[0] ?? null
    );
  }, [generatedCarousels, activeCarouselId]);

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
        // Don't await persons — a hung /persons used to stall the video list.
        const vids = await apiClient.carouselRecentVideos(5, true);
        if (cancelled) return;
        setRecent(vids.items ?? []);
      } catch {
        /* api() already toasted */
      } finally {
        if (!cancelled) setLoadingRecent(false);
      }
      void apiClient
        .persons()
        .then((people) => {
          if (!cancelled) setPersons(people);
        })
        .catch(() => {
          if (!cancelled) setPersons([]);
        });
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
      try {
        const res = await apiClient.carouselVideos({
          q: debouncedQuery || undefined,
          limit: 30,
          captionedOnly: true,
        });
        if (!cancelled) setAllVideos(res.items ?? []);
      } catch {
        if (!cancelled) setAllVideos([]);
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
    setGeneratedCarousels([]);
    setCarouselLayouts(null);
    setActiveCarouselId(null);
    setPersonNotFound(null);
  }, []);

  const refreshThemeSaves = useCallback(async (driveFileId: string) => {
    setLoadingThemeSaves(true);
    try {
      const res = await apiClient.carouselPipelineSaves(driveFileId, 12, "themes");
      setThemeSaves(res.items ?? []);
    } catch {
      setThemeSaves([]);
    } finally {
      setLoadingThemeSaves(false);
    }
  }, []);

  const loadThemesForVideo = useCallback(
    async (opts: {
      video: CarouselRecentVideo;
      personName: string;
      entity: string;
      requestKey: string;
      /** Stable selection key (without regen suffix) for Continue/cache state. */
      selectionKey: string;
      force?: boolean;
      signal?: AbortSignal;
    }) => {
      const { video, personName, entity, requestKey, selectionKey, force, signal } = opts;
      setLoadingThemes(true);
      setPersonNotFound(null);
      try {
        const res = await apiClient.carouselPipelineThemes(video.id, {
          personName: personName || undefined,
          searchEntity: entity || undefined,
          force: Boolean(force),
          signal,
        });
        if (signal?.aborted || themesRequestKeyRef.current !== requestKey) return;

        if (res.error === "person_not_found" || res.person_found === false) {
          const msg =
            res.message ||
            res.warning ||
            "Person not found in this video. Try without that person or change video.";
          toastApiError(msg);
          setPersonNotFound(msg);
          setThemes([]);
          setThemeSaveId(null);
          setThemesFromCache(false);
          setThemesLoadedKey(null);
          setPhase(1);
          return;
        }
        setThemes(res.themes ?? []);
        setThemeSaveId(res.save_id ?? null);
        setThemesFromCache(Boolean(res.cache_hit));
        setThemesLoadedKey(selectionKey);
        if (!(res.themes ?? []).length && res.warning && !String(res.warning).toLowerCase().includes("cached")) {
          toastApiError(
            formatApiError(
              res.warning,
              "We couldn’t find themes for this video yet. Wait until the transcript is ready, then try again."
            )
          );
        }
        setPhase(2);
        void refreshThemeSaves(video.id);
      } catch (e) {
        if (signal?.aborted || themesRequestKeyRef.current !== requestKey) return;
        if (e instanceof Error && e.name === "AbortError") return;
        setThemes([]);
        setThemeSaveId(null);
        setThemesFromCache(false);
        setThemesLoadedKey(null);
      } finally {
        if (!signal?.aborted && themesRequestKeyRef.current === requestKey) {
          setLoadingThemes(false);
        }
      }
    },
    [refreshThemeSaves]
  );

  /**
   * Selection change: reset downstream and stay on phase 1.
   * Never auto-load themes or advance — user must click Continue / Load saved themes.
   */
  useEffect(() => {
    if (!selectedVideo) {
      setLoadingThemes(false);
      setThemeSaves([]);
      setThemeSaveId(null);
      setThemesFromCache(false);
      setThemeHistoryOpen(false);
      setThemesLoadedKey(null);
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

    themesAbortRef.current?.abort();
    const ac = new AbortController();
    themesAbortRef.current = ac;
    setThemes([]);
    setThemeSaveId(null);
    setThemesFromCache(false);
    setThemesLoadedKey(null);
    setThemeHistoryOpen(false);
    resetFromPhase2();
    setLoadingThemes(false);
    setPersonNotFound(null);
    setPhase(1);
    setSearchEntity(entity);

    let cancelled = false;
    void (async () => {
      setLoadingThemeSaves(true);
      try {
        const res = await apiClient.carouselPipelineSaves(video.id, 12, "themes");
        if (cancelled || ac.signal.aborted) return;
        setThemeSaves(res.items ?? []);
      } catch {
        if (cancelled || ac.signal.aborted) return;
        setThemeSaves([]);
      } finally {
        if (!cancelled && !ac.signal.aborted) setLoadingThemeSaves(false);
      }
      // Do not call loadThemesForVideo here — wait for Continue / Load saved themes.
    })();

    return () => {
      cancelled = true;
      themesAbortRef.current?.abort();
      themesAbortRef.current = null;
    };
  }, [selectedVideo, personPick, objectQuery, resetFromPhase2]);

  async function continueToThemes() {
    if (!selectedVideo || loadingThemes) return;
    const video = selectedVideo;
    const personName = personPick.trim();
    const fromObject = objectQuery.trim();
    const entity =
      personName && fromObject
        ? `${personName} / ${fromObject}`
        : personName || fromObject || "";
    const requestKey = `${video.id}|${personName}|${entity}`;
    themesAbortRef.current?.abort();
    const ac = new AbortController();
    themesAbortRef.current = ac;
    themesRequestKeyRef.current = requestKey;
    setThemeHistoryOpen(false);
    await loadThemesForVideo({
      video,
      personName,
      entity,
      requestKey,
      selectionKey: requestKey,
      force: false,
      signal: ac.signal,
    });
  }

  async function regenerateThemes() {
    if (!selectedVideo || loadingThemes) return;
    const video = selectedVideo;
    const personName = personPick.trim();
    const fromObject = objectQuery.trim();
    const entity =
      personName && fromObject
        ? `${personName} / ${fromObject}`
        : personName || fromObject || "";
    const selectionKey = `${video.id}|${personName}|${entity}`;
    const requestKey = `${selectionKey}|regen|${Date.now()}`;
    themesAbortRef.current?.abort();
    const ac = new AbortController();
    themesAbortRef.current = ac;
    themesRequestKeyRef.current = requestKey;
    setThemeHistoryOpen(false);
    setSelectedThemes([]);
    setExtract(null);
    setSelectedHooks([]);
    setSelectedTopics([]);
    await loadThemesForVideo({
      video,
      personName,
      entity,
      requestKey,
      selectionKey,
      force: true,
      signal: ac.signal,
    });
  }

  async function restoreThemeSave(saveId: number) {
    try {
      const res = await apiClient.carouselPipelineSaveGet(saveId);
      const themesPayload = (res.payload?.themes ?? []) as CarouselPipelineTheme[];
      if (!themesPayload.length) {
        toastApiError("That save has no themes.");
        return;
      }
      setThemes(themesPayload);
      setThemeSaveId(res.id);
      setThemesFromCache(true);
      setThemesLoadedKey(themesSelectionKey || null);
      setSelectedThemes([]);
      setExtract(null);
      setSelectedHooks([]);
      setSelectedTopics([]);
      setPhase(2);
      setThemeHistoryOpen(false);
    } catch {
      /* api() already toasted */
    }
  }

  function selectVideo(video: CarouselRecentVideo) {
    // Switching video resets downstream; themes wait for Continue / Load.
    setSelectedVideo(video);
  }

  const continueDisabledReason = !selectedVideo
    ? "Select a captioned video first"
    : loadingThemes
      ? "Loading themes…"
      : null;

  function onToggleTheme(theme: CarouselPipelineTheme) {
    setSelectedThemes((prev) => toggleTheme(prev, theme));
    setExtract(null);
    setSelectedHooks([]);
    setSelectedTopics([]);
    setPhaseIntent(null);
    setPhaseIntentScore(null);
    setOutline(null);
    setGeneratedCarousels([]);
    setCarouselLayouts(null);
    setActiveCarouselId(null);
    if (phase > 2) setPhase(2);
  }

  async function extractFromSelectedThemes() {
    if (!selectedVideo || loadingExtract) return;
    if (!selectedThemes.length) {
      toastApiError("Select at least one theme.");
      return;
    }
    setLoadingExtract(true);
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
      setSelectedHooks([]);
      setSelectedTopics([]);
      setPhaseIntent(res.intent ?? null);
      setPhaseIntentScore(res.intent_score ?? null);
      setPhase(3);
    } catch {
      setExtract(null);
    } finally {
      setLoadingExtract(false);
    }
  }

  async function goToPreviewIntent() {
    if (!selectedHooks.length && !selectedTopics.length) {
      toastApiError("Select at least one hook or topic.");
      return;
    }
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
    if (!selectedVideo || !selectedThemes.length || !extract || building) return;
    setBuilding(true);
    setImageQualityNote(null);
    try {
      const hookPicks = selectedHooks.map((text) => toHookTimedPick(text, extract));
      // Explicit topics + parents implied by selected hooks → one carousel each.
      const topicPicks = expandTopicSeeds(selectedTopics, selectedHooks, extract);
      if (!topicPicks.length && !hookPicks.length) {
        toastApiError("Select at least one topic or hook.");
        return;
      }

      // Transcript-first: never call Gemini frame selection here.
      const res = await apiClient.carouselPipelineGenerate({
        drive_file_id: selectedVideo.id,
        video_name: selectedVideo.name,
        intent: phaseIntent || extract.intent || undefined,
        themes: selectedThemes.map((t) => ({
          theme_id: t.theme_id,
          title: t.title,
          start_sec: t.start_sec,
          end_sec: t.end_sec,
          summary: t.summary,
        })),
        hooks: hookPicks,
        topics: topicPicks.length
          ? topicPicks
          : hookPicks.map((h) => ({
              id: h.id,
              text: h.text,
              start_sec: h.start_sec,
              end_sec: h.end_sec,
              theme_id: h.theme_id,
            })),
        min_slides: 6,
        max_slides: 10,
        select_images: false,
      });

      const list =
        res.carousels && res.carousels.length
          ? res.carousels
          : res.slides?.length
            ? [
                {
                  id: "carousel_1",
                  kind: "hook" as const,
                  title: res.title,
                  topic_labels: res.topics ?? selectedTopics,
                  slide_count: res.slide_count,
                  slides: res.slides,
                  hooks: res.hooks,
                  topics: res.topics,
                  images_ready: false,
                },
              ]
            : [];
      if (!list.length) {
        toastApiError("Generate returned no carousels. Try fewer hooks or another theme.");
        return;
      }
      const withPicks = applyHookFrameOverrides(list, hookFrames);
      setGeneratedCarousels(withPicks);
      setActiveCarouselId(withPicks[0]?.id ?? null);
      setImagesReady(Boolean(res.images_ready));
      setCarouselLayouts(res.layouts ?? null);
      setOutline(res);
      setPhase(5);
      requestAnimationFrame(() => {
        outlineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } catch {
      // Stay on phase 4; api() already toasted the failure.
    } finally {
      setBuilding(false);
    }
  }

  async function selectCarouselImages() {
    if (!selectedVideo || selectingImages || !generatedCarousels.length) return;
    setSelectingImages(true);
    setImageQualityNote(null);
    try {
      const res = await apiClient.carouselPipelineSelectImages({
        drive_file_id: selectedVideo.id,
        carousels: generatedCarousels,
      });
      const list = res.carousels ?? [];
      setCarouselLayouts(res.layouts ?? null);
      // Image selection returns pipeline frames, so re-apply the user's picks.
      setGeneratedCarousels(
        applyHookFrameOverrides(
          carouselsForLayout(res.layouts, carouselLayout, list),
          hookFrames
        )
      );
      if (list.length && !list.some((c) => c.id === activeCarouselId)) {
        setActiveCarouselId(list[0]?.id ?? null);
      }
      setImagesReady(true);
      setOutline((prev) =>
        prev
          ? {
              ...prev,
              carousels: list,
              slides: res.slides ?? prev.slides,
              images_ready: true,
              quality: res.quality,
              layouts: res.layouts ?? prev.layouts,
            }
          : prev
      );
      const q = res.quality;
      if (q) {
        const rej = Object.entries(q.rejected || {})
          .map(([k, v]) => `${v} ${k}`)
          .join(", ");
        setImageQualityNote(
          `Frames: ${q.candidates ?? 0} candidates → ${q.kept ?? 0} kept` +
            (rej ? ` (filtered ${rej})` : "")
        );
      }
    } catch {
      /* api() already toasted */
    } finally {
      setSelectingImages(false);
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

      {pipelineLocked && (
        <p className="text-xs font-medium text-muted-foreground" role="status">
          Carousel generation is in progress. Editing and regeneration are temporarily locked.
        </p>
      )}

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
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
              Selected:{" "}
              <span className="font-medium text-foreground">{selectedVideo.name}</span>
              {themesNeedContinue
                ? themeSaves.length > 0 || loadingThemeSaves
                  ? " · loading saved themes…"
                  : " · continue in Themes below to generate (first time)"
                : themes.length > 0
                  ? ` · ${themes.length} themes loaded — select themes, then Extract`
                  : ""}
            </p>
          </div>
        )}
      </section>

      {selectedVideo && !personNotFound && (
        <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-2">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="studio-section-label">2 · Themes</p>
              <h2 className="mt-1 text-base font-semibold text-foreground">Narrative themes</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Non-overlapping segments from this video
                {personPick.trim() ? ` · “${personPick.trim()}” appears here` : ""}. Select one or more
                themes, then extract hooks & topics from the combined set.
                {themesFromCache
                  ? " Loaded from a saved set for this transcript."
                  : themeSaveId
                    ? " Saved for next time."
                    : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <button
                  type="button"
                  className="studio-btn studio-btn-ghost"
                  onClick={() => setThemeHistoryOpen((v) => !v)}
                  aria-expanded={themeHistoryOpen}
                  disabled={loadingThemes}
                >
                  <History size={14} />
                  Saved themes
                  <ChevronDown size={14} className={cn(themeHistoryOpen && "rotate-180")} />
                </button>
                {themeHistoryOpen && (
                  <div
                    className="absolute right-0 z-20 mt-1 w-72 rounded-lg border border-border bg-card p-1 shadow-lg"
                    role="listbox"
                  >
                    {loadingThemeSaves && (
                      <p className="px-2 py-2 text-xs text-muted-foreground">Loading saves…</p>
                    )}
                    {!loadingThemeSaves && themeSaves.length === 0 && (
                      <p className="px-2 py-2 text-xs text-muted-foreground">
                        No saved themes yet — first generation is stored automatically.
                      </p>
                    )}
                    {themeSaves.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        className={cn(
                          "flex w-full flex-col gap-0.5 rounded-md px-2 py-2 text-left hover:bg-muted",
                          themeSaveId === s.id && "bg-muted"
                        )}
                        onClick={() => void restoreThemeSave(s.id)}
                      >
                        <span className="line-clamp-1 text-sm font-medium text-foreground">
                          {s.label || `Themes #${s.id}`}
                        </span>
                        <span className="text-[10px] text-muted-foreground">
                          {s.created_at ? new Date(s.created_at).toLocaleString() : ""} ·{" "}
                          {s.theme_count ?? 0} themes
                          {themeSaveId === s.id ? " · current" : ""}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                onClick={() => void regenerateThemes()}
                disabled={
                  loadingThemes ||
                  !selectedVideo ||
                  themesNeedContinue ||
                  loadingExtract ||
                  building ||
                  phase >= 5 ||
                  pipelineLocked
                }
                title={
                  themesNeedContinue
                    ? "Load themes first"
                    : "Generate a fresh theme set and save it"
                }
              >
                <RefreshCw size={14} className={cn(loadingThemes && "animate-spin")} />
                Regenerate
              </button>
            </div>
          </div>

          {loadingThemes ? (
            <p className="mt-4 text-sm text-muted-foreground">
              <LoadingLabel>
                {personPick.trim()
                  ? `Checking “${personPick.trim()}” in video, then loading themes…`
                  : themesFromCache || themeSaves.length > 0
                    ? "Loading themes…"
                    : "Generating themes…"}
              </LoadingLabel>
            </p>
          ) : themesNeedContinue && phase < 3 ? (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <p className="text-sm text-muted-foreground">
                {loadingThemes || loadingThemeSaves
                  ? "Loading saved themes…"
                  : themeSaves.length > 0
                    ? "Saved themes are available — load them to select and Extract."
                    : "Continue generates themes once for this video (then they’re cached)."}
              </p>
              {/* First-time: Continue. Cached: Load (never a second Continue after themes exist). */}
              {!loadingThemeSaves && (
                <button
                  type="button"
                  className={
                    themeSaves.length > 0
                      ? "studio-btn studio-btn-primary"
                      : "studio-btn studio-btn-accent studio-btn-continue"
                  }
                  onClick={() => void continueToThemes()}
                  disabled={
                    Boolean(continueDisabledReason) ||
                    phase >= 3 ||
                    loadingExtract ||
                    building ||
                    pipelineLocked ||
                    loadingThemes
                  }
                  data-testid="carousel-continue-themes"
                  title={
                    continueDisabledReason ||
                    (themeSaves.length > 0
                      ? "Load saved themes"
                      : "Generate themes for this video")
                  }
                >
                  {loadingThemes ? (
                    <LoadingLabel>Working…</LoadingLabel>
                  ) : themeSaves.length > 0 ? (
                    <>
                      <Sparkles size={15} />
                      Load saved themes
                    </>
                  ) : (
                    <>
                      Continue
                      <ArrowRight size={14} className="studio-btn-continue-arrow" />
                    </>
                  )}
                </button>
              )}
            </div>
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

          {!themesNeedContinue && themes.length > 0 && (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="studio-btn studio-btn-primary"
                onClick={() => void extractFromSelectedThemes()}
                disabled={
                  loadingExtract ||
                  loadingThemes ||
                  building ||
                  pipelineLocked ||
                  selectedThemes.length === 0 ||
                  themesNeedContinue
                }
                title={
                  selectedThemes.length === 0
                    ? "Select at least one theme"
                    : loadingExtract
                      ? "Extracting…"
                      : "Extract hooks & topics from selected themes"
                }
                data-testid="carousel-extract-themes"
              >
                {loadingExtract ? (
                  <LoadingLabel>Extracting hooks & generating topics…</LoadingLabel>
                ) : selectedThemes.length > 1 ? (
                  `Extract from ${selectedThemes.length} themes`
                ) : selectedThemes.length === 1 ? (
                  "Extract hooks & topics"
                ) : (
                  "Select themes, then Extract"
                )}
              </button>
              {selectedThemes.length > 0 ? (
                <p className="text-xs text-muted-foreground">
                  {selectedThemes.length} theme{selectedThemes.length === 1 ? "" : "s"} selected
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Select one or more themes above to enable Extract.
                </p>
              )}
              {themeSaveId ? (
                <p className="text-xs text-muted-foreground">
                  {themesFromCache ? "Restored" : "Autosaved"} themes #{themeSaveId}
                </p>
              ) : null}
            </div>
          )}
        </section>
      )}

      {extract && selectedThemes.length > 0 && phase >= 3 && (
        <section className="studio-panel p-4 sm:p-6" data-testid="carousel-phase-3">
          <p className="studio-section-label">3 · Hooks & topics</p>
          <h2 className="mt-1 text-base font-semibold text-foreground">
            Topics → subtopics → hooks
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Cohesive topics from the transcript (where the speaker takes a direction), optional
            subtopics, then hooks crafted one topic at a time. Autosaved for later · shuffle
            reshuffles your picks. Carousel images stay deferred until after you generate slides
            and press Select &amp; filter images.
            {extract.any_translated ? " Some lines were translated for display." : ""}
          </p>
          {selectedVideo && (
            <TopicsHooksTree
              driveFileId={selectedVideo.id}
              extract={extract}
              selectedHooks={selectedHooks}
              selectedTopics={selectedTopics}
              onToggleHook={(text) => setSelectedHooks((prev) => toggleText(prev, text))}
              onToggleTopic={(text) => setSelectedTopics((prev) => toggleText(prev, text))}
              onPreview={(item) => setPreviewCue(item)}
              onRestoreExtract={(next, hooks, topics) => {
                setExtract(next);
                setSelectedHooks(hooks);
                setSelectedTopics(topics);
                if (next.intent) setPhaseIntent(next.intent);
                if (next.intent_score != null) setPhaseIntentScore(next.intent_score);
              }}
              onFramePicked={(hookText, frameTs, previewUrl) =>
                setHookFrames((prev) => ({
                  ...prev,
                  [hookText]: { frame_ts: frameTs, preview_url: previewUrl },
                }))
              }
            />
          )}
          {phase < 4 && (
            <div className="mt-4">
              <button
                type="button"
                className="studio-btn studio-btn-primary"
                onClick={() => void goToPreviewIntent()}
                disabled={
                  loadingIntent ||
                  loadingExtract ||
                  building ||
                  pipelineLocked ||
                  (!selectedHooks.length && !selectedTopics.length)
                }
                title={
                  !selectedHooks.length && !selectedTopics.length
                    ? "Select at least one hook or topic"
                    : loadingIntent
                      ? "Updating intent…"
                      : undefined
                }
                data-testid="carousel-continue-preview"
              >
                {loadingIntent ? (
                  <LoadingLabel>Updating intent…</LoadingLabel>
                ) : (
                  "Continue to preview & intent"
                )}
              </button>
            </div>
          )}
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

          <ul
            className="studio-scroll-fade mt-4 max-h-56 space-y-1 overflow-y-auto rounded-lg border border-border"
            data-testid="carousel-preview-markers"
          >
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

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="studio-btn studio-btn-accent"
              onClick={() => void generateCarousel()}
              disabled={
                building ||
                selectingImages ||
                loadingExtract ||
                pipelineLocked ||
                (!selectedHooks.length && !selectedTopics.length)
              }
              title={
                building
                  ? "Building carousels…"
                  : !selectedHooks.length && !selectedTopics.length
                    ? "Select at least one hook or topic"
                    : undefined
              }
              data-testid="carousel-generate"
            >
              {building ? (
                <LoadingLabel>Building transcript carousels…</LoadingLabel>
              ) : (
                "Generate carousels"
              )}
            </button>
          </div>
        </section>
      )}

      <div ref={outlineRef}>
        {(phase >= 5 || outline || generatedCarousels.length > 0) && (
          <section className="studio-panel space-y-4 p-4 sm:p-6" data-testid="carousel-phase-5">
            <div>
              <p className="studio-section-label">5 · Carousels</p>
              <h2 className="mt-1 text-base font-semibold text-foreground">
                {generatedCarousels.length > 0
                  ? `${generatedCarousels.length} carousel${generatedCarousels.length === 1 ? "" : "s"}`
                  : activeGeneratedCarousel?.title || outline?.title || "Carousel cards"}
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                One carousel per selected hook — each with 6+ short exact-transcript lines (not
                paragraphs). Edit lines if needed, then run{" "}
                <strong>Select &amp; filter images</strong> for frames.
              </p>
            </div>

            {carouselSaves.length > 0 && (
              <details className="rounded-lg border border-border px-3 py-2">
                <summary className="cursor-pointer text-xs font-semibold text-foreground">
                  Generation history ({carouselSaves.length})
                </summary>
                <div className="mt-2 space-y-1">
                  {carouselSaves.map((save) => (
                    <div key={save.id} className="flex items-center justify-between gap-2 text-xs">
                      <span className="text-muted-foreground">
                        v{save.copy_version ?? 1} · {save.source || "generation"} ·{" "}
                        {save.created_at ? new Date(save.created_at).toLocaleString() : ""}
                      </span>
                      <button
                        type="button"
                        className="studio-btn studio-btn-ghost px-2 py-1 text-[11px]"
                        onClick={() => {
                          void apiClient.carouselPipelineSaveGet(save.id).then((restored) => {
                            const payload = restored.payload;
                            const list = payload.carousels ?? (payload.slides?.length
                              ? [{
                                  id: "restored",
                                  kind: "mixed",
                                  title: restored.label || "Carousel",
                                  topic_labels: [],
                                  slide_count: payload.slides.length,
                                  slides: payload.slides,
                                  images_ready: true,
                                }]
                              : []);
                            setGeneratedCarousels(list as CarouselGeneratedItem[]);
                            setActiveCarouselId(list[0]?.id ?? null);
                            setImagesReady(true);
                            setPhase(5);
                          }).catch(() => {
                            /* api() already toasted */
                          });
                        }}
                      >
                        Restore
                      </button>
                    </div>
                  ))}
                </div>
              </details>
            )}

            {generatedCarousels.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="studio-btn studio-btn-accent studio-btn-continue"
                  onClick={() => void selectCarouselImages()}
                  disabled={
                    selectingImages || building || pipelineLocked || !generatedCarousels.length
                  }
                  title={
                    selectingImages
                      ? "Selecting images…"
                      : "Rank and attach frames using your edited transcripts"
                  }
                  data-testid="carousel-select-images"
                >
                  {selectingImages ? (
                    <LoadingLabel>Selecting & filtering images…</LoadingLabel>
                  ) : imagesReady ? (
                    <>
                      <RefreshCw size={15} />
                      Re-run image selection
                    </>
                  ) : (
                    <>
                      <ImageIcon size={15} />
                      Select &amp; filter images
                      <ArrowRight size={14} className="studio-btn-continue-arrow" />
                    </>
                  )}
                </button>
                {imageQualityNote && (
                  <p className="text-xs text-muted-foreground">{imageQualityNote}</p>
                )}
                {!imagesReady && !selectingImages && (
                  <p className="text-xs text-muted-foreground">
                    Edit slide text below, then run image selection once.
                  </p>
                )}
              </div>
            )}

            {generatedCarousels.length > 0 && (
              <div
                className="flex flex-wrap gap-2"
                role="tablist"
                aria-label="Generated carousels"
              >
                {generatedCarousels.map((c) => {
                  const on = c.id === activeCarouselId;
                  const kindLabel =
                    c.kind === "hook" ? "Hook" : c.kind === "mixed" ? "Mixed" : "Topic";
                  return (
                    <button
                      key={c.id}
                      type="button"
                      role="tab"
                      aria-selected={on}
                      className={cn(
                        "rounded-lg border px-3 py-2 text-left text-xs transition",
                        on
                          ? "border-foreground bg-muted font-semibold text-foreground"
                          : "border-border text-muted-foreground hover:border-muted-foreground/40 hover:text-foreground"
                      )}
                      onClick={() => setActiveCarouselId(c.id)}
                    >
                      <span className="block text-[10px] font-bold uppercase tracking-wider opacity-70">
                        {kindLabel} · {c.slide_count} slides
                      </span>
                      <span className="mt-0.5 line-clamp-2 max-w-[14rem]">
                        {c.hook_goal || c.topic_labels?.join(" · ") || c.title}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}

            {generatedCarousels.length > 0 && (
              <InlineTranscriptEditor
                carousels={generatedCarousels}
                activeCarouselId={activeCarouselId}
                onActivateCarousel={setActiveCarouselId}
                onChangeSlideText={(carouselId, slideIndex, text) => {
                  setGeneratedCarousels((prev) =>
                    prev.map((c) => {
                      if (c.id !== carouselId) return c;
                      const slides = c.slides.map((s, i) =>
                        i === slideIndex
                          ? {
                              ...s,
                              hook_line: text,
                              transcript_text: text,
                              snippet: text,
                            }
                          : s
                      );
                      return { ...c, slides, slide_count: slides.length };
                    })
                  );
                }}
              />
            )}

            {activeGeneratedCarousel && selectedVideo && (
              <InstagramCarouselPost
                title={activeGeneratedCarousel.title}
                slides={activeGeneratedCarousel.slides}
                driveFileId={selectedVideo.id}
                imagesReady={imagesReady}
                locked={pipelineLocked}
                layoutMode={carouselLayout}
                onLayoutModeChange={(mode) => {
                  if (pipelineLocked) return;
                  setCarouselLayout(mode);
                  const next = carouselsForLayout(
                    carouselLayouts ?? outline?.layouts,
                    mode,
                    generatedCarousels
                  );
                  if (next.length) {
                    setGeneratedCarousels(next);
                    if (!next.some((c) => c.id === activeCarouselId)) {
                      setActiveCarouselId(next[0]?.id ?? null);
                    }
                  }
                }}
                onSaveCopy={async (slides, references) => {
                  if (pipelineLocked) return;
                  await apiClient.carouselCopySave({
                    drive_file_id: selectedVideo.id,
                    layout_mode: carouselLayout,
                    slides,
                    theme: { title: activeGeneratedCarousel.title },
                    references,
                  });
                  const saves = await apiClient.carouselPipelineSaves(selectedVideo.id, 12, "carousel");
                  setCarouselSaves(saves.items ?? []);
                }}
                onRegenerateSlide={async (slide, index) => {
                  if (pipelineLocked) return;
                  const res = await apiClient.carouselRegenerateSlide({
                    drive_file_id: selectedVideo.id,
                    carousel_id: activeGeneratedCarousel.id,
                    slide_index: index,
                    slide,
                  });
                  setGeneratedCarousels((prev) =>
                    prev.map((c) =>
                      c.id === activeGeneratedCarousel.id
                        ? {
                            ...c,
                            slides: c.slides.map((s, i) => (i === index ? res.slide : s)),
                          }
                        : c
                    )
                  );
                }}
                onOpenSlide={(slide) =>
                  setPreviewCue({
                    start_sec: slide.timestamp_sec,
                    text: slide.hook_line,
                  })
                }
                onSlidesChange={(slides) => {
                  setGeneratedCarousels((prev) =>
                    prev.map((c) =>
                      c.id === activeGeneratedCarousel.id ? { ...c, slides } : c
                    )
                  );
                  setOutline((prev) =>
                    prev && prev.title === activeGeneratedCarousel.title
                      ? { ...prev, slides }
                      : prev
                  );
                }}
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

function InlineTranscriptEditor({
  carousels,
  activeCarouselId,
  onActivateCarousel,
  onChangeSlideText,
}: {
  carousels: CarouselGeneratedItem[];
  activeCarouselId: string | null;
  onActivateCarousel: (id: string) => void;
  onChangeSlideText: (carouselId: string, slideIndex: number, text: string) => void;
}) {
  const anyTranslated = carousels.some((c) =>
    c.slides.some((s) => Boolean(s.translated || s.original_text))
  );
  return (
    <div
      className="rounded-lg border border-border bg-muted/20 p-3 sm:p-4"
      data-testid="carousel-inline-transcript-editor"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Inline transcript editor
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            Review and edit English one-liners per hook before selecting images.
            {anyTranslated ? " Some lines were auto-translated from the source language." : ""}
          </p>
        </div>
      </div>
      <div className="mt-3 space-y-4">
        {carousels.map((car) => {
          const active = car.id === activeCarouselId;
          return (
            <div
              key={car.id}
              className={cn(
                "rounded-lg border p-3 transition",
                active ? "border-foreground/40 bg-background" : "border-border/70 bg-background/60"
              )}
            >
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 text-left"
                onClick={() => onActivateCarousel(car.id)}
              >
                <span>
                  <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                    Hook group · {car.slide_count} lines
                  </span>
                  <span className="mt-0.5 block text-sm font-semibold text-foreground">
                    {car.hook_goal || car.hooks?.[0] || car.title}
                  </span>
                </span>
                <span className="text-[10px] text-muted-foreground">{active ? "Editing" : "Open"}</span>
              </button>
              <ul className="mt-3 space-y-2">
                {car.slides.map((slide, i) => (
                  <li key={`${car.id}-slide-${i}`}>
                    <label className="block">
                      <span className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
                        <span>
                          Slide {i + 1} of {car.slides.length} · transcript
                          {slide.translated ? " · EN" : ""}
                        </span>
                        <span className="tabular-nums">
                          {formatTimestampRange(slide.timestamp_sec, slide.end_timestamp_sec)}
                        </span>
                      </span>
                      <textarea
                        className="studio-textarea studio-textarea-compact w-full text-sm leading-snug"
                        rows={1}
                        value={slide.transcript_text || slide.hook_line || ""}
                        onFocus={() => onActivateCarousel(car.id)}
                        onChange={(e) => onChangeSlideText(car.id, i, e.target.value)}
                        spellCheck
                        data-testid={`inline-transcript-${car.id}-${i}`}
                      />
                      {slide.original_text ? (
                        <span className="mt-1 block text-[10px] text-muted-foreground">
                          Source: {slide.original_text}
                        </span>
                      ) : null}
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function InstagramCarouselPost({
  title,
  slides,
  driveFileId,
  imagesReady,
  locked,
  onOpenSlide,
  onSlidesChange,
  onRegenerateSlide,
  layoutMode,
  onLayoutModeChange,
  onSaveCopy,
}: {
  title: string;
  slides: CarouselOutlineSlide[];
  driveFileId: string;
  imagesReady: boolean;
  locked: boolean;
  onOpenSlide: (slide: CarouselOutlineSlide) => void;
  onSlidesChange?: (slides: CarouselOutlineSlide[]) => void;
  onRegenerateSlide?: (slide: CarouselOutlineSlide, index: number) => Promise<void>;
  layoutMode: "single_1" | "split_2";
  onLayoutModeChange: (mode: "single_1" | "split_2") => void;
  onSaveCopy?: (
    slides: CarouselOutlineSlide[],
    references: Record<string, unknown>[]
  ) => Promise<void>;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(0);
  const [pickingFrame, setPickingFrame] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const n = slides.length;
  const current = slides[Math.min(Math.max(active, 0), Math.max(n - 1, 0))];
  const references = slides.map((slide, index) => ({
    type: "copy+image",
    slide_index: index,
    copy: slide.transcript_text || slide.hook_line || "",
    image: {
      drive_file_id: slide.drive_file_id,
      timestamp_sec: slide.frame_ts ?? slide.timestamp_sec,
      preview_url: slide.preview_url ?? null,
    },
  }));

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

  function updateSlideText(index: number, text: string) {
    const next = slides.map((s, i) =>
      i === index
        ? {
            ...s,
            hook_line: text,
            transcript_text: text,
            snippet: text,
          }
        : s
    );
    onSlidesChange?.(next);
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
        <div className="flex items-center gap-2">
          <select
            className="studio-select px-2 py-1 text-[11px]"
            value={layoutMode}
            onChange={(e) => onLayoutModeChange(e.target.value as "single_1" | "split_2")}
            aria-label="Carousel layout"
          >
            <option value="single_1">Single image</option>
            <option value="split_2">Split panels</option>
          </select>
          <button
            type="button"
            className="studio-btn studio-btn-ghost px-2 py-1 text-[11px]"
            title="Replace this slide's image with a frame from the transcript"
            onClick={() => setPickingFrame(true)}
            disabled={locked}
          >
            <ImageIcon size={14} />
            Frame
          </button>
          {imagesReady && onRegenerateSlide && (
            <button
              type="button"
              className="studio-btn studio-btn-ghost px-2 py-1 text-[11px]"
              disabled={regenerating || locked}
              title="Regenerate this slide frame"
              onClick={() => {
                setRegenerating(true);
                void onRegenerateSlide(current, active).finally(() => setRegenerating(false));
              }}
            >
              <RefreshCw size={14} className={cn(regenerating && "animate-spin")} />
              {regenerating ? "Working…" : "Regenerate"}
            </button>
          )}
          <span className="ig-post-count" aria-live="polite">
            {active + 1}/{n}
          </span>
        </div>
      </div>

      <p className="mt-3 text-xs text-muted-foreground" data-testid="carousel-transcript-editor">
        Slide lines are edited in the <span className="font-medium text-foreground">Inline transcript editor</span>{" "}
        above. Overlay below mirrors the active carousel.
      </p>

      <div className="ig-stage mt-4">
        <div
          ref={trackRef}
          className="ig-track"
          role="region"
          aria-roledescription="carousel"
          aria-label="Carousel slides"
        >
          {slides.map((slide, i) => {
            // A frame the user picked themselves shows immediately; pipeline
            // frames still wait for the explicit Select & filter images step.
            const manualFrame = slide.frame_source === "manual";
            const framesVisible = imagesReady || manualFrame;
            const showReal = framesVisible && Boolean(slide.preview_url);
            // Split needs two distinct frames from this slide's own span; without
            // them a split render would repeat the same still, so fall back
            // to the single-image layout instead.
            const rawPanels =
              layoutMode === "split_2" && (slide.panels?.length ?? 0) >= 2
                ? slide.panels!.slice(0, 2)
                : null;
            const splitPanels =
              rawPanels &&
              rawPanels[0]?.preview_url &&
              rawPanels[1]?.preview_url &&
              rawPanels[0].preview_url !== rawPanels[1].preview_url &&
              rawPanels[0].frame_ts !== rawPanels[1].frame_ts
                ? rawPanels
                : null;
            const panelCaptions = splitPanels
              ? splitPanelCaptions(
                  splitPanels,
                  slide.transcript_text || slide.hook_line || ""
                )
              : [];
            return (
              <article
                key={`${slide.index}-${slide.drive_file_id}-${slide.timestamp_sec}`}
                className={cn("ig-slide", splitPanels && "ig-slide-split")}
                aria-label={`Slide ${i + 1} of ${n}`}
                aria-hidden={i !== active}
              >
                {splitPanels ? (
                  splitPanels.map((panel, p) => (
                    <div className="ig-panel" key={`${slide.index}-panel-${p}`}>
                      {framesVisible && panel.preview_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={slideFrameUrl(panel.preview_url, slide.frame_source)}
                          alt=""
                          draggable={false}
                          style={focalPointStyle(panel)}
                        />
                      ) : (
                        <div
                          className="ig-slide-placeholder"
                          aria-hidden
                          data-testid="carousel-dummy-bg"
                        >
                          <span className="ig-slide-placeholder-icon">
                            <ImageIcon aria-hidden />
                          </span>
                          <span className="ig-slide-placeholder-label">
                            {imagesReady ? "No frame" : "Background pending"}
                          </span>
                        </div>
                      )}
                      <div className="ig-panel-scrim" aria-hidden />
                      <p className="ig-panel-caption">
                        {panelCaptions[p] || ""}
                      </p>
                    </div>
                  ))
                ) : (
                  <>
                    {showReal ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={slideFrameUrl(slide.preview_url!, slide.frame_source)}
                        alt=""
                        draggable={false}
                        style={focalPointStyle(slide)}
                      />
                    ) : (
                      <div
                        className="ig-slide-placeholder"
                        aria-hidden
                        data-testid="carousel-dummy-bg"
                      >
                        <span className="ig-slide-placeholder-icon">
                          <ImageIcon aria-hidden />
                        </span>
                        <span className="ig-slide-placeholder-label">
                          {imagesReady ? "No frame" : "Background pending"}
                        </span>
                      </div>
                    )}
                    <div className="ig-slide-scrim" aria-hidden />
                    <div className="ig-slide-body">
                      <p className="ig-slide-hook">
                        {slide.transcript_text || slide.hook_line || ""}
                      </p>
                      <p className="ig-slide-meta">
                        {formatTimestampRange(slide.timestamp_sec, slide.end_timestamp_sec)}
                        {" · transcript"}
                        {slide.frame_source === "ai" ? " · AI frame" : ""}
                        {slide.frame_source === "fallback" ? " · fallback" : ""}
                        {manualFrame ? " · your frame" : ""}
                        {!framesVisible ? " · placeholder" : ""}
                      </p>
                    </div>
                  </>
                )}
              </article>
            );
          })}
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
          {current.hook_line || current.transcript_text || ""}
        </p>
      </div>
      <div className="mt-3 rounded-lg border border-border bg-muted/20 px-3 py-2">
        <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
          Reference for active slide
        </p>
        <p className="mt-1 text-xs text-foreground">
          Copy + indexed frame at {formatTimestampRange(
            current.frame_ts ?? current.timestamp_sec,
            current.end_timestamp_sec
          )} are included when saving this generation.
        </p>
      </div>
      {onSaveCopy && (
        <button
          type="button"
          className="studio-btn studio-btn-ghost mt-3 w-full"
          onClick={() => void onSaveCopy(slides, references)}
        >
          Save copy for this generation
        </button>
      )}

      {!imagesReady && (
        <p className="mt-3 text-center text-xs text-muted-foreground">
          Dummy backgrounds shown — edit transcripts above, then{" "}
          <span className="font-medium text-foreground">Select &amp; filter images</span>.
        </p>
      )}

      {imagesReady && n > 1 && (
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
                <img
                  src={slideFrameUrl(slide.preview_url, slide.frame_source)}
                  alt=""
                  draggable={false}
                  style={focalPointStyle(slide)}
                />
              ) : (
                <span className="ig-slide-placeholder" style={{ position: "absolute", inset: 0 }} />
              )}
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

      {pickingFrame && (
        <TranscriptFramePicker
          driveFileId={driveFileId}
          startSec={current.timestamp_sec}
          endSec={current.end_timestamp_sec}
          hookText={current.hook_line}
          onClose={() => setPickingFrame(false)}
          onSave={(picked) => {
            if (!picked.length) return;
            const next = slides.map((slide, i) =>
              i === active ? withReplacedFrame(slide, picked[0]) : slide
            );
            onSlidesChange?.(next);
            setPickingFrame(false);
          }}
        />
      )}
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
        "studio-video-list studio-scroll-fade mt-2 divide-y divide-border overflow-y-auto rounded-lg border border-border",
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
                {item.analysed && item.original_text && item.original_text !== item.text ? (
                  <span className="mt-1 block text-[10px] leading-snug text-muted-foreground">
                    <span className="font-semibold uppercase tracking-wide">From transcript · </span>
                    <span className="line-clamp-2 italic">
                      {item.original_text}
                    </span>
                  </span>
                ) : null}
                {item.translated ? (
                  <span className="mt-1 block text-[10px] font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
                    Genuine hook · translated
                  </span>
                ) : item.analysed ? (
                  <span className="mt-1 block text-[10px] font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
                    Genuine hook · analysed
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
