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
  Clapperboard,
  Film,
  History,
  ImageIcon,
  Layers,
  Play,
  RefreshCw,
  Search,
  Sparkles,
  Target,
  Upload,
  Video,
  X,
} from "lucide-react";
import {
  apiAssetUrl,
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
  type CarouselItemFeedback,
  type CarouselItemReference,
  type Person,
} from "@/lib/api";
import { DownloadButton, LoadingLabel, ServiceErrorCard } from "@/components/ui";
import { ModalOverlay } from "@/components/modal";
import { useDismissible } from "@/lib/use-dismissible";
import { cn } from "@/lib/utils";
import {
  applyHookFrameOverrides,
  focalPointStyle,
  formatTimestampRange,
  slideFrameUrl,
  withReplacedFrame,
  withReplacedImage,
  type PickedFrame,
} from "./utils";
import { TopicsHooksTree, TranscriptFramePicker } from "./topics-hooks-tree";
import { ItemFeedback } from "@/components/item-feedback";
import { ItemReferences } from "@/components/item-references";

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
  const [selectedThemes, setSelectedThemes] = useState<CarouselPipelineTheme[]>([]);
  const [themeSaves, setThemeSaves] = useState<CarouselGenerationSaveListItem[]>([]);
  const [themeSaveId, setThemeSaveId] = useState<number | null>(null);
  const [themesFromCache, setThemesFromCache] = useState(false);
  const [themesMissing, setThemesMissing] = useState(false);
  const [extractFromCache, setExtractFromCache] = useState(false);
  const [themeHistoryOpen, setThemeHistoryOpen] = useState(false);
  const [loadingThemeSaves, setLoadingThemeSaves] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const [prerunBusy, setPrerunBusy] = useState(false);
  const [prerunNote, setPrerunNote] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const themeHistoryRef = useRef<HTMLDivElement>(null);
  useDismissible(themeHistoryOpen, () => setThemeHistoryOpen(false), themeHistoryRef);
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
  const [outlineError, setOutlineError] = useState<string | null>(null);
  const [imagesReady, setImagesReady] = useState(false);
  const [selectingImages, setSelectingImages] = useState(false);
  const [imageQualityNote, setImageQualityNote] = useState<string | null>(null);
  const [carouselLayout, setCarouselLayout] = useState<"single_1" | "split_2">("single_1");
  const [carouselLayouts, setCarouselLayouts] = useState<CarouselLayouts | null>(null);
  const [pipelineLocked, setPipelineLocked] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState("idle");
  const [carouselSaves, setCarouselSaves] = useState<CarouselGenerationSaveListItem[]>([]);
  const [genHistoryOpen, setGenHistoryOpen] = useState(false);
  const genHistoryRef = useRef<HTMLDetailsElement>(null);
  useDismissible(genHistoryOpen, () => setGenHistoryOpen(false), genHistoryRef);
  // Frames picked against a hook before slides exist, keyed by hook text.
  const [hookFrames, setHookFrames] = useState<Record<string, PickedFrame>>({});
  // Persisted thumbs/comments keyed by `${kind}:${target_key}`.
  const [itemFeedback, setItemFeedback] = useState<Record<string, CarouselItemFeedback>>({});
  // Persisted image/copy refs keyed by `${kind}:${target_key}` → list.
  const [itemReferences, setItemReferences] = useState<Record<string, CarouselItemReference[]>>(
    {}
  );
  const outlineRef = useRef<HTMLDivElement>(null);
  const phase3Ref = useRef<HTMLElement | null>(null);

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

  useEffect(() => {
    if (!selectedVideo) {
      setItemFeedback({});
      setItemReferences({});
      return;
    }
    let cancelled = false;
    void apiClient
      .carouselFeedbackList(selectedVideo.id)
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, CarouselItemFeedback> = {};
        for (const item of res.items ?? []) {
          map[`${item.target_kind}:${item.target_key}`] = item;
        }
        setItemFeedback(map);
      })
      .catch(() => {
        if (!cancelled) setItemFeedback({});
      });
    void apiClient
      .carouselReferencesList(selectedVideo.id)
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, CarouselItemReference[]> = {};
        for (const item of res.items ?? []) {
          const key = `${item.target_kind}:${item.target_key}`;
          (map[key] ??= []).push(item);
        }
        setItemReferences(map);
      })
      .catch(() => {
        if (!cancelled) setItemReferences({});
      });
    return () => {
      cancelled = true;
    };
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

  const loadRecentVideos = useCallback(async (signal?: AbortSignal) => {
    setLoadingRecent(true);
    setError(null);
    try {
      // Don't await persons here — a hung /persons used to stall the whole video list.
      const vids = await apiClient.carouselRecentVideos(5, true);
      if (signal?.aborted) return;
      setRecent(vids.items ?? []);
    } catch (e) {
      if (signal?.aborted) return;
      setError(formatApiError(e, "Could not load recent videos"));
    } finally {
      if (!signal?.aborted) setLoadingRecent(false);
    }
    void apiClient
      .persons()
      .then((people) => {
        if (!signal?.aborted) setPersons(people);
      })
      .catch(() => {
        if (!signal?.aborted) setPersons([]);
      });
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    void loadRecentVideos(ac.signal);
    return () => ac.abort();
  }, [loadRecentVideos]);

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
    setGeneratedCarousels([]);
    setCarouselLayouts(null);
    setActiveCarouselId(null);
    setOutlineError(null);
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
      generate?: boolean;
      signal?: AbortSignal;
    }) => {
      const { video, personName, entity, requestKey, selectionKey, force, generate, signal } = opts;
      setLoadingThemes(true);
      setPersonNotFound(null);
      setThemesMissing(false);
      try {
        const res = await apiClient.carouselPipelineThemes(video.id, {
          personName: personName || undefined,
          searchEntity: entity || undefined,
          force: Boolean(force),
          generate: Boolean(generate),
          signal,
        });
        if (signal?.aborted || themesRequestKeyRef.current !== requestKey) return;

        if (res.error === "person_not_found" || res.person_found === false) {
          const msg =
            res.message ||
            res.warning ||
          "That person isn’t in this video. Clear the person filter or pick a different video.";
          setPersonNotFound(msg);
          setThemes([]);
          setThemeSaveId(null);
          setThemesFromCache(false);
          setThemesMissing(false);
          setThemesLoadedKey(null);
          setPhase(1);
          return;
        }
        const nextThemes = res.themes ?? [];
        setThemes(nextThemes);
        setThemeSaveId(res.save_id ?? null);
        setThemesFromCache(Boolean(res.cache_hit));
        setThemesMissing(!nextThemes.length && !res.cache_hit);
        setThemesLoadedKey(selectionKey);
        if (res.warning && nextThemes.length === 0) setError(res.warning);
        else if (res.warning) setError(res.warning);
        // Never clobber an in-progress extract / hooks step back to themes.
        setPhase((p) => (p >= 3 ? p : 2));
        void refreshThemeSaves(video.id);
      } catch (e) {
        if (signal?.aborted || themesRequestKeyRef.current !== requestKey) return;
        if (e instanceof Error && e.name === "AbortError") return;
        setError(formatApiError(e, "Theme segmentation failed"));
        setThemes([]);
        setThemeSaveId(null);
        setThemesFromCache(false);
        setThemesMissing(false);
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
    setThemesMissing(false);
    setThemesLoadedKey(null);
    setThemeHistoryOpen(false);
    resetFromPhase2();
    setLoadingThemes(false);
    setError(null);
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
    // Prefer restoring a saved themes row when present and no person filter
    // (person path still needs presence check via the themes API).
    if (!personName && themeSaves.length > 0) {
      const latest = themeSaves[0];
      if (latest?.id) {
        await restoreThemeSave(latest.id);
        return;
      }
    }
    const requestKey = `${video.id}|${personName}|${entity}`;
    themesAbortRef.current?.abort();
    const ac = new AbortController();
    themesAbortRef.current = ac;
    themesRequestKeyRef.current = requestKey;
    setThemeHistoryOpen(false);
    setError(null);
    // Cache-only — never auto-call Gemini on Continue/Load.
    await loadThemesForVideo({
      video,
      personName,
      entity,
      requestKey,
      selectionKey: requestKey,
      force: false,
      generate: false,
      signal: ac.signal,
    });
  }

  async function generateThemes() {
    if (!selectedVideo || loadingThemes) return;
    const video = selectedVideo;
    const personName = personPick.trim();
    const fromObject = objectQuery.trim();
    const entity =
      personName && fromObject
        ? `${personName} / ${fromObject}`
        : personName || fromObject || "";
    const requestKey = `${video.id}|${personName}|${entity}|gen`;
    themesAbortRef.current?.abort();
    const ac = new AbortController();
    themesAbortRef.current = ac;
    themesRequestKeyRef.current = requestKey;
    setThemeHistoryOpen(false);
    setError(null);
    await loadThemesForVideo({
      video,
      personName,
      entity,
      requestKey,
      selectionKey: `${video.id}|${personName}|${entity}`,
      force: false,
      generate: true,
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
    setError(null);
    await loadThemesForVideo({
      video,
      personName,
      entity,
      requestKey,
      selectionKey,
      force: true,
      generate: false,
      signal: ac.signal,
    });
  }

  async function restoreThemeSave(saveId: number) {
    setError(null);
    try {
      const res = await apiClient.carouselPipelineSaveGet(saveId);
      const themesPayload = (res.payload?.themes ?? []) as CarouselPipelineTheme[];
      if (!themesPayload.length) {
        setError("That save has no themes.");
        return;
      }
      setThemes(themesPayload);
      setThemeSaveId(res.id);
      setThemesFromCache(true);
      setThemesMissing(false);
      setThemesLoadedKey(themesSelectionKey || null);
      setSelectedThemes([]);
      setExtract(null);
      setSelectedHooks([]);
      setSelectedTopics([]);
      setPhase(2);
      setThemeHistoryOpen(false);
    } catch (e) {
      setError(formatApiError(e, "Could not restore saved themes"));
    }
  }

  function selectVideo(video: CarouselRecentVideo) {
    // Switching video resets downstream; user must click Continue to load themes.
    setSelectedVideo(video);
  }

  async function uploadVideos(files: FileList | File[]) {
    const list = Array.from(files).filter((f) => f.type.startsWith("video/") || /\.(mp4|webm|mov|mkv|avi)$/i.test(f.name));
    if (!list.length) {
      setError("Pick a video file (mp4, webm, mov, mkv, avi).");
      return;
    }
    setUploading(true);
    setUploadNote(null);
    setError(null);
    try {
      const notes: string[] = [];
      for (const file of list) {
        const res = await apiClient.carouselUploadVideo(file);
        notes.push(`${res.name}: ${res.message}`);
      }
      setUploadNote(notes.join(" · "));
      await loadRecentVideos();
    } catch (e) {
      setError(formatApiError(e, "Upload failed"));
    } finally {
      setUploading(false);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  }

  async function runPrerun(opts?: { force?: boolean }) {
    if (prerunBusy) return;
    setPrerunBusy(true);
    setPrerunNote(null);
    setError(null);
    try {
      const ids = selectedVideo ? [selectedVideo.id] : [];
      const res = await apiClient.carouselPipelinePrerun({
        drive_file_ids: ids,
        force: Boolean(opts?.force),
      });
      const hits = res.items.filter((i) => i.themes_cache_hit || i.extract_cache_hit).length;
      const gens = res.items.filter((i) => i.themes_generated || i.extract_generated).length;
      setPrerunNote(
        `Pre-run finished: ${res.ok_count}/${res.count} ok · cache hits ${hits} · generated ${gens}`
      );
      if (selectedVideo) void refreshThemeSaves(selectedVideo.id);
    } catch (e) {
      setError(formatApiError(e, "Pre-run failed"));
    } finally {
      setPrerunBusy(false);
    }
  }

  const continueDisabledReason = !selectedVideo
    ? "Select a captioned video first"
    : loadingThemes
      ? "Loading themes…"
      : null;

  function onToggleTheme(theme: CarouselPipelineTheme) {
    setSelectedThemes((prev) => toggleTheme(prev, theme));
    setExtract(null);
    setExtractFromCache(false);
    setSelectedHooks([]);
    setSelectedTopics([]);
    setPhaseIntent(null);
    setPhaseIntentScore(null);
    setOutline(null);
    setGeneratedCarousels([]);
    setCarouselLayouts(null);
    setActiveCarouselId(null);
    setOutlineError(null);
    if (phase > 2) setPhase(2);
  }

  async function extractFromSelectedThemes(opts?: { force?: boolean; generate?: boolean }) {
    if (!selectedVideo || loadingExtract) return;
    if (!selectedThemes.length) {
      setError("Select at least one theme.");
      return;
    }
    const force = Boolean(opts?.force);
    // Explicit Extract/Generate button → generate on miss; Continue-style load uses generate=false.
    const generate = opts?.generate !== undefined ? Boolean(opts.generate) : true;
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
        force,
        generate,
        themes: ordered.map((t) => ({
          theme_id: t.theme_id,
          title: t.title,
          start_sec: t.start_sec,
          end_sec: t.end_sec,
          summary: t.summary,
        })),
      });
      const hasTree =
        (res.hooks?.length ?? 0) > 0 ||
        (res.topics?.length ?? 0) > 0 ||
        (res.topic_tree?.length ?? 0) > 0;
      if (!hasTree && !res.cache_hit) {
        setExtract(null);
        setExtractFromCache(false);
        setError(
          res.message ||
            res.warning ||
            "No cached hooks/topics for these themes. Click Extract to generate."
        );
        return;
      }
      setExtract(res);
      setExtractFromCache(Boolean(res.cache_hit));
      setSelectedHooks([]);
      setSelectedTopics([]);
      setPhaseIntent(res.intent ?? null);
      setPhaseIntentScore(res.intent_score ?? null);
      setPhase(3);
      // Reveal the current-generation tree (topics/hooks) under the themes step.
      requestAnimationFrame(() => {
        phase3Ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } catch (e) {
      setError(formatApiError(e, "Hook & topic extract failed"));
      setExtract(null);
      setExtractFromCache(false);
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

  async function generateCarousel(opts?: { force?: boolean }) {
    if (!selectedVideo || !selectedThemes.length || !extract || building) return;
    setBuilding(true);
    setOutlineError(null);
    setImageQualityNote(null);
    try {
      const hookPicks = selectedHooks.map((text) => toHookTimedPick(text, extract));
      // Explicit topics + parents implied by selected hooks → one carousel each.
      const topicPicks = expandTopicSeeds(selectedTopics, selectedHooks, extract);
      // Product model: one hook (or topic-as-goal) per request.
      const goals: CarouselTimedPick[] = hookPicks.length
        ? hookPicks
        : topicPicks.length
          ? topicPicks
          : [];
      if (!goals.length) {
        setOutlineError("Select at least one topic or hook.");
        return;
      }

      const force = Boolean(opts?.force);
      const merged: CarouselGeneratedItem[] = [];
      let lastRes: CarouselOutlineResponse | null = null;
      let layoutsAcc: CarouselLayouts | null = null;
      let anyImages = false;
      let cacheHits = 0;
      let generated = 0;

      for (let i = 0; i < goals.length; i++) {
        const goal = goals[i];
        const isHook = hookPicks.some((h) => h.text === goal.text);
        setImageQualityNote(
          `Building hook ${i + 1}/${goals.length}…`
        );
        const body = {
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
          hooks: isHook ? [goal] : [],
          topics: isHook
            ? []
            : [
                {
                  id: goal.id,
                  text: goal.text,
                  start_sec: goal.start_sec,
                  end_sec: goal.end_sec,
                  theme_id: goal.theme_id,
                },
              ],
          min_slides: 6,
          max_slides: 10,
          select_images: false,
          // Explicit Generate must always fall through to Gemini on cache miss.
          generate: true,
          force,
        };
        let res = await apiClient.carouselPipelineGenerate(body);
        const empty =
          !(res.carousels && res.carousels.length) && !(res.slides && res.slides.length);
        // Safety net: if a stale client/proxy dropped generate, retry once forced.
        if (empty && !res.cache_hit) {
          res = await apiClient.carouselPipelineGenerate({
            ...body,
            generate: true,
            force: true,
          });
        }
        if (res.cache_hit) cacheHits += 1;
        if (res.generated) generated += 1;
        const list =
          res.carousels && res.carousels.length
            ? res.carousels
            : res.slides?.length
              ? [
                  {
                    id: `hook_${i + 1}`,
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
        for (const c of list) {
          merged.push({
            ...c,
            id: c.id || `hook_${merged.length + 1}`,
            references: c.references ?? res.references ?? [],
          });
        }
        if (res.layouts) layoutsAcc = res.layouts;
        anyImages = anyImages || Boolean(res.images_ready);
        lastRes = res;
      }

      if (!merged.length) {
        setOutlineError(
          lastRes?.message ||
            lastRes?.warning ||
            "Generate returned no carousels. Try fewer hooks or another theme."
        );
        return;
      }
      // Re-id sequentially for stable tabs after per-hook merge.
      const normalized = merged.map((c, idx) => ({ ...c, id: `hook_${idx + 1}` }));
      const withPicks = applyHookFrameOverrides(normalized, hookFrames);
      setGeneratedCarousels(withPicks);
      setActiveCarouselId(withPicks[0]?.id ?? null);
      setImagesReady(anyImages);
      setCarouselLayouts(layoutsAcc);
      setOutline(
        lastRes
          ? {
              ...lastRes,
              carousels: withPicks,
              carousel_count: withPicks.length,
              slides: withPicks[0]?.slides ?? [],
              title: withPicks[0]?.title ?? lastRes.title,
            }
          : null
      );
      setOutlineError(null);
      if (cacheHits && !generated) {
        setImageQualityNote(`Served from cache (${cacheHits} hook${cacheHits === 1 ? "" : "s"}).`);
      } else if (cacheHits || generated) {
        setImageQualityNote(
          `Per-hook jobs: ${cacheHits} cache hit${cacheHits === 1 ? "" : "s"}, ${generated} generated.`
        );
      }
      setPhase(5);
      requestAnimationFrame(() => {
        outlineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } catch (e) {
      // Stay on phase 4 with a visible error — never silently wipe success state
      // without explaining why (error UI used to live only inside phase 5).
      setOutlineError(formatApiError(e, "Carousel generation failed"));
    } finally {
      setBuilding(false);
    }
  }

  async function selectCarouselImages() {
    if (!selectedVideo || selectingImages || !generatedCarousels.length) return;
    setSelectingImages(true);
    setOutlineError(null);
    setImageQualityNote(null);
    try {
      const res = await apiClient.carouselPipelineSelectImages({
        drive_file_id: selectedVideo.id,
        carousels: generatedCarousels,
      });
      const list = (res.carousels ?? []).map((c) => ({
        ...c,
        references: c.references ?? res.references ?? c.references,
      }));
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
              references: res.references ?? prev.references,
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
    } catch (e) {
      setOutlineError(formatApiError(e, "Image selection failed"));
    } finally {
      setSelectingImages(false);
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-10 pb-8">
      <header className="studio-rise">
        <p className="studio-eyebrow">Studio</p>
        <h1 className="studio-title">Create a carousel</h1>
        <p className="studio-lede">
          Start with a captioned video, choose the story beats you want, then generate
          polished slide layouts ready for Instagram.
        </p>
        <PhaseRail phase={phase} />
      </header>

      {error && (
        <ServiceErrorCard
          message={error}
          onDismiss={() => setError(null)}
          onRetry={() => void loadRecentVideos()}
          retrying={loadingRecent}
          retryLabel="Reload videos"
        />
      )}
      {pipelineLocked && (
        <div className="studio-callout" role="status">
          Generation is running. Editing is paused until it finishes.
        </div>
      )}

      <section className="studio-panel p-5 sm:p-8" data-testid="carousel-phase-1">
        <p className="studio-section-label">Step 1</p>
        <h2 className="studio-section-heading">
          <Video size={20} />
          Choose a video
        </h2>
        <p className="mt-2 text-sm text-slate-500">
          Use a video that already has captions. Upload a new file to index, or pre-run
          themes/hooks so studio clicks stay cache-first.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            ref={uploadInputRef}
            type="file"
            accept="video/mp4,video/webm,video/quicktime,video/x-matroska,.mp4,.webm,.mov,.mkv,.avi"
            className="hidden"
            multiple
            onChange={(e) => {
              if (e.target.files?.length) void uploadVideos(e.target.files);
            }}
          />
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            disabled={uploading}
            onClick={() => uploadInputRef.current?.click()}
            data-testid="carousel-upload-videos"
            title="Upload videos to index"
          >
            <Upload size={14} />
            {uploading ? <LoadingLabel>Uploading…</LoadingLabel> : "Upload files"}
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost"
            disabled={prerunBusy || (!selectedVideo && recent.length === 0)}
            onClick={() => void runPrerun({ force: false })}
            data-testid="carousel-prerun"
            title="Warm theme + extract caches for selected or recent videos"
          >
            <Sparkles size={14} />
            {prerunBusy ? <LoadingLabel>Pre-running…</LoadingLabel> : "Pre-run caches"}
          </button>
          {uploadNote && (
            <p className="w-full text-xs text-muted-foreground" role="status">
              {uploadNote}
            </p>
          )}
          {prerunNote && (
            <p className="w-full text-xs text-muted-foreground" role="status">
              {prerunNote}
            </p>
          )}
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <div className="studio-field">
            <label htmlFor="person-filter" className="studio-field-label">
              Person
              <span className="studio-field-hint">Optional</span>
            </label>
            <select
              id="person-filter"
              className="studio-select"
              value={personPick}
              onChange={(e) => setPersonPick(e.target.value)}
              disabled={persons.length === 0}
              aria-describedby={persons.length === 0 ? "person-filter-help" : undefined}
            >
              <option value="">Anyone</option>
              {persons.map((p) => (
                <option key={p.id} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            {persons.length === 0 ? (
              <p id="person-filter-help" className="studio-field-help">
                No people indexed yet. You can continue without a person filter.
              </p>
            ) : null}
          </div>
          <div className="studio-field">
            <label htmlFor="topic-context" className="studio-field-label">
              Topic focus
              <span className="studio-field-hint">Optional</span>
            </label>
            <input
              id="topic-context"
              className="studio-input"
              placeholder="e.g. admissions, fundraising…"
              value={objectQuery}
              onChange={(e) => setObjectQuery(e.target.value)}
            />
          </div>
        </div>

        {personNotFound && (
          <div className="studio-callout-zinc mt-4 rounded-xl px-4 py-3 text-sm" role="status">
            {personNotFound}
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="studio-segment" role="group" aria-label="Browse videos">
            {(
              [
                { id: "recent", label: "Recent" },
                { id: "all", label: "All videos" },
              ] as const
            ).map((opt) => {
              const active = videoScope === opt.id;
              return (
                <button
                  key={opt.id}
                  type="button"
                  className={cn("studio-segment-btn", active && "is-active")}
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
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500"
              />
              <input
                className="studio-input w-full !pl-10"
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
                <LoadingLabel>Loading recent videos…</LoadingLabel>
              </p>
            ) : recent.length === 0 ? (
              <div className="studio-empty mt-2">
                <Video size={28} />
                <p>No captioned videos yet. Index a video with transcripts, then refresh this list.</p>
              </div>
            ) : (
              <VideoPickList
                videos={recent}
                selectedId={selectedVideo?.id}
                onSelect={selectVideo}
                maxHeightClass="max-h-[min(24rem,50vh)]"
              />
            )
          ) : loadingAll ? (
            <p className="text-sm text-muted-foreground">
              <LoadingLabel>Searching library…</LoadingLabel>
            </p>
          ) : allVideosError ? (
            <p className="text-sm text-destructive" role="alert">
              {allVideosError}
            </p>
          ) : allVideos.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {debouncedQuery
                ? "No videos match that title."
                : "No captioned videos in the library yet."}
            </p>
          ) : (
            <VideoPickList
              videos={allVideos}
              selectedId={selectedVideo?.id}
              onSelect={selectVideo}
              maxHeightClass="max-h-[min(24rem,50vh)]"
            />
          )}
        </div>

        {selectedVideo && (
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
              Selected:{" "}
              <span className="font-medium text-foreground">{selectedVideo.name}</span>
              {themesNeedContinue
                ? " · click Continue in Step 2 to load themes"
                : themes.length > 0
                  ? ` · ${themes.length} themes loaded — select themes, then Extract`
                  : ""}
            </p>
            {themesNeedContinue && (
              <button
                type="button"
                className="studio-btn studio-btn-primary studio-btn-continue"
                onClick={(e) => {
                  e.preventDefault();
                  if (themeSaves.length > 0) void continueToThemes();
                  else void generateThemes();
                }}
                disabled={
                  Boolean(continueDisabledReason) ||
                  loadingThemes ||
                  loadingExtract ||
                  building ||
                  pipelineLocked
                }
                data-testid="carousel-continue-from-video"
                title={
                  continueDisabledReason ||
                  (themeSaves.length > 0
                    ? "Load cached themes"
                    : "Generate themes for this video")
                }
              >
                {loadingThemes ? (
                  <LoadingLabel>Loading themes…</LoadingLabel>
                ) : themeSaves.length > 0 ? (
                  <>
                    <Sparkles size={15} />
                    Load themes
                  </>
                ) : (
                  <>
                    Generate themes
                    <ArrowRight size={14} className="studio-btn-continue-arrow" />
                  </>
                )}
              </button>
            )}
          </div>
        )}
      </section>

      {selectedVideo && !personNotFound && (
        <section className="studio-panel p-5 sm:p-7" data-testid="carousel-phase-2">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="studio-section-label">Step 2</p>
              <h2 className="studio-section-heading">
                <Layers size={20} />
                Pick themes
              </h2>
              <p className="mt-2 text-sm text-slate-500">
                Themes are non-overlapping segments of the talk
                {personPick.trim() ? ` · “${personPick.trim()}” appears in this video` : ""}.
                Select one or more, then extract hooks and topics.
                {themesFromCache
                  ? " Restored from a saved set for this transcript."
                  : themeSaveId
                    ? " Saved for next time."
                    : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative" ref={themeHistoryRef}>
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
                    className="studio-scroll-fade absolute right-0 z-20 mt-1 max-h-[min(16rem,50vh)] w-72 overflow-x-hidden overflow-y-auto rounded-lg border border-slate-200 bg-white p-1 shadow-lg"
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
                          "flex w-full flex-col gap-0.5 rounded-md px-2 py-2 text-left hover:bg-zinc-50",
                          themeSaveId === s.id &&
                            "bg-slate-100 ring-1 ring-slate-900/20"
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
                onClick={(e) => {
                  e.preventDefault();
                  void regenerateThemes();
                }}
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
                    ? "Saved themes are available — load them (cache only, no Gemini)."
                    : "No cached themes yet — generate once; later clicks stay cache-first."}
              </p>
              {!loadingThemeSaves && (
                <button
                  type="button"
                  className={
                    themeSaves.length > 0
                      ? "studio-btn studio-btn-primary"
                      : "studio-btn studio-btn-primary studio-btn-continue"
                  }
                  onClick={(e) => {
                    e.preventDefault();
                    if (themeSaves.length > 0) void continueToThemes();
                    else void generateThemes();
                  }}
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
                      Generate themes
                      <ArrowRight size={14} className="studio-btn-continue-arrow" />
                    </>
                  )}
                </button>
              )}
            </div>
          ) : themes.length === 0 ? (
            <div className="mt-4 space-y-3">
              <p className="text-sm text-muted-foreground">
                {themesMissing
                  ? "No cached themes for this transcript. Generate themes explicitly — Continue never calls Gemini."
                  : "No themes found for this video yet."}
              </p>
              <button
                type="button"
                className="studio-btn studio-btn-primary"
                onClick={(e) => {
                  e.preventDefault();
                  void generateThemes();
                }}
                disabled={loadingThemes || pipelineLocked}
                data-testid="carousel-generate-themes"
              >
                {loadingThemes ? <LoadingLabel>Generating…</LoadingLabel> : "Generate themes"}
              </button>
            </div>
          ) : (
            <ul className="mt-4 space-y-2">
              {themes.map((t) => {
                const active = selectedThemes.some((x) => x.theme_id === t.theme_id);
                const fbKey = `theme:${t.theme_id || t.title}`;
                return (
                  <li key={t.theme_id} className="studio-theme-item">
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={active}
                      className={cn(
                        "studio-select-row flex w-full items-start gap-3 px-3 py-3 text-left",
                        active && "is-selected"
                      )}
                      onClick={() => onToggleTheme(t)}
                    >
                      <span
                        className={cn(
                          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded border",
                          active
                            ? "border-slate-900 bg-slate-900 text-white"
                            : "border-slate-200 bg-white"
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
                    {selectedVideo ? (
                      <div className="px-3 pb-1">
                        <ItemFeedback
                          driveFileId={selectedVideo.id}
                          kind="theme"
                          targetKey={t.theme_id || t.title}
                          targetLabel={t.title}
                          initial={itemFeedback[fbKey] ?? null}
                          onSaved={(item) =>
                            setItemFeedback((prev) => ({
                              ...prev,
                              [`${item.target_kind}:${item.target_key}`]: item,
                            }))
                          }
                        />
                        <ItemReferences
                          driveFileId={selectedVideo.id}
                          kind="theme"
                          targetKey={t.theme_id || t.title}
                          targetLabel={t.title}
                          frameStartSec={t.start_sec}
                          frameEndSec={t.end_sec}
                          items={itemReferences[fbKey] ?? []}
                          onAdded={(item) =>
                            setItemReferences((prev) => {
                              const key = `${item.target_kind}:${item.target_key}`;
                              const existing = prev[key] ?? [];
                              if (existing.some((r) => r.id === item.id)) return prev;
                              return { ...prev, [key]: [item, ...existing] };
                            })
                          }
                          onRemoved={(id) =>
                            setItemReferences((prev) => {
                              const next: Record<string, CarouselItemReference[]> = {};
                              for (const [k, list] of Object.entries(prev)) {
                                const filtered = list.filter((r) => r.id !== id);
                                if (filtered.length) next[k] = filtered;
                              }
                              return next;
                            })
                          }
                        />
                      </div>
                    ) : null}
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
                onClick={(e) => {
                  e.preventDefault();
                  void extractFromSelectedThemes({ generate: true });
                }}
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
                      : "Extract hooks & topics (cache-first; generates only on miss)"
                }
                data-testid="carousel-extract-themes"
              >
                {loadingExtract ? (
                  <LoadingLabel>Extracting hooks and topics… (can take 1–3 min)</LoadingLabel>
                ) : selectedThemes.length > 1 ? (
                  `Extract from ${selectedThemes.length} themes`
                ) : selectedThemes.length === 1 ? (
                  "Extract hooks & topics"
                ) : (
                  "Select themes, then Extract"
                )}
              </button>
              <button
                type="button"
                className="studio-btn studio-btn-ghost"
                onClick={(e) => {
                  e.preventDefault();
                  void extractFromSelectedThemes({ force: true, generate: false });
                }}
                disabled={
                  loadingExtract ||
                  loadingThemes ||
                  building ||
                  pipelineLocked ||
                  selectedThemes.length === 0
                }
                title="Force regenerate hooks & topics"
              >
                <RefreshCw size={14} className={cn(loadingExtract && "animate-spin")} />
                Regenerate extract
              </button>
              {selectedThemes.length > 0 ? (
                <p className="text-xs text-muted-foreground">
                  {selectedThemes.length} theme{selectedThemes.length === 1 ? "" : "s"} selected
                  {extractFromCache ? " · extract cache hit" : ""}
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
        <section
          ref={phase3Ref}
          className="studio-panel p-5 sm:p-7"
          data-testid="carousel-phase-3"
        >
          <p className="studio-section-label">Step 3</p>
          <h2 className="studio-section-heading">
            <Sparkles size={20} />
            Hooks & topics
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            Browse topics and subtopics, then select the hooks you want on slides. Nothing is
            pre-selected — pick intentionally. Images stay deferred until you generate and run
            frame selection.
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
              feedbackByKey={itemFeedback}
              onFeedbackSaved={(item) =>
                setItemFeedback((prev) => ({
                  ...prev,
                  [`${item.target_kind}:${item.target_key}`]: item,
                }))
              }
              referencesByKey={itemReferences}
              onReferenceAdded={(item) =>
                setItemReferences((prev) => {
                  const key = `${item.target_kind}:${item.target_key}`;
                  const existing = prev[key] ?? [];
                  if (existing.some((r) => r.id === item.id)) return prev;
                  return { ...prev, [key]: [item, ...existing] };
                })
              }
              onReferenceRemoved={(id) =>
                setItemReferences((prev) => {
                  const next: Record<string, CarouselItemReference[]> = {};
                  for (const [k, list] of Object.entries(prev)) {
                    const filtered = list.filter((r) => r.id !== id);
                    if (filtered.length) next[k] = filtered;
                  }
                  return next;
                })
              }
            />
          )}
          {phase < 4 && (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="studio-btn studio-btn-primary studio-btn-continue"
                onClick={(e) => {
                  e.preventDefault();
                  void goToPreviewIntent();
                }}
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
              <span
                className="text-xs text-muted-foreground"
                data-testid="topics-hooks-selection-count"
              >
                {selectedHooks.length + selectedTopics.length === 0
                  ? "Select a hook or topic to continue"
                  : [
                      selectedHooks.length
                        ? `${selectedHooks.length} hook${selectedHooks.length === 1 ? "" : "s"}`
                        : null,
                      selectedTopics.length
                        ? `${selectedTopics.length} topic${selectedTopics.length === 1 ? "" : "s"}`
                        : null,
                    ]
                      .filter(Boolean)
                      .join(" · ") + " selected"}
              </span>
            </div>
          )}
        </section>
      )}

      {extract && selectedThemes.length > 0 && phase >= 4 && (
        <section className="studio-panel p-5 sm:p-7" data-testid="carousel-phase-4">
          <p className="studio-section-label">Step 4</p>
          <h2 className="studio-section-heading">
            <Target size={20} />
            Preview & intent
          </h2>
          <p className="mt-2 text-sm text-slate-500">
            Markers from your selected hooks and topics
            {selectedThemes.length > 1
              ? ` across ${selectedThemes.length} themes`
              : ` in “${selectedThemes[0]?.title ?? "theme"}”`}
            . Intent sets direction only — no script is written here.
          </p>

          {(phaseIntent || extract.intent) && (
            <div className="mt-4 rounded-lg bg-white px-3 py-3 shadow-sm">
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
            className="studio-scroll-fade mt-4 max-h-[min(24rem,50vh)] divide-y divide-slate-200 overflow-x-hidden overflow-y-auto rounded-lg bg-white shadow-sm"
            data-testid="carousel-preview-markers"
          >
            {selectionPreviewMarkers.length === 0 ? (
              <li className="px-3 py-2 text-xs text-muted-foreground">
                No selected hooks or topics yet.
              </li>
            ) : (
              selectionPreviewMarkers.map((p) => {
                const isHook = p.label === "Hook";
                const isTheme = p.label === "Theme";
                let targetKey = p.text;
                if (isHook) {
                  const match = resolvePick(p.text, extract.hooks ?? []);
                  targetKey = match?.id || p.text;
                } else if (isTheme) {
                  const match = selectedThemes.find(
                    (t) => t.title === p.text || t.summary === p.text
                  );
                  targetKey = match?.theme_id || p.text;
                }
                const kind = isHook ? "hook" : isTheme ? "theme" : null;
                const fbKey = kind ? `${kind}:${targetKey}` : null;
                return (
                  <li key={`${p.label}-${p.start_sec}-${p.text.slice(0, 40)}`} className="px-0 py-0">
                    <button
                      type="button"
                      className="flex w-full items-start gap-2 px-3 py-2 text-left text-sm hover:bg-zinc-50"
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
                    {kind && selectedVideo ? (
                      <div className="px-3 pb-2">
                        <ItemFeedback
                          driveFileId={selectedVideo.id}
                          kind={kind}
                          targetKey={targetKey}
                          targetLabel={p.text}
                          initial={fbKey ? itemFeedback[fbKey] ?? null : null}
                          onSaved={(item) =>
                            setItemFeedback((prev) => ({
                              ...prev,
                              [`${item.target_kind}:${item.target_key}`]: item,
                            }))
                          }
                        />
                        {fbKey ? (
                          <ItemReferences
                            driveFileId={selectedVideo.id}
                            kind={kind}
                            targetKey={targetKey}
                            targetLabel={p.text}
                            frameStartSec={p.start_sec}
                            frameEndSec={p.end_sec}
                            items={itemReferences[fbKey] ?? []}
                            onAdded={(item) =>
                              setItemReferences((prev) => {
                                const key = `${item.target_kind}:${item.target_key}`;
                                const existing = prev[key] ?? [];
                                if (existing.some((r) => r.id === item.id)) return prev;
                                return { ...prev, [key]: [item, ...existing] };
                              })
                            }
                            onRemoved={(id) =>
                              setItemReferences((prev) => {
                                const next: Record<string, CarouselItemReference[]> = {};
                                for (const [k, list] of Object.entries(prev)) {
                                  const filtered = list.filter((r) => r.id !== id);
                                  if (filtered.length) next[k] = filtered;
                                }
                                return next;
                              })
                            }
                          />
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                );
              })
            )}
          </ul>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="studio-btn studio-btn-primary"
              onClick={(e) => {
                e.preventDefault();
                void generateCarousel({ force: false });
              }}
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
                    : "One hook per job — cache-first, generates only on miss"
              }
              data-testid="carousel-generate"
            >
              {building ? (
                <LoadingLabel>Building carousels… (one hook at a time)</LoadingLabel>
              ) : (
                "Generate carousels"
              )}
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-ghost"
              onClick={(e) => {
                e.preventDefault();
                void generateCarousel({ force: true });
              }}
              disabled={
                building ||
                selectingImages ||
                loadingExtract ||
                pipelineLocked ||
                (!selectedHooks.length && !selectedTopics.length)
              }
              title="Force regenerate all selected hooks (bypass cache)"
            >
              <RefreshCw size={14} className={cn(building && "animate-spin")} />
              Regenerate carousels
            </button>
            {outlineError && phase < 5 && (
              <p className="text-xs font-medium text-destructive" role="alert">
                {outlineError}
              </p>
            )}
          </div>
        </section>
      )}

      <div ref={outlineRef}>
        {phase >= 5 && (
          <section className="studio-panel space-y-4 p-5 sm:p-7" data-testid="carousel-phase-5">
            <div>
              <p className="studio-section-label">Step 5</p>
              <h2 className="studio-section-heading">
                <Clapperboard size={20} />
                {generatedCarousels.length > 0
                  ? `${generatedCarousels.length} carousel${generatedCarousels.length === 1 ? "" : "s"}`
                  : activeGeneratedCarousel?.title || outline?.title || "Your carousels"}
              </h2>
              <p className="mt-2 text-sm text-slate-500">
                Carousel text from your selection. Edit copy if needed, then click{" "}
                <strong>Select &amp; filter images</strong> — frames are not fetched until that click.
              </p>
            </div>

            {outlineError && (
              <p className="text-xs font-medium text-destructive" role="alert">
                {outlineError}
              </p>
            )}

            {generatedCarousels.length === 0 && !outlineError && (
              <p className="text-sm text-muted-foreground" role="status">
                No carousel text yet. Go back to Step 4 and click Generate carousels.
              </p>
            )}

            {carouselSaves.length > 0 && (
              <details
                ref={genHistoryRef}
                className="rounded-lg bg-white px-3 py-2 shadow-sm"
                open={genHistoryOpen}
                onToggle={(e) => setGenHistoryOpen(e.currentTarget.open)}
              >
                <summary className="cursor-pointer text-xs font-semibold text-foreground">
                  Generation history ({carouselSaves.length}) — restore is a click, never auto
                </summary>
                <div className="studio-scroll-fade mt-2 max-h-[min(16rem,40vh)] space-y-1 overflow-x-hidden overflow-y-auto">
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
                            if (!list.length) {
                              setOutlineError("That save has no carousel slides.");
                              return;
                            }
                            setGeneratedCarousels(list as CarouselGeneratedItem[]);
                            setActiveCarouselId(list[0]?.id ?? null);
                            setImagesReady(Boolean((list[0] as CarouselGeneratedItem)?.images_ready));
                            setOutline({
                              source: "user_restore",
                              title: (list[0] as CarouselGeneratedItem)?.title || "Carousel",
                              slide_count: (list[0] as CarouselGeneratedItem)?.slide_count ?? 0,
                              hooks: [],
                              topics: [],
                              slides: (list[0] as CarouselGeneratedItem)?.slides ?? [],
                              carousels: list as CarouselGeneratedItem[],
                              carousel_count: list.length,
                              images_ready: Boolean((list[0] as CarouselGeneratedItem)?.images_ready),
                            });
                            setPhase(5);
                          }).catch((e) =>
                            setOutlineError(formatApiError(e, "Could not restore generation"))
                          );
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
                  className="studio-btn studio-btn-primary"
                  onClick={(e) => {
                    e.preventDefault();
                    void selectCarouselImages();
                  }}
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
                    <LoadingLabel>Selecting frames…</LoadingLabel>
                  ) : imagesReady ? (
                    <>
                      <RefreshCw size={15} aria-hidden />
                      Re-run image selection
                    </>
                  ) : (
                    <>
                      <ImageIcon size={15} aria-hidden />
                      Select &amp; filter images
                      <ArrowRight size={14} className="studio-btn-continue-arrow" aria-hidden />
                    </>
                  )}
                </button>
                {imageQualityNote && (
                  <p className="text-xs text-slate-500">{imageQualityNote}</p>
                )}
                {!imagesReady && !selectingImages && (
                  <p className="text-xs text-slate-500">
                    Edit slide text below, then run image selection once.
                  </p>
                )}
              </div>
            )}

            {generatedCarousels.length > 0 && (
              <div
                className="flex flex-wrap items-stretch gap-2"
                role="tablist"
                aria-label="Generated carousels"
                data-testid="carousel-tablist"
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
                      className={cn("studio-carousel-tab", on && "is-selected")}
                      onClick={() => setActiveCarouselId(c.id)}
                    >
                      <span className="studio-carousel-tab-kind">
                        {kindLabel} · {c.slide_count} slides
                      </span>
                      <span className="studio-carousel-tab-title">
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
                references={
                  activeGeneratedCarousel.references ??
                  outline?.references ??
                  []
                }
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
      className="rounded-lg bg-white p-3 shadow-sm sm:p-4"
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
                active
                  ? "border-slate-900 bg-white shadow-[0_0_0_3px_rgba(15,23,42,0.12)]"
                  : "border-slate-200 bg-white"
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
                <span
                  className="studio-btn studio-btn-ghost pointer-events-none shrink-0"
                  aria-hidden
                >
                  {active ? "Editing" : "Open"}
                </span>
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
  references: attachedRefs = [],
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
  references?: CarouselItemReference[];
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
  const imageFileRef = useRef<HTMLInputElement>(null);
  const [active, setActive] = useState(0);
  const [pickingFrame, setPickingFrame] = useState(false);
  const [changingImage, setChangingImage] = useState(false);
  const [imageUrlDraft, setImageUrlDraft] = useState("");
  const [imageBusy, setImageBusy] = useState(false);
  const [imageNote, setImageNote] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const n = slides.length;
  const current = slides[Math.min(Math.max(active, 0), Math.max(n - 1, 0))];
  const imageRefs = attachedRefs.filter((r) => r.ref_kind === "image" && r.image_url);
  const copyRefs = attachedRefs.filter((r) => r.ref_kind === "copy" && r.copy_text);
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

  function applySlideImage(previewUrl: string, frameTs?: number | null) {
    const next = slides.map((slide, i) =>
      i === active ? withReplacedImage(slide, { preview_url: previewUrl, frame_ts: frameTs }) : slide
    );
    onSlidesChange?.(next);
    setChangingImage(false);
    setImageUrlDraft("");
    setImageNote("Image updated");
    setTimeout(() => setImageNote(null), 1200);
  }

  async function uploadSlideImage(file: File | null | undefined) {
    if (!file) return;
    setImageBusy(true);
    setImageNote("Uploading…");
    try {
      const uploaded = await apiClient.carouselReferenceUploadImage(file);
      applySlideImage(uploaded.url, current?.frame_ts ?? current?.timestamp_sec);
    } catch (e) {
      setImageNote(formatApiError(e, "Upload failed"));
    } finally {
      setImageBusy(false);
      if (imageFileRef.current) imageFileRef.current.value = "";
    }
  }

  if (!n || !current) {
    return <p className="text-sm text-muted-foreground">No slides yet — generate a carousel first.</p>;
  }

  return (
    <div className="ig-post studio-fade-in" data-testid="ig-carousel-post">
      <div className="ig-post-header">
        <div className="ig-post-header-row">
          <p className="ig-post-title" title={title}>
            {title}
          </p>
          <span className="ig-post-count" aria-live="polite">
            {active + 1}/{n}
          </span>
        </div>
        <div className="ig-post-actions" role="toolbar" aria-label="Slide frame controls">
          <div className="studio-field studio-field-inline">
            <label htmlFor="carousel-layout" className="sr-only">
              Layout
            </label>
            <select
              id="carousel-layout"
              className="studio-select ig-post-action-control"
              value={layoutMode}
              onChange={(e) => onLayoutModeChange(e.target.value as "single_1" | "split_2")}
              aria-label="Layout"
            >
              <option value="single_1">Single image</option>
              <option value="split_2">Split panels</option>
            </select>
          </div>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Replace this slide's image with a frame from the transcript"
            onClick={() => {
              setChangingImage(false);
              setPickingFrame(true);
            }}
            disabled={locked}
          >
            <ImageIcon size={14} />
            Frame
          </button>
          <button
            type="button"
            className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
            title="Change this slide's image (upload, URL, or attached ref)"
            onClick={() => {
              setPickingFrame(false);
              setChangingImage((v) => !v);
            }}
            disabled={locked || imageBusy}
          >
            <Upload size={14} />
            Image
          </button>
          {imagesReady && onRegenerateSlide && (
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm ig-post-action-btn"
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
        </div>
      </div>

      {changingImage ? (
        <div className="mt-3 rounded-lg border border-border bg-white px-3 py-2" data-testid="slide-image-changer">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Change slide image
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input
              ref={imageFileRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif,.jpg,.jpeg,.png,.webp,.gif"
              className="sr-only"
              disabled={imageBusy || locked}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void uploadSlideImage(f);
              }}
            />
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              disabled={imageBusy || locked}
              onClick={() => imageFileRef.current?.click()}
            >
              <Upload size={12} />
              Upload file
            </button>
            <input
              type="url"
              className="studio-input min-w-[12rem] flex-1 px-2 py-1 text-xs"
              placeholder="Paste image URL…"
              value={imageUrlDraft}
              disabled={imageBusy || locked}
              onChange={(e) => setImageUrlDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && imageUrlDraft.trim()) {
                  e.preventDefault();
                  applySlideImage(imageUrlDraft.trim());
                }
              }}
            />
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              disabled={imageBusy || locked || !imageUrlDraft.trim()}
              onClick={() => applySlideImage(imageUrlDraft.trim())}
            >
              Use URL
            </button>
            <button
              type="button"
              className="studio-btn studio-btn-ghost studio-btn-sm"
              onClick={() => setChangingImage(false)}
            >
              Close
            </button>
          </div>
          {imageRefs.length > 0 ? (
            <ul className="mt-2 flex flex-wrap gap-2">
              {imageRefs.map((r) => {
                const src =
                  r.image_url?.startsWith("http")
                    ? r.image_url
                    : apiAssetUrl(r.image_url || "");
                return (
                  <li key={r.id}>
                    <button
                      type="button"
                      className="overflow-hidden rounded border border-border"
                      title={r.note || r.image_url || "Attached ref"}
                      disabled={imageBusy || locked}
                      onClick={() =>
                        applySlideImage(r.image_url!, r.frame_ts ?? current.frame_ts)
                      }
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={src} alt="" className="h-12 w-12 object-cover" />
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {imageNote ? <p className="mt-1 text-[11px] text-muted-foreground">{imageNote}</p> : null}
        </div>
      ) : null}

      {(imageRefs.length > 0 || copyRefs.length > 0) && (
        <div className="mt-3 rounded-lg border border-border bg-slate-50 px-3 py-2" data-testid="carousel-attached-refs">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            Attached references used for this carousel
          </p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {imageRefs.map((r) => {
              const src =
                r.image_url?.startsWith("http")
                  ? r.image_url
                  : apiAssetUrl(r.image_url || "");
              return (
                <li key={`img-${r.id}`} className="flex items-center gap-2 text-xs text-foreground">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={src} alt="" className="h-8 w-8 rounded object-cover" />
                  <span className="truncate">
                    <span className="font-medium">Image</span>
                    {r.note ? ` · ${r.note}` : ""}
                    {r.target_kind ? ` · ${r.target_kind}` : ""}
                  </span>
                </li>
              );
            })}
            {copyRefs.map((r) => (
              <li key={`copy-${r.id}`} className="text-xs text-foreground">
                <span className="font-medium">Copy</span>
                {r.note ? ` · ${r.note}` : ""}:{" "}
                <span className="text-muted-foreground">{r.copy_text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

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
            // them a split render would repeat a neighbour's still, so fall back
            // to the single-image layout instead.
            const splitPanels =
              layoutMode === "split_2" && (slide.panels?.length ?? 0) >= 2
                ? slide.panels!.slice(0, 2)
                : null;
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
                        {panel.caption ||
                          (p === 0
                            ? slide.transcript_text || slide.hook_line || ""
                            : "")}
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
      <div className="mt-3 rounded-lg bg-white px-3 py-2 shadow-sm">
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
          onPick={(item) => {
            const next = slides.map((slide, i) =>
              i === active ? withReplacedFrame(slide, item) : slide
            );
            onSlidesChange?.(next);
            setPickingFrame(false);
          }}
        />
      )}
    </div>
  );
}

/** Mini list poster: YouTube hqdefault for `yt:` ids / `[id]` in name, else early cached frame. */
function videoListThumbUrl(video: CarouselRecentVideo): string | null {
  const id = (video.id || "").trim();
  if (id.startsWith("yt:")) {
    const ytId = id.slice(3).trim();
    if (ytId) return `https://i.ytimg.com/vi/${ytId}/hqdefault.jpg`;
  }
  const fromName = (video.name || "").match(/\[([A-Za-z0-9_-]{11})\]/);
  if (fromName?.[1]) {
    return `https://i.ytimg.com/vi/${fromName[1]}/hqdefault.jpg`;
  }
  if (!id) return null;
  return cacheOnlyAssetUrl(`/media/video/${encodeURIComponent(id)}/frame?ts=0`);
}

function VideoListThumb({ video }: { video: CarouselRecentVideo }) {
  const [failed, setFailed] = useState(false);
  const src = failed ? null : videoListThumbUrl(video);

  if (!src) {
    return (
      <span className="studio-video-thumb is-empty" aria-hidden>
        <Film size={14} strokeWidth={1.5} />
      </span>
    );
  }

  return (
    <span className="studio-video-thumb" aria-hidden>
      <img
        src={src}
        alt=""
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
      />
    </span>
  );
}

function VideoPickList({
  videos,
  selectedId,
  onSelect,
  disabled,
  maxHeightClass = "max-h-[min(24rem,50vh)]",
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
        "studio-video-list studio-scroll-fade overflow-x-hidden overflow-y-auto",
        maxHeightClass
      )}
    >
      {videos.map((v) => {
        const active = selectedId === v.id;
        return (
          <li key={v.id}>
            <button
              type="button"
              className={cn("studio-video-row", active && "is-active")}
              onClick={() => onSelect(v)}
              disabled={disabled}
            >
              <span className={cn("studio-check", active && "is-on")} aria-hidden>
                {active ? <Check size={12} strokeWidth={2.5} /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-semibold text-slate-900">{v.name}</span>
                <span className="mt-0.5 block truncate text-xs text-zinc-500">
                  {v.has_captions !== false ? `${v.cue_count ?? "…"} cues · ` : "No captions · "}
                  {v.path || v.mime_type}
                </span>
              </span>
              <VideoListThumb video={v} />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function PhaseRail({ phase }: { phase: Phase }) {
  const steps = [
    { n: 1, label: "Video", Icon: Video },
    { n: 2, label: "Themes", Icon: Layers },
    { n: 3, label: "Hooks", Icon: Sparkles },
    { n: 4, label: "Intent", Icon: Target },
    { n: 5, label: "Carousel", Icon: Clapperboard },
  ] as const;
  return (
    <ol className="studio-phase-rail" aria-label="Studio steps">
      {steps.map((s) => (
        <li
          key={s.n}
          className={cn(
            "studio-phase-chip",
            phase === s.n && "is-active",
            phase > s.n && "is-done"
          )}
        >
          {phase > s.n ? <Check size={12} /> : <s.Icon size={12} />}
          <span>
            {s.n}. {s.label}
          </span>
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
          <li className="text-xs text-muted-foreground">Nothing in this theme window yet.</li>
        )}
        {items.map((item) => {
          const on = selected.includes(item.text);
          const display = quote ? `\u201C${item.text}\u201D` : item.text;
          return (
            <li key={item.id} className="flex gap-1">
              <button
                type="button"
                className={cn(
                  "studio-select-row min-w-0 flex-1 px-2.5 py-2 text-left text-xs",
                  on ? "is-selected font-semibold" : ""
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
      <div className="border-t border-slate-200 px-4 py-4">
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
