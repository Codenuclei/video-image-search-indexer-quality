"use client";

import { useSyncExternalStore } from "react";
import {
  apiClient,
  faceThumbnailUrl,
  formatApiError,
  getApiErrorStatus,
  isAbortError,
  isServiceUnavailableMessage,
  type FaceCrawlResponse,
  type FaceSearchMatch,
  type FaceSearchResponse,
  type LeadershipPerson,
  type LeadershipRoster,
} from "@/lib/api";
import { ensureAppSession } from "@/lib/app-session";
import { hydrateKeyFromDisk, readCache, writeCache } from "@/lib/data-cache";
import { stableJsonHash } from "@/lib/fingerprints";

const LEADERSHIP_ROSTER_CACHE_KEY = "leadershipRoster:executive";

export type ReverseFaceSession = {
  dragOver: boolean;
  file: File | null;
  previewUrl: string | null;
  searching: boolean;
  result: FaceSearchResponse | null;
  crawlUrls: string;
  crawling: boolean;
  crawlResult: FaceCrawlResponse | null;
  error: string | null;
  roster: LeadershipRoster | null;
  rosterLoading: boolean;
  rosterHydrated: boolean;
  selectedLeader: LeadershipPerson | null;
  tagging: boolean;
  tagMessage: string | null;
  confirmTagOpen: boolean;
};

const initial: ReverseFaceSession = {
  dragOver: false,
  file: null,
  previewUrl: null,
  searching: false,
  result: null,
  crawlUrls: "",
  crawling: false,
  crawlResult: null,
  error: null,
  roster: null,
  rosterLoading: true,
  rosterHydrated: false,
  selectedLeader: null,
  tagging: false,
  tagMessage: null,
  confirmTagOpen: false,
};

let state: ReverseFaceSession = initial;
const listeners = new Set<() => void>();
let searchJob: Promise<void> | null = null;
let crawlJob: Promise<void> | null = null;
let searchGeneration = 0;
let searchAbort: AbortController | null = null;

function bumpSearchGeneration() {
  searchGeneration += 1;
  searchJob = null;
  if (searchAbort) {
    searchAbort.abort();
    searchAbort = null;
  }
  return searchGeneration;
}

function isStaleSearch(gen: number) {
  return gen !== searchGeneration;
}

/** One automatic retry on transient service-unavailable / cold-start failures. */
async function withNetworkRetry<T>(
  fn: () => Promise<T>,
  opts: { signal?: AbortSignal; isStale: () => boolean; delayMs?: number }
): Promise<T> {
  const delayMs = opts.delayMs ?? 800;
  try {
    return await fn();
  } catch (e) {
    if (opts.signal?.aborted || isAbortError(e) || opts.isStale()) throw e;
    const msg = formatApiError(e, "Request failed");
    if (!isServiceUnavailableMessage(msg)) throw e;
    await new Promise((r) => setTimeout(r, delayMs));
    if (opts.isStale() || opts.signal?.aborted) throw e;
    return await fn();
  }
}

function emit() {
  listeners.forEach((fn) => fn());
}

export function getReverseFaceSession(): ReverseFaceSession {
  return state;
}

export function patchReverseFaceSession(patch: Partial<ReverseFaceSession>) {
  state = { ...state, ...patch };
  emit();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useReverseFaceSession(): ReverseFaceSession {
  return useSyncExternalStore(subscribe, getReverseFaceSession, getReverseFaceSession);
}

function reverseFaceErrorMessage(error: unknown, fallback: string): string {
  const status = getApiErrorStatus(error);
  if (status === 401) {
    return "Session expired — please sign out and sign in again";
  }
  return formatApiError(error, fallback);
}

export async function hydrateLeadershipRoster(force = false) {
  if (state.rosterHydrated && !force) return;
  // Sync httpOnly app session before roster / leader face calls (collections BFFs share it).
  await ensureAppSession();
  let cachedRoster: LeadershipRoster | null = null;
  if (!force) {
    hydrateKeyFromDisk(LEADERSHIP_ROSTER_CACHE_KEY);
    cachedRoster = readCache<LeadershipRoster>(LEADERSHIP_ROSTER_CACHE_KEY)?.data ?? null;
  }
  patchReverseFaceSession({
    roster: cachedRoster ?? state.roster,
    rosterLoading: !cachedRoster,
    rosterHydrated: Boolean(cachedRoster),
    error: null,
  });
  try {
    // silent: panel owns errors — avoid spurious global session-expired toasts
    const roster = await apiClient.leadershipRoster("executive", { silent: true });
    writeCache(
      LEADERSHIP_ROSTER_CACHE_KEY,
      roster,
      stableJsonHash(roster),
      true
    );
    patchReverseFaceSession({ roster, rosterLoading: false, rosterHydrated: true });
  } catch (e) {
    patchReverseFaceSession({
      error: reverseFaceErrorMessage(e, "Failed to load Executive Leaders"),
      roster: cachedRoster ?? state.roster,
      rosterLoading: false,
      rosterHydrated: true,
    });
  }
}

export function setReverseFaceFile(next: File | null) {
  bumpSearchGeneration();
  const prevUrl = state.previewUrl;
  if (prevUrl) URL.revokeObjectURL(prevUrl);
  patchReverseFaceSession({
    file: next,
    result: null,
    error: null,
    tagMessage: null,
    selectedLeader: null,
    searching: false,
    previewUrl: next ? URL.createObjectURL(next) : null,
  });
}

/** Clear current face-search results and upload (dustbin). */
export function clearReverseFaceSearch() {
  bumpSearchGeneration();
  const prevUrl = state.previewUrl;
  if (prevUrl) URL.revokeObjectURL(prevUrl);
  patchReverseFaceSession({
    file: null,
    previewUrl: null,
    result: null,
    error: null,
    tagMessage: null,
    selectedLeader: null,
    searching: false,
    confirmTagOpen: false,
  });
}

export function runReverseFaceSearch(upload?: File) {
  const target = upload ?? state.file;
  if (!target) return searchJob;
  void import("@/lib/search-session").then(({ cancelSearch }) => {
    // Drop in-flight text search; keep prior text results until face results arrive.
    cancelSearch({ clearResults: false });
  });
  const gen = bumpSearchGeneration();
  searchAbort = new AbortController();
  const signal = searchAbort.signal;
  patchReverseFaceSession({
    searching: true,
    error: null,
    tagMessage: null,
    selectedLeader: null,
    result: null,
  });
  let job!: Promise<void>;
  job = (async () => {
    try {
      const result = await withNetworkRetry(
        () => apiClient.searchUploadedFace(target, 20, signal),
        { signal, isStale: () => isStaleSearch(gen) }
      );
      if (isStaleSearch(gen)) return;
      patchReverseFaceSession({ result, searching: false });
    } catch (e) {
      if (isStaleSearch(gen)) return;
      if (signal.aborted || isAbortError(e)) {
        patchReverseFaceSession({ searching: false, error: null });
        return;
      }
      patchReverseFaceSession({
        error: formatApiError(e, "Face search failed"),
        result: null,
        searching: false,
      });
    } finally {
      if (searchJob === job) searchJob = null;
      if (searchAbort?.signal === signal) searchAbort = null;
    }
  })();
  searchJob = job;
  return job;
}

export function selectReverseFaceLeader(person: LeadershipPerson) {
  if (!person.image_url) {
    patchReverseFaceSession({ error: `No portrait URL for ${person.name}` });
    return searchJob;
  }
  void import("@/lib/search-session").then(({ cancelSearch }) => {
    cancelSearch({ clearResults: false });
  });
  const gen = bumpSearchGeneration();
  searchAbort = new AbortController();
  const signal = searchAbort.signal;
  const prevUrl = state.previewUrl;
  if (prevUrl) URL.revokeObjectURL(prevUrl);
  patchReverseFaceSession({
    selectedLeader: person,
    file: null,
    previewUrl: null,
    searching: true,
    error: null,
    tagMessage: null,
    result: null,
  });
  let job!: Promise<void>;
  job = (async () => {
    try {
      await ensureAppSession();
      if (isStaleSearch(gen)) return;
      const result = await withNetworkRetry(
        () => apiClient.searchFaceByUrl(person.image_url!, 20, signal, { silent: true }),
        { signal, isStale: () => isStaleSearch(gen) }
      );
      if (isStaleSearch(gen)) return;
      patchReverseFaceSession({ result, searching: false, error: null });
    } catch (e) {
      if (isStaleSearch(gen)) return;
      if (signal.aborted || isAbortError(e)) {
        patchReverseFaceSession({ searching: false, error: null });
        return;
      }
      patchReverseFaceSession({
        error: reverseFaceErrorMessage(e, "Leader face search failed"),
        result: null,
        searching: false,
      });
    } finally {
      if (searchJob === job) searchJob = null;
      if (searchAbort?.signal === signal) searchAbort = null;
    }
  })();
  searchJob = job;
  return job;
}

/**
 * Retarget reverse-face to an Indexed Person (representative face) without wiping
 * into caption-only text search — used when the Search person filter changes
 * while face results are already on screen.
 *
 * Returns true when a face retarget was started. Returns false when there is no
 * representative face thumb — clears reverse-face so the caller can fall back to
 * a normal person-aware text search (avoids pairing a new person with stale faces).
 *
 * Do NOT call cancelSearch here: callers immediately runSearch(), and an async
 * cancel would bump searchGeneration and abort that new text search.
 */
export function retargetReverseFaceToPerson(opts: {
  name: string;
  representativeFaceId: number | null | undefined;
}): boolean {
  const faceId = opts.representativeFaceId ?? null;
  const imageUrl = faceThumbnailUrl(faceId);
  if (!imageUrl) {
    clearReverseFaceSearch();
    return false;
  }
  const gen = bumpSearchGeneration();
  searchAbort = new AbortController();
  const signal = searchAbort.signal;
  const prevUrl = state.previewUrl;
  if (prevUrl) URL.revokeObjectURL(prevUrl);
  const syntheticLeader: LeadershipPerson = {
    name: opts.name,
    role: "",
    image_url: imageUrl,
  };
  patchReverseFaceSession({
    selectedLeader: syntheticLeader,
    file: null,
    previewUrl: null,
    searching: true,
    error: null,
    tagMessage: null,
    result: null,
  });
  let job!: Promise<void>;
  job = (async () => {
    try {
      await ensureAppSession();
      if (isStaleSearch(gen)) return;
      const result = await withNetworkRetry(
        () => apiClient.searchFaceByUrl(imageUrl, 20, signal, { silent: true }),
        { signal, isStale: () => isStaleSearch(gen) }
      );
      if (isStaleSearch(gen)) return;
      patchReverseFaceSession({ result, searching: false, error: null });
    } catch (e) {
      if (isStaleSearch(gen)) return;
      if (signal.aborted || isAbortError(e)) {
        patchReverseFaceSession({ searching: false, error: null });
        return;
      }
      patchReverseFaceSession({
        error: reverseFaceErrorMessage(e, "Person face search failed"),
        result: null,
        searching: false,
      });
    } finally {
      if (searchJob === job) searchJob = null;
      if (searchAbort?.signal === signal) searchAbort = null;
    }
  })();
  searchJob = job;
  return true;
}

export function runReverseFaceCrawl() {
  const urls = state.crawlUrls
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (!urls.length) return crawlJob;
  patchReverseFaceSession({ crawling: true, error: null });
  let job!: Promise<void>;
  job = (async () => {
    try {
      const crawlResult = await apiClient.crawlFaceUrls(urls);
      patchReverseFaceSession({ crawlResult, crawling: false });
    } catch (e) {
      patchReverseFaceSession({
        error: formatApiError(e, "Crawl failed"),
        crawlResult: null,
        crawling: false,
      });
    } finally {
      if (crawlJob === job) crawlJob = null;
    }
  })();
  crawlJob = job;
  return job;
}

export function isUnknownFaceMatch(match: {
  person_id: number | null;
  cluster_id?: number | null;
  cluster_status?: string | null;
}): boolean {
  const status = (match.cluster_status ?? "").toLowerCase();
  return match.person_id == null || status === "unknown";
}

/** Total matching Drive files across face-search results (not just match rows). */
export function totalMatchFileCount(matches: FaceSearchMatch[]): number {
  let sum = 0;
  for (const m of matches) {
    sum += m.file_count ?? m.appears_in?.length ?? 0;
  }
  if (sum === 0 && matches.length > 0) return matches.length;
  return sum;
}

/** Collect cluster/face ids for name-tag (prefer clusters; leftover faces only). */
export function collectUnknownNameTagIds(
  matches: Array<{
    face_id: number;
    person_id: number | null;
    cluster_id?: number | null;
    cluster_status?: string | null;
  }>
): { clusterIds: number[]; faceIds: number[] } {
  const clusterIds = Array.from(
    new Set(
      matches
        .filter((m) => m.cluster_id != null && isUnknownFaceMatch(m))
        .map((m) => m.cluster_id as number)
    )
  );
  const faceIds = matches
    .filter(
      (m) =>
        m.person_id == null &&
        (m.cluster_id == null || !clusterIds.includes(m.cluster_id))
    )
    .map((m) => m.face_id);
  return { clusterIds, faceIds };
}

export async function runReverseFaceNameTag(opts: {
  name: string;
  role?: string | null;
  clusterIds?: number[];
  faceIds?: number[];
}): Promise<boolean> {
  const name = (opts.name || "").trim();
  if (!name) {
    patchReverseFaceSession({ error: "Name cannot be empty" });
    return false;
  }

  let clusterIds: number[];
  let faceIds: number[];
  if (opts.clusterIds === undefined && opts.faceIds === undefined) {
    const collected = collectUnknownNameTagIds(state.result?.matches ?? []);
    clusterIds = collected.clusterIds;
    faceIds = collected.faceIds;
  } else {
    clusterIds = opts.clusterIds ?? [];
    faceIds = opts.faceIds ?? [];
  }

  if (!clusterIds.length && !faceIds.length) {
    patchReverseFaceSession({
      tagMessage: "Nothing to tag — matches already have person links.",
      confirmTagOpen: false,
    });
    return false;
  }

  patchReverseFaceSession({ tagging: true, error: null, confirmTagOpen: false, tagMessage: null });
  try {
    const res = await apiClient.leadershipNameTag({
      name,
      role: opts.role ?? null,
      cluster_ids: clusterIds,
      face_ids: faceIds,
    });
    const okCount = res.actions.filter((a) => a.ok).length;
    // Release the tagging spinner as soon as the write completes — refresh can follow.
    patchReverseFaceSession({
      tagging: false,
      tagMessage: `Tagged as “${res.person.name}” (person #${res.person.id}) · ${okCount} action(s) · ${res.person.file_count ?? res.person.occurrence_count} files`,
    });

    // Indexed People is revision-gated / 60s-polled — invalidate+refetch so the new
    // person shows without a full page reload (fire-and-forget; do not block spinner).
    // Dynamic import avoids reverse-face ↔ search-session ↔ cache-revisions cycles.
    void import("@/lib/cache-revisions")
      .then(({ forceRefreshPersonsCache }) => forceRefreshPersonsCache())
      .catch(() => {
        /* best-effort — People tab will catch up on next poll */
      });

    // Refresh matches in the background so name-tag does not feel blocked on reverse search.
    // Tie the refresh AbortSignal to searchAbort so bumpSearchGeneration cancels cleanly.
    const refreshGen = searchGeneration;
    const leaderUrl = state.selectedLeader?.image_url;
    const upload = state.file;
    const isStaleRefresh = () => refreshGen !== searchGeneration;
    void (async () => {
      const refreshAbort = new AbortController();
      searchAbort = refreshAbort;
      const signal = refreshAbort.signal;
      try {
        if (leaderUrl) {
          const refreshed = await withNetworkRetry(
            () => apiClient.searchFaceByUrl(leaderUrl, 20, signal, { silent: true }),
            { signal, isStale: isStaleRefresh }
          );
          if (!isStaleRefresh()) patchReverseFaceSession({ result: refreshed });
        } else if (upload) {
          const refreshed = await withNetworkRetry(
            () => apiClient.searchUploadedFace(upload, 20, signal),
            { signal, isStale: isStaleRefresh }
          );
          if (!isStaleRefresh()) patchReverseFaceSession({ result: refreshed });
        }
      } catch {
        /* keep tagged message; refresh is best-effort (incl. abort) */
      } finally {
        if (searchAbort?.signal === signal) searchAbort = null;
      }
    })();
    return true;
  } catch (e) {
    patchReverseFaceSession({ error: formatApiError(e, "Name-tag failed") });
    return false;
  } finally {
    patchReverseFaceSession({ tagging: false });
  }
}
