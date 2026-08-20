"use client";

import { useSyncExternalStore } from "react";
import {
  API_BASE,
  apiClient,
  formatApiError,
  type FolderContext,
  type Person,
  type SearchMoment,
  type SearchResponse,
  type SearchResultFile,
} from "@/lib/api";
import { toastApiError } from "@/lib/toast-api-error";
import { readCache, writeCache, hydrateKeyFromDisk } from "@/lib/data-cache";
import { personsRevision, stableJsonHash } from "@/lib/fingerprints";

const SEARCH_RESULTS_CACHE_KEY = "searchResults";
const MAX_CACHED_SEARCHES = 6;

type CachedSearchResult = {
  result: SearchResponse;
  updatedAt: number;
};

type SearchResultsCache = Record<string, CachedSearchResult>;

export type SearchSession = {
  q: string;
  person: string;
  mime: string;
  folderPath: string;
  rerank: boolean;
  useCaptions: boolean;
  settingsHydrated: boolean;
  catalogsHydrated: boolean;
  persons: Person[];
  folderContexts: FolderContext[];
  linkedinMap: Record<string, string>;
  results: SearchResponse | null;
  lastSearchMode: { captions: boolean; rerank: boolean } | null;
  loading: boolean;
  previewFile: SearchResultFile | null;
  previewMoment: SearchMoment | null;
};

const initial: SearchSession = {
  q: "",
  person: "",
  mime: "all",
  folderPath: "",
  rerank: true,
  useCaptions: false,
  settingsHydrated: false,
  catalogsHydrated: false,
  persons: [],
  folderContexts: [],
  linkedinMap: {},
  results: null,
  lastSearchMode: null,
  loading: false,
  previewFile: null,
  previewMoment: null,
};

let state: SearchSession = initial;
const listeners = new Set<() => void>();
let inFlight: Promise<void> | null = null;
let searchGeneration = 0;

function emit() {
  listeners.forEach((fn) => fn());
}

export function getSearchSession(): SearchSession {
  return state;
}

export function patchSearchSession(patch: Partial<SearchSession>) {
  state = { ...state, ...patch };
  emit();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSearchSession(): SearchSession {
  return useSyncExternalStore(subscribe, getSearchSession, getSearchSession);
}

export function hydrateSearchCatalogs() {
  if (state.catalogsHydrated) return;
  patchSearchSession({ catalogsHydrated: true });

  hydrateKeyFromDisk("persons");
  hydrateKeyFromDisk("folderContexts");
  hydrateKeyFromDisk("reidLinkedinMap");

  const cachedPersons = readCache<Person[]>("persons");
  if (cachedPersons?.data?.length) {
    patchSearchSession({ persons: cachedPersons.data });
  }
  const cachedFolders = readCache<FolderContext[]>("folderContexts");
  if (cachedFolders?.data?.length) {
    patchSearchSession({ folderContexts: cachedFolders.data });
  }
  const cachedLi = readCache<Record<string, string>>("reidLinkedinMap");
  if (cachedLi?.data) {
    patchSearchSession({ linkedinMap: cachedLi.data });
  }

  void (async () => {
    try {
      const rev = await apiClient.personsRevision().catch(() => null);
      if (!cachedPersons || !rev || cachedPersons.revision !== rev.revision) {
        const persons = await apiClient.persons();
        writeCache("persons", persons, rev?.revision ?? personsRevision(persons), true);
        patchSearchSession({ persons });
      }
    } catch {
      /* ignore */
    }
  })();

  apiClient.reidLinkedinMap().then((linkedinMap) => {
    writeCache("reidLinkedinMap", linkedinMap, String(Object.keys(linkedinMap).length), true);
    patchSearchSession({ linkedinMap });
  }).catch(() => {});

  void (async () => {
    try {
      const rev = await apiClient.folderContextsRevision().catch(() => null);
      if (!cachedFolders || !rev || cachedFolders.revision !== rev.revision) {
        const folderContexts = await apiClient.folderContexts();
        writeCache(
          "folderContexts",
          folderContexts,
          rev?.revision ?? String(folderContexts.length),
          true
        );
        patchSearchSession({ folderContexts });
      }
    } catch {
      /* ignore */
    }
  })();
}

export function hydrateSearchSettings() {
  if (state.settingsHydrated) return;
  hydrateKeyFromDisk("settings");
  const cached = readCache<{ search_rerank_enabled: boolean; search_use_captions: boolean }>("settings");
  if (cached?.data) {
    patchSearchSession({
      rerank: cached.data.search_rerank_enabled,
      useCaptions: cached.data.search_use_captions,
      settingsHydrated: true,
    });
  }
  void (async () => {
    try {
      const rev = await apiClient.settingsRevision().catch(() => null);
      if (cached && rev && cached.revision === rev.revision) {
        patchSearchSession({ settingsHydrated: true });
        return;
      }
      const s = await apiClient.settings();
      writeCache("settings", s, rev?.revision ?? String(Date.now()), true);
      patchSearchSession({
        rerank: s.search_rerank_enabled,
        useCaptions: s.search_use_captions,
        settingsHydrated: true,
      });
    } catch {
      patchSearchSession({ settingsHydrated: true });
    }
  })();
}

export async function persistSearchRerank(value: boolean) {
  patchSearchSession({ rerank: value });
  try {
    await apiClient.updateSettings({ search_rerank_enabled: value });
  } catch {
    /* keep local toggle */
  }
}

export async function persistSearchCaptions(value: boolean) {
  patchSearchSession({ useCaptions: value });
  try {
    await apiClient.updateSettings({ search_use_captions: value });
  } catch {
    /* keep local toggle */
  }
}

export function runSearch() {
  const current = getSearchSession();
  const q = current.q.trim();
  if (!q) return inFlight;
  const mime = current.mime === "video" ? "all" : current.mime;
  const params = new URLSearchParams({ q });
  if (current.person) params.set("person", current.person);
  if (mime !== "all") params.set("mime", mime);
  if (current.folderPath) params.set("folder_path", current.folderPath);
  if (!current.rerank) params.set("rerank", "false");
  if (current.useCaptions) params.set("captions", "true");
  const queryKey = stableJsonHash(params.toString());
  hydrateKeyFromDisk(SEARCH_RESULTS_CACHE_KEY);
  const cachedSearches =
    readCache<SearchResultsCache>(SEARCH_RESULTS_CACHE_KEY)?.data ?? {};
  const cached = cachedSearches[queryKey]?.result ?? null;
  const generation = ++searchGeneration;
  patchSearchSession({
    results: cached,
    loading: !cached,
    mime,
    previewFile: null,
    previewMoment: null,
    lastSearchMode: { captions: current.useCaptions, rerank: current.rerank },
  });
  let job!: Promise<void>;
  job = (async () => {
    try {
      const res = await fetch(`${API_BASE}/search?${params}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      const results = (await res.json()) as SearchResponse;
      const previous = readCache<SearchResultsCache>(SEARCH_RESULTS_CACHE_KEY)?.data ?? {};
      const entries = Object.entries({
        ...previous,
        [queryKey]: { result: results, updatedAt: Date.now() },
      })
        .sort(([, a], [, b]) => b.updatedAt - a.updatedAt)
        .slice(0, MAX_CACHED_SEARCHES);
      writeCache(
        SEARCH_RESULTS_CACHE_KEY,
        Object.fromEntries(entries),
        String(Date.now()),
        true
      );
      if (generation !== searchGeneration) return;
      patchSearchSession({ results, loading: false });
    } catch (e) {
      if (generation !== searchGeneration) return;
      if (cached) {
        patchSearchSession({ loading: false });
        return;
      }
      toastApiError(formatApiError(e, "Search failed"));
      patchSearchSession({ results: null, loading: false });
    } finally {
      if (inFlight === job) inFlight = null;
    }
  })();
  inFlight = job;
  return job;
}
