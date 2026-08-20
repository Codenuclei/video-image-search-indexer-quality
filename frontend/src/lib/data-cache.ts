"use client";

import { localGetJson, localRemove, localRemovePrefix, localSetJson } from "@/lib/local-store";

export const CACHE_PREFIX = "dfi:cache:v1:";

export type CacheEntry<T> = {
  data: T;
  revision: string;
  updatedAt: number;
};

type MemorySlot = {
  entry: CacheEntry<unknown>;
  inFlight: Promise<unknown> | null;
};

const memory = new Map<string, MemorySlot>();
const listeners = new Map<string, Set<() => void>>();

function storageKey(key: string): string {
  return `${CACHE_PREFIX}${key}`;
}

function emit(key: string) {
  listeners.get(key)?.forEach((fn) => fn());
}

export function subscribeCacheKey(key: string, listener: () => void): () => void {
  let set = listeners.get(key);
  if (!set) {
    set = new Set();
    listeners.set(key, set);
  }
  set.add(listener);
  return () => {
    set!.delete(listener);
    if (set!.size === 0) listeners.delete(key);
  };
}

export function readCache<T>(key: string): CacheEntry<T> | null {
  const slot = memory.get(key);
  if (slot) return slot.entry as CacheEntry<T>;
  return null;
}

/** Load localStorage into memory (call from client effects / subscribe, not during SSR render). */
export function hydrateKeyFromDisk(key: string): CacheEntry<unknown> | null {
  if (typeof window === "undefined") return null;
  if (memory.has(key)) return memory.get(key)!.entry;
  const fromDisk = localGetJson<CacheEntry<unknown>>(storageKey(key));
  if (fromDisk?.data !== undefined && fromDisk.revision != null) {
    memory.set(key, { entry: fromDisk, inFlight: null });
    emit(key);
    return fromDisk;
  }
  return null;
}

export function writeCache<T>(key: string, data: T, revision: string, persist = true): void {
  const entry: CacheEntry<T> = { data, revision, updatedAt: Date.now() };
  const prev = memory.get(key);
  memory.set(key, { entry: entry as CacheEntry<unknown>, inFlight: prev?.inFlight ?? null });
  if (persist) {
    const ok = localSetJson(storageKey(key), entry);
    if (!ok) {
      // Oversized — keep memory only; drop stale disk copy if any.
      localRemove(storageKey(key));
    }
  }
  emit(key);
}

export function invalidateCache(key: string): void {
  memory.delete(key);
  localRemove(storageKey(key));
  emit(key);
}

export function clearAllDataCache(): void {
  memory.clear();
  localRemovePrefix(CACHE_PREFIX);
  listeners.forEach((set) => set.forEach((fn) => fn()));
}

/** Dedupe concurrent fetches for the same key. */
export async function withInFlight<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const slot = memory.get(key);
  if (slot?.inFlight) return slot.inFlight as Promise<T>;
  const job = fn().finally(() => {
    const cur = memory.get(key);
    if (cur) cur.inFlight = null;
  });
  if (slot) slot.inFlight = job;
  else memory.set(key, { entry: { data: null, revision: "", updatedAt: 0 }, inFlight: job });
  return job;
}

/** Multi-tab: when another tab writes our prefix, refresh memory from disk. */
export function startCacheStorageSync(): () => void {
  if (typeof window === "undefined") return () => {};
  function onStorage(e: StorageEvent) {
    if (!e.key?.startsWith(CACHE_PREFIX)) return;
    const key = e.key.slice(CACHE_PREFIX.length);
    if (e.newValue == null) {
      memory.delete(key);
      emit(key);
      return;
    }
    try {
      const entry = JSON.parse(e.newValue) as CacheEntry<unknown>;
      memory.set(key, { entry, inFlight: memory.get(key)?.inFlight ?? null });
      emit(key);
    } catch {
      /* ignore */
    }
  }
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}
