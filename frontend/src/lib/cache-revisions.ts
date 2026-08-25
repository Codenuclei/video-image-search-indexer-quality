"use client";

import { apiClient } from "@/lib/api";
import { invalidateCache, readCache, writeCache, hydrateKeyFromDisk } from "@/lib/data-cache";
import { personsRevision } from "@/lib/fingerprints";
import { indexedFolderPickerOptions } from "@/lib/library-folders";
import { patchSearchSession } from "@/lib/search-session";

const REVISION_POLL_MS = 60_000;

type CacheRevisionEntry = {
  key: string;
  fetchRevision: () => Promise<{ revision: string }>;
  refresh: () => Promise<void>;
};

const LIBRARY_SHELL_CACHE_KEY = "driveLibraryShell";

const CACHE_REVISION_ENTRIES: CacheRevisionEntry[] = [
  {
    key: "persons",
    fetchRevision: () => apiClient.personsRevision(),
    refresh: async () => {
      hydrateKeyFromDisk("persons");
      const rev = await apiClient.personsRevision().catch(() => null);
      const persons = await apiClient.persons();
      writeCache("persons", persons, rev?.revision ?? personsRevision(persons), true);
      patchSearchSession({ persons });
    },
  },
  {
    key: "folderContexts",
    fetchRevision: () => apiClient.folderContextsRevision(),
    refresh: async () => {
      hydrateKeyFromDisk("folderContexts");
      const rev = await apiClient.folderContextsRevision().catch(() => null);
      const folderContexts = await apiClient.folderContexts();
      writeCache(
        "folderContexts",
        folderContexts,
        rev?.revision ?? String(folderContexts.length),
        true
      );
      patchSearchSession({ folderContexts });
    },
  },
  {
    key: LIBRARY_SHELL_CACHE_KEY,
    fetchRevision: () => apiClient.driveLibraryRevision(),
    refresh: async () => {
      hydrateKeyFromDisk(LIBRARY_SHELL_CACHE_KEY);
      const rev = await apiClient.driveLibraryRevision().catch(() => null);
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
    },
  },
  {
    key: "settings",
    fetchRevision: () => apiClient.settingsRevision(),
    refresh: async () => {
      hydrateKeyFromDisk("settings");
      const rev = await apiClient.settingsRevision().catch(() => null);
      const s = await apiClient.settings();
      writeCache("settings", s, rev?.revision ?? String(Date.now()), true);
      patchSearchSession({
        rerank: s.search_rerank_enabled,
        useCaptions: s.search_use_captions,
        settingsHydrated: true,
      });
    },
  },
];

async function syncCacheRevision(entry: CacheRevisionEntry): Promise<void> {
  hydrateKeyFromDisk(entry.key);
  const cached = readCache<unknown>(entry.key);
  if (!cached) return;
  try {
    const rev = await entry.fetchRevision();
    if (rev.revision === cached.revision) return;
    invalidateCache(entry.key);
    await entry.refresh();
  } catch {
    /* ignore — stale cache is acceptable until next poll */
  }
}

export async function syncAllCacheRevisions(): Promise<void> {
  await Promise.all(CACHE_REVISION_ENTRIES.map((entry) => syncCacheRevision(entry)));
}

export function startCacheRevisionPolling(): () => void {
  if (typeof window === "undefined") return () => {};

  void syncAllCacheRevisions();

  const onVisibility = () => {
    if (document.visibilityState === "visible") {
      void syncAllCacheRevisions();
    }
  };
  document.addEventListener("visibilitychange", onVisibility);

  const interval = window.setInterval(() => {
    void syncAllCacheRevisions();
  }, REVISION_POLL_MS);

  return () => {
    window.clearInterval(interval);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
