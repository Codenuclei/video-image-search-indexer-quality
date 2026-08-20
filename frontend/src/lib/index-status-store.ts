"use client";

import { useSyncExternalStore } from "react";
import { apiClient, type IndexStatus } from "@/lib/api";
import { indexStatusRevision } from "@/lib/fingerprints";
import { readCache, writeCache, subscribeCacheKey, withInFlight, hydrateKeyFromDisk } from "@/lib/data-cache";

const KEY = "indexStatus";
const POLL_MS = 12000;

type StoreState = {
  status: IndexStatus | null;
  error: boolean;
  failStreak: number;
};

let failStreak = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
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

function subscribe(listener: () => void) {
  listeners.add(listener);
  subscriberCount += 1;
  ensurePolling();
  return () => {
    listeners.delete(listener);
    subscriberCount = Math.max(0, subscriberCount - 1);
    if (subscriberCount === 0 && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
      started = false;
    }
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
    return true;
  } catch {
    failStreak += 1;
    emit();
    return false;
  }
}

function ensurePolling() {
  if (typeof window === "undefined" || started) return;
  started = true;
  hydrateKeyFromDisk(KEY);
  void pollIndexStatus();
  pollTimer = setInterval(() => void pollIndexStatus(), POLL_MS);
}

/** Subscribe to shared index status (one network poller for the app). */
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
