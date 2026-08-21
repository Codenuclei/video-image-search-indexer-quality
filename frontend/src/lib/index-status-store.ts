"use client";

import { useSyncExternalStore } from "react";
import { API_BASE, apiClient, type IndexStatus } from "@/lib/api";
import { indexStatusRevision } from "@/lib/fingerprints";
import { readCache, writeCache, subscribeCacheKey, withInFlight, hydrateKeyFromDisk } from "@/lib/data-cache";

const KEY = "indexStatus";
/** Fallback only while indexing is busy and SSE is quiet — not a steady poll. */
const BUSY_FALLBACK_MS = 30000;

type StoreState = {
  status: IndexStatus | null;
  error: boolean;
  failStreak: number;
};

let failStreak = 0;
let busyFallbackTimer: ReturnType<typeof setInterval> | null = null;
let eventSource: EventSource | null = null;
let subscriberCount = 0;
let started = false;

const listeners = new Set<() => void>();

/** Must be referentially stable when values unchanged (React useSyncExternalStore). */
const EMPTY_SNAPSHOT: StoreState = { status: null, error: false, failStreak: 0 };
let cachedSnapshot: StoreState = EMPTY_SNAPSHOT;

function emit() {
  listeners.forEach((fn) => fn());
}

function getSnapshot(): StoreState {
  const entry = readCache<IndexStatus>(KEY);
  const status = entry?.data ?? null;
  const error = failStreak >= 3;
  if (
    cachedSnapshot.status === status &&
    cachedSnapshot.error === error &&
    cachedSnapshot.failStreak === failStreak
  ) {
    return cachedSnapshot;
  }
  cachedSnapshot = { status, error, failStreak };
  return cachedSnapshot;
}

function getServerSnapshot(): StoreState {
  return EMPTY_SNAPSHOT;
}

function isBusy(status: IndexStatus | null): boolean {
  if (!status) return false;
  if (status.is_running) return true;
  if ((status.pending_count ?? 0) > 0) return true;
  const processing = status.counts_by_status?.processing ?? 0;
  return processing > 0;
}

function syncBusyFallback() {
  const status = readCache<IndexStatus>(KEY)?.data ?? null;
  if (isBusy(status)) {
    if (!busyFallbackTimer) {
      busyFallbackTimer = setInterval(() => void pollIndexStatus(), BUSY_FALLBACK_MS);
    }
  } else if (busyFallbackTimer) {
    clearInterval(busyFallbackTimer);
    busyFallbackTimer = null;
  }
}

function ensureSSE() {
  if (typeof window === "undefined" || eventSource) return;
  try {
    const es = new EventSource(`${API_BASE}/index/events`);
    eventSource = es;
    es.addEventListener("revision", () => {
      void pollIndexStatus();
    });
    es.onmessage = () => {
      void pollIndexStatus();
    };
    es.onerror = () => {
      /* EventSource reconnects using server `retry:` */
    };
  } catch {
    /* ignore — busy fallback still covers active runs */
  }
}

function stopLive() {
  if (subscriberCount > 0) return;
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (busyFallbackTimer) {
    clearInterval(busyFallbackTimer);
    busyFallbackTimer = null;
  }
  started = false;
}

function ensureLive() {
  if (typeof window === "undefined" || started) return;
  started = true;
  hydrateKeyFromDisk(KEY);
  emit();
  void pollIndexStatus().then(() => syncBusyFallback());
  ensureSSE();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  subscriberCount += 1;
  ensureLive();
  return () => {
    listeners.delete(listener);
    subscriberCount = Math.max(0, subscriberCount - 1);
    stopLive();
  };
}

export async function pollIndexStatus(): Promise<boolean> {
  try {
    await withInFlight(KEY, async () => {
      const status = await apiClient.indexStatus();
      const rev = status.revision ?? indexStatusRevision(status);
      writeCache(KEY, status, rev, true);
    });
    failStreak = 0;
    emit();
    syncBusyFallback();
    return true;
  } catch {
    failStreak += 1;
    emit();
    return false;
  }
}

/** Subscribe to shared index status (SSE push + cache; no always-on 12s poll). */
export function useIndexStatusStore(): StoreState & { refresh: () => Promise<boolean> } {
  useSyncExternalStore(
    (cb) => subscribeCacheKey(KEY, cb),
    () => readCache<IndexStatus>(KEY),
    () => null
  );
  const state = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return { ...state, refresh: pollIndexStatus };
}

export function getCachedIndexStatus(): IndexStatus | null {
  return readCache<IndexStatus>(KEY)?.data ?? null;
}
