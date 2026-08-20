"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  readCache,
  subscribeCacheKey,
  withInFlight,
  writeCache,
  hydrateKeyFromDisk,
  type CacheEntry,
} from "@/lib/data-cache";

export type CachedResourceOptions<T> = {
  key: string;
  fetcher: () => Promise<T>;
  /** Cheap freshness check; if equals cached revision, skip full fetch. */
  getRevision?: () => Promise<string | null>;
  /** Derive revision from full payload (used when writing cache). */
  revisionFromData?: (data: T) => string;
  pollMs?: number;
  persist?: boolean;
  enabled?: boolean;
};

export type CachedResource<T> = {
  data: T | null;
  revision: string | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  refresh: (force?: boolean) => Promise<void>;
};

function snapEntry<T>(key: string): CacheEntry<T> | null {
  return readCache<T>(key);
}

export function useCachedResource<T>(opts: CachedResourceOptions<T>): CachedResource<T> {
  const {
    key,
    fetcher,
    getRevision,
    revisionFromData,
    pollMs = 0,
    persist = true,
    enabled = true,
  } = opts;

  const entry = useSyncExternalStore(
    (cb) => subscribeCacheKey(key, cb),
    () => snapEntry<T>(key),
    () => null
  );

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  const getRevisionRef = useRef(getRevision);
  const revisionFromDataRef = useRef(revisionFromData);
  fetcherRef.current = fetcher;
  getRevisionRef.current = getRevision;
  revisionFromDataRef.current = revisionFromData;

  useEffect(() => {
    hydrateKeyFromDisk(key);
  }, [key]);

  const refresh = useCallback(
    async (force = false) => {
      if (!enabled) return;
      const cached = readCache<T>(key);
      if (cached) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        await withInFlight(key, async () => {
          const latest = readCache<T>(key);
          if (!force && latest && getRevisionRef.current) {
            try {
              const rev = await getRevisionRef.current();
              if (rev != null && rev === latest.revision) return;
            } catch {
              /* full fetch */
            }
          }
          const data = await fetcherRef.current();
          let finalRev =
            revisionFromDataRef.current?.(data) ??
            (getRevisionRef.current ? await getRevisionRef.current().catch(() => null) : null) ??
            latest?.revision ??
            String(Date.now());
          writeCache(key, data, finalRev, persist);
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [key, persist, enabled]
  );

  useEffect(() => {
    if (!enabled) return;
    void refresh(false);
  }, [enabled, key, refresh]);

  useEffect(() => {
    if (!enabled || !pollMs || pollMs <= 0) return;
    const t = setInterval(() => void refresh(false), pollMs);
    return () => clearInterval(t);
  }, [enabled, pollMs, refresh]);

  return {
    data: entry?.data ?? null,
    revision: entry?.revision ?? null,
    loading: loading && !entry,
    refreshing,
    error,
    refresh,
  };
}
