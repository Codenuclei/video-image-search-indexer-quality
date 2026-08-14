/**
 * Arena.ai Text Arena → Creative Writing top 10
 * Source: https://arena.ai/leaderboard/text/creative-writing (Aug 12, 2026)
 *
 * Arena "-high" is a voting/effort tag; we map to the closest callable API id.
 */

import type { CarouselLlmModelOption, CarouselLlmProviderOption } from "@/lib/test-api";

export type ArenaCreativeWritingEntry = CarouselLlmModelOption & {
  arena_rank: number;
  arena_id: string;
};

/** Exactly 10 rows, Arena rank order. */
export const ARENA_CREATIVE_WRITING_TOP10: ArenaCreativeWritingEntry[] = [
  {
    arena_rank: 1,
    arena_id: "claude-fable-5",
    id: "claude-fable-5",
    label: "#1 Claude Fable 5",
    provider: "claude",
  },
  {
    arena_rank: 2,
    arena_id: "claude-opus-4-6-high",
    id: "claude-opus-4-6",
    label: "#2 Claude Opus 4.6 (high)",
    provider: "claude",
  },
  {
    arena_rank: 3,
    arena_id: "gemini-3.7-flash-high",
    id: "gemini-3.7-flash",
    label: "#3 Gemini 3.7 Flash (high)",
    provider: "gemini",
  },
  {
    arena_rank: 4,
    arena_id: "claude-opus-4-7-high",
    id: "claude-opus-4-7",
    label: "#4 Claude Opus 4.7 (high)",
    provider: "claude",
  },
  {
    arena_rank: 5,
    arena_id: "gemini-3-pro",
    // Google API id used on Arena; falls back handled in catalog if absent.
    id: "gemini-3-pro-preview",
    label: "#5 Gemini 3 Pro",
    provider: "gemini",
  },
  {
    arena_rank: 6,
    arena_id: "gemini-3.1-pro-preview",
    id: "gemini-3.1-pro-preview",
    label: "#6 Gemini 3.1 Pro Preview",
    provider: "gemini",
  },
  {
    arena_rank: 7,
    arena_id: "claude-opus-4-7",
    id: "claude-opus-4-7",
    label: "#7 Claude Opus 4.7",
    provider: "claude",
  },
  {
    arena_rank: 8,
    arena_id: "claude-opus-4-6",
    id: "claude-opus-4-6",
    label: "#8 Claude Opus 4.6",
    provider: "claude",
  },
  {
    arena_rank: 9,
    arena_id: "qwen3.8-max",
    id: "qwen/qwen3.8-max",
    label: "#9 Qwen3.8 Max",
    provider: "openrouter",
  },
  {
    arena_rank: 10,
    arena_id: "claude-opus-5-high",
    id: "claude-opus-5",
    label: "#10 Claude Opus 5 (high)",
    provider: "claude",
  },
];

export const ARENA_CW_PROVIDERS: CarouselLlmProviderOption[] = [
  { id: "claude", label: "Claude (direct)" },
  { id: "gemini", label: "Gemini" },
  { id: "openrouter", label: "OpenRouter" },
];

/** OpenRouter alternates when direct Anthropic/Gemini id is missing from live catalog. */
const OPENROUTER_FALLBACK: Record<string, string> = {
  "claude-fable-5": "anthropic/claude-fable-5",
  "claude-opus-4-6": "anthropic/claude-opus-4.6",
  "claude-opus-4-7": "anthropic/claude-opus-4.7",
  "claude-opus-5": "anthropic/claude-opus-5",
  "gemini-3.7-flash": "google/gemini-3.7-flash",
  "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
  "gemini-3-pro-preview": "google/gemini-3.1-pro-preview",
};

/**
 * Prefer curated Arena top-10. If a direct id is missing from the live catalog,
 * remap that row to its OpenRouter twin when available.
 */
export function resolveArenaCreativeWritingCatalog(
  liveModels: CarouselLlmModelOption[]
): CarouselLlmModelOption[] {
  const byKey = new Set(liveModels.map((m) => `${m.provider}:${m.id}`));
  const byId = new Set(liveModels.map((m) => m.id));

  return ARENA_CREATIVE_WRITING_TOP10.map((row) => {
    const key = `${row.provider}:${row.id}`;
    if (byKey.has(key) || byId.has(row.id) || liveModels.length === 0) {
      return {
        id: row.id,
        label: row.label,
        provider: row.provider,
      };
    }
    const orId = OPENROUTER_FALLBACK[row.id];
    if (orId && (byId.has(orId) || byKey.has(`openrouter:${orId}`))) {
      return {
        id: orId,
        label: `${row.label} · OpenRouter`,
        provider: "openrouter",
      };
    }
    // Still surface the curated row so the picker stays scoped to top-10.
    return {
      id: row.id,
      label: row.label,
      provider: row.provider,
    };
  });
}
