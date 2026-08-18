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
  patchReverseFaceSession({ rosterLoading: true, error: null });
  try {
    const roster = await apiClient.leadershipRoster("executive");
    patchReverseFaceSession({ roster, rosterLoading: false, rosterHydrated: true });
  } catch (e) {
    patchReverseFaceSession({
      error: formatApiError(e, "Failed to load Executive Leaders"),
      roster: null,
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
