"use client";

import { useEffect } from "react";
import { startCacheStorageSync } from "@/lib/data-cache";

/** Multi-tab localStorage sync for dfi:cache:v1:* */
export function CacheSyncBoot() {
  useEffect(() => startCacheStorageSync(), []);
  return null;
}
