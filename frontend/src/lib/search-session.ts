"use client";

import { useSyncExternalStore } from "react";
import {
  API_BASE,
  apiClient,
  formatApiError,
  type FolderContext,
  type LibraryResponse,
  type Person,
  type SearchMoment,
  type SearchResponse,
  type SearchResultFile,
} from "@/lib/api";
import { toastApiError } from "@/lib/toast-api-error";
import { readCache, writeCache, hydrateKeyFromDisk } from "@/lib/data-cache";
import { personsRevision, stableJsonHash } from "@/lib/fingerprints";
import {
  indexedFolderPickerOptions,
  type LibraryFolderOption,
} from "@/lib/library-folders";

const SEARCH_RESULTS_CACHE_KEY = "searchResults";
const LIBRARY_SHELL_CACHE_KEY = "driveLibraryShell";
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
  /** Indexed Folders paths for Search folder filter (excludes virtual "/"). */
  libraryFolders: LibraryFolderOption[];
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
  libraryFolders: [],
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
let searchAbort: AbortController | null = null;
let activeSearchId: string | null = null;

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
  hydrateKeyFromDisk(LIBRARY_SHELL_CACHE_KEY);
  hydrateKeyFromDisk("reidLinkedinMap");

  const cachedPersons = readCache<Person[]>("persons");
  if (cachedPersons?.data?.length) {
    patchSearchSession({ persons: cachedPersons.data });
  }
  const cachedFolders = readCache<FolderContext[]>("folderContexts");
  if (cachedFolders?.data?.length) {
    patchSearchSession({ folderContexts: cachedFolders.data });
  }
  const cachedShell = readCache<LibraryResponse>(LIBRARY_SHELL_CACHE_KEY);
  if (cachedShell?.data?.tree) {
    patchSearchSession({
      libraryFolders: indexedFolderPickerOptions(cachedShell.data.tree),
    });
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

  void (async () => {
    try {
      const rev = await apiClient.driveLibraryRevision().catch(() => null);
      if (cachedShell && rev && cachedShell.revision === rev.revision) {
        return;
      }
      const shell = await apiClient.driveLibraryShell();
      writeCache(
        LIBRARY_SHELL_CACHE_KEY,
        shell,
        rev?.revision ?? shell.revision ?? String(shell.summary?.total_files ?? Date.now()),
        true
      );
      patchSearchSession({
        libraryFolders: indexedFolderPickerOptions(shell.tree),
      });
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

/** Clear displayed results and query state (does not touch cached responses). */
export function resetSearchResults() {
  cancelSearch({ clearQuery: true, clearResults: true });
}

function notifyBackendCancel(searchId: string | null) {
  if (!searchId) return;
  void fetch(`${API_BASE}/search/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ search_id: searchId }),
    cache: "no-store",
    keepalive: true,
  }).catch(() => {
    /* best-effort */
  });
}

function abortInFlightHttp() {
  const cancelledId = activeSearchId;
  if (searchAbort) {
    searchAbort.abort();
    searchAbort = null;
  }
  activeSearchId = null;
  notifyBackendCancel(cancelledId);
}

/** Abort in-flight search and optionally clear results. Notifies the backend. */
export function cancelSearch(options?: {
  clearQuery?: boolean;
  clearResults?: boolean;
}) {
  const clearQuery = options?.clearQuery ?? false;
  const clearResults = options?.clearResults ?? false;
  searchGeneration += 1;
  inFlight = null;
  abortInFlightHttp();
  patchSearchSession({
    ...(clearQuery ? { q: "" } : {}),
    ...(clearResults
      ? {
          results: null,
          lastSearchMode: null,
          previewFile: null,
          previewMoment: null,
        }
      : {}),
    loading: false,
  });
}

function newSearchId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `s-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function isAbortError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const name = "name" in error ? String((error as { name?: unknown }).name) : "";
  return name === "AbortError";
}

export function runSearch() {
  const current = getSearchSession();
  const q = current.q.trim();
  if (!q) return inFlight;

  // Drop any prior request without clearing the query / cached preview results.
  abortInFlightHttp();

  const mime = current.mime === "video" ? "all" : current.mime;
  const searchId = newSearchId();
  activeSearchId = searchId;
  searchAbort = new AbortController();
  const signal = searchAbort.signal;
  const params = new URLSearchParams({ q, search_id: searchId });
  if (current.person) params.set("person", current.person);
  if (mime !== "all") params.set("mime", mime);
  if (current.folderPath) params.set("folder_path", current.folderPath);
  if (!current.rerank) params.set("rerank", "false");
  if (current.useCaptions) params.set("captions", "true");

  // Cache key must ignore ephemeral search_id.
  const cacheParams = new URLSearchParams({ q });
  if (current.person) cacheParams.set("person", current.person);
  if (mime !== "all") cacheParams.set("mime", mime);
  if (current.folderPath) cacheParams.set("folder_path", current.folderPath);
  if (!current.rerank) cacheParams.set("rerank", "false");
  if (current.useCaptions) cacheParams.set("captions", "true");
  const stableQueryKey = stableJsonHash(cacheParams.toString());
  hydrateKeyFromDisk(SEARCH_RESULTS_CACHE_KEY);
  const cachedSearches =
    readCache<SearchResultsCache>(SEARCH_RESULTS_CACHE_KEY)?.data ?? {};
  const cached = cachedSearches[stableQueryKey]?.result ?? null;
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
      const res = await fetch(`${API_BASE}/search?${params}`, {
        cache: "no-store",
        signal,
      });
      if (!res.ok) throw new Error(await res.text());
      const results = (await res.json()) as SearchResponse;
      const previous = readCache<SearchResultsCache>(SEARCH_RESULTS_CACHE_KEY)?.data ?? {};
      const entries = Object.entries({
        ...previous,
        [stableQueryKey]: { result: results, updatedAt: Date.now() },
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
      if (signal.aborted || isAbortError(e)) {
        patchSearchSession({ loading: false });
        return;
      }
      if (cached) {
        patchSearchSession({ loading: false });
        return;
      }
      toastApiError(formatApiError(e, "Search failed"));
      patchSearchSession({ results: null, loading: false });
    } finally {
      if (inFlight === job) inFlight = null;
      if (searchAbort?.signal === signal) searchAbort = null;
      if (activeSearchId === searchId) activeSearchId = null;
    }
  })();
  inFlight = job;
  return job;
}
