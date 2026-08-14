"use client";

import { useEffect, useState } from "react";
import {
  testApi,
  type CarouselLlmModelOption,
  type CarouselLlmProviderOption,
  type CarouselRunConfig,
} from "@/lib/test-api";
import {
  ARENA_CREATIVE_WRITING_TOP10,
  ARENA_CW_PROVIDERS,
  resolveArenaCreativeWritingCatalog,
} from "./arena-creative-writing-top10";

/** Default = Arena Creative Writing #1. */
export const DEFAULT_CAROUSEL_RUN_CONFIG: CarouselRunConfig = {
  provider: "claude",
  model: "claude-fable-5",
};

export const FALLBACK_PROVIDERS: CarouselLlmProviderOption[] = ARENA_CW_PROVIDERS;

export const FALLBACK_MODELS: CarouselLlmModelOption[] = ARENA_CREATIVE_WRITING_TOP10.map(
  (row) => ({
    id: row.id,
    label: row.label,
    provider: row.provider,
  })
);

let cachedModels: CarouselLlmModelOption[] | null = null;
let cachedProviders: CarouselLlmProviderOption[] | null = null;
let loadPromise: Promise<void> | null = null;

async function ensureLlmCatalog(): Promise<void> {
  if (cachedModels && cachedProviders) return;
  if (!loadPromise) {
    loadPromise = testApi
      .getCarouselLlmModels()
      .then((res) => {
        const live = res.models?.length
          ? res.models
          : res.carousel_llm_model_options?.length
            ? res.carousel_llm_model_options
            : [];
        cachedModels = resolveArenaCreativeWritingCatalog(live);
        cachedProviders = ARENA_CW_PROVIDERS;
      })
      .catch(() => {
        cachedModels = FALLBACK_MODELS;
        cachedProviders = FALLBACK_PROVIDERS;
      })
      .finally(() => {
        loadPromise = null;
      });
  }
  await loadPromise;
}

export function useCarouselLlmCatalog() {
  const [models, setModels] = useState<CarouselLlmModelOption[]>(
    cachedModels ?? FALLBACK_MODELS
  );
  const [providers, setProviders] = useState<CarouselLlmProviderOption[]>(
    cachedProviders ?? FALLBACK_PROVIDERS
  );

  useEffect(() => {
    let cancelled = false;
    void ensureLlmCatalog().then(() => {
      if (cancelled) return;
      if (cachedModels) setModels(cachedModels);
      if (cachedProviders) setProviders(cachedProviders);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return { models, providers };
}

export function defaultModelForProvider(
  provider: CarouselRunConfig["provider"],
  models: { id: string; provider: string }[]
): string {
  const hit = models.find((m) => m.provider === provider);
  if (hit) return hit.id;
  if (provider === "openrouter" || provider === "auto") {
    return "qwen/qwen3.8-max";
  }
  if (provider === "gemini") return "gemini-3.7-flash";
  return DEFAULT_CAROUSEL_RUN_CONFIG.model;
}
