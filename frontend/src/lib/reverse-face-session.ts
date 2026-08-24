"use client";

import { useSyncExternalStore } from "react";
import {
  apiClient,
  formatApiError,
  type FaceCrawlResponse,
  type FaceSearchResponse,
  type LeadershipPerson,
  type LeadershipRoster,
} from "@/lib/api";
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

export async function hydrateLeadershipRoster(force = false) {
  if (state.rosterHydrated && !force) return;
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
    const roster = await apiClient.leadershipRoster("executive");
    writeCache(
      LEADERSHIP_ROSTER_CACHE_KEY,
      roster,
      stableJsonHash(roster),
      true
    );
    patchReverseFaceSession({ roster, rosterLoading: false, rosterHydrated: true });
  } catch (e) {
    patchReverseFaceSession({
      error: formatApiError(e, "Failed to load Executive Leaders"),
      roster: cachedRoster ?? state.roster,
      rosterLoading: false,
      rosterHydrated: true,
    });
  }
}

export function setReverseFaceFile(next: File | null) {
  const prevUrl = state.previewUrl;
  if (prevUrl) URL.revokeObjectURL(prevUrl);
  patchReverseFaceSession({
    file: next,
    result: null,
    error: null,
    tagMessage: null,
    selectedLeader: null,
    previewUrl: next ? URL.createObjectURL(next) : null,
  });
}

/** Clear current face-search results and upload (dustbin). */
export function clearReverseFaceSearch() {
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
  patchReverseFaceSession({
    searching: true,
    error: null,
    tagMessage: null,
    selectedLeader: null,
  });
  let job!: Promise<void>;
  job = (async () => {
    try {
      const result = await apiClient.searchUploadedFace(target, 20);
      patchReverseFaceSession({ result, searching: false });
    } catch (e) {
      patchReverseFaceSession({
        error: formatApiError(e, "Face search failed"),
        result: null,
        searching: false,
      });
    } finally {
      if (searchJob === job) searchJob = null;
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
      const result = await apiClient.searchFaceByUrl(person.image_url, 20);
      patchReverseFaceSession({ result, searching: false });
    } catch (e) {
      patchReverseFaceSession({
        error: formatApiError(e, "Leader face search failed"),
        result: null,
        searching: false,
      });
    } finally {
      if (searchJob === job) searchJob = null;
    }
  })();
  searchJob = job;
  return job;
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
    patchReverseFaceSession({
      tagMessage: `Tagged as “${res.person.name}” (person #${res.person.id}) · ${okCount} action(s) · ${res.person.occurrence_count} appearances`,
    });

    // Refresh matches so named people show up immediately.
    if (state.selectedLeader?.image_url) {
      const refreshed = await apiClient.searchFaceByUrl(state.selectedLeader.image_url, 20);
      patchReverseFaceSession({ result: refreshed });
    } else if (state.file) {
      const refreshed = await apiClient.searchUploadedFace(state.file, 20);
      patchReverseFaceSession({ result: refreshed });
    }
    return true;
  } catch (e) {
    patchReverseFaceSession({ error: formatApiError(e, "Name-tag failed") });
    return false;
  } finally {
    patchReverseFaceSession({ tagging: false });
  }
}
