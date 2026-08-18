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
  apiClient.persons().then((persons) => patchSearchSession({ persons })).catch(() => {});
  apiClient.reidLinkedinMap().then((linkedinMap) => patchSearchSession({ linkedinMap })).catch(() => {});
  apiClient
    .folderContexts()
    .then((folderContexts) => patchSearchSession({ folderContexts }))
    .catch(() => {});
  patchSearchSession({ catalogsHydrated: true });
}

export function hydrateSearchSettings() {
  if (state.settingsHydrated) return;
  apiClient
    .settings()
    .then((s) => {
      patchSearchSession({
        rerank: s.search_rerank_enabled,
        useCaptions: s.search_use_captions,
        settingsHydrated: true,
      });
    })
    .catch(() => patchSearchSession({ settingsHydrated: true }));
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
  patchSearchSession({
    loading: true,
    mime,
    previewFile: null,
    previewMoment: null,
    lastSearchMode: { captions: current.useCaptions, rerank: current.rerank },
  });
  const params = new URLSearchParams({ q });
  if (current.person) params.set("person", current.person);
  if (mime !== "all") params.set("mime", mime);
  if (current.folderPath) params.set("folder_path", current.folderPath);
  if (!current.rerank) params.set("rerank", "false");
  if (current.useCaptions) params.set("captions", "true");
  let job!: Promise<void>;
  job = (async () => {
    try {
      const res = await fetch(`${API_BASE}/search?${params}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      const results = (await res.json()) as SearchResponse;
      patchSearchSession({ results, loading: false });
    } catch (e) {
      toastApiError(formatApiError(e, "Search failed"));
      patchSearchSession({ results: null, loading: false });
    } finally {
      if (inFlight === job) inFlight = null;
    }
  })();
  inFlight = job;
  return job;
}
