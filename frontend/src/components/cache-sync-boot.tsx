"use client";

import { useEffect } from "react";
import { startCacheRevisionPolling } from "@/lib/cache-revisions";
import { startCacheStorageSync } from "@/lib/data-cache";

/** Multi-tab localStorage sync + server revision polling for dfi:cache:v1:* */
export function CacheSyncBoot() {
  useEffect(() => {
    const stopStorage = startCacheStorageSync();
    const stopRevisions = startCacheRevisionPolling();
    return () => {
      stopStorage();
      stopRevisions();
    };
  }, []);
  return null;
}
