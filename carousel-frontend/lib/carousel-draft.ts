/**
 * Pure helpers for studio draft autosave serialize/deserialize + conflict checks.
 */

export type StudioDraftPhase = 1 | 2 | 3 | 4 | 5;

export type StudioDraftState = {
  drive_file_id: string;
  phase: StudioDraftPhase;
  selected_themes: unknown[];
  selected_topics: unknown[];
  selected_hooks: unknown[];
  intent: string | null;
  intent_score: number | null;
  themes: unknown[];
  topic_tree: unknown[];
  hooks: unknown[];
  topics: unknown[];
  carousels: unknown[];
  slides: unknown[];
  layouts: Record<string, unknown> | null;
  layout_mode: string;
  images_ready: boolean;
  base_save_id?: number | null;
  client_updated_at?: string | null;
  label?: string | null;
};

export type StudioDraftUpsertBody = StudioDraftState;

export type StudioDraftRecord = {
  id: number;
  drive_file_id: string;
  kind?: string;
  label?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  source?: string | null;
  status?: string;
  layout_mode?: string;
  copy_version?: number;
  payload: Partial<StudioDraftState> & Record<string, unknown>;
};

export type CarouselProjectFinished = {
  id: number;
  label?: string | null;
  copy_version?: number;
  layout_mode?: string;
  preview_url?: string | null;
  slide_count?: number | null;
  created_at?: string | null;
  source?: string | null;
};

export type CarouselProjectCard = {
  drive_file_id: string;
  video_name?: string | null;
  video_path?: string | null;
  has_draft: boolean;
  draft_phase?: number | null;
  draft_updated_at?: string | null;
  draft_id?: number | null;
  finished_count: number;
  latest_finished: CarouselProjectFinished | null;
  last_activity_at?: string | null;
};

export type CarouselProjectsResponse = {
  items: CarouselProjectCard[];
  limit: number;
  offset: number;
  has_more: boolean;
};

/** Parse `draft=1` (or true/yes) from a query string value. */
export function parseDraftParam(raw: string | null | undefined): boolean {
  if (raw == null || raw === "") return false;
  const v = String(raw).trim().toLowerCase();
  return v === "1" || v === "true" || v === "yes";
}

function asPhase(value: unknown, fallback: StudioDraftPhase = 1): StudioDraftPhase {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  if (!Number.isFinite(n)) return fallback;
  const i = Math.trunc(n);
  if (i >= 1 && i <= 5) return i as StudioDraftPhase;
  return fallback;
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asStringOrNull(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") return value;
  return String(value);
}

function asNumberOrNull(value: unknown): number | null {
  if (value == null || value === "") return null;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Build a wire body from live studio state (never throws). */
export function serializeStudioDraft(
  state: Partial<StudioDraftState> & { drive_file_id: string }
): StudioDraftUpsertBody {
  const drive_file_id = String(state.drive_file_id || "").trim();
  return {
    drive_file_id,
    phase: asPhase(state.phase, 1),
    selected_themes: asList(state.selected_themes),
    selected_topics: asList(state.selected_topics),
    selected_hooks: asList(state.selected_hooks),
    intent: asStringOrNull(state.intent),
    intent_score: asNumberOrNull(state.intent_score),
    themes: asList(state.themes),
    topic_tree: asList(state.topic_tree),
    hooks: asList(state.hooks),
    topics: asList(state.topics),
    carousels: asList(state.carousels),
    slides: asList(state.slides),
    layouts:
      state.layouts && typeof state.layouts === "object" && !Array.isArray(state.layouts)
        ? (state.layouts as Record<string, unknown>)
        : null,
    layout_mode:
      state.layout_mode === "split_2" || state.layout_mode === "single_1"
        ? state.layout_mode
        : "single_1",
    images_ready: Boolean(state.images_ready),
    base_save_id:
      typeof state.base_save_id === "number" && Number.isFinite(state.base_save_id)
        ? state.base_save_id
        : null,
    client_updated_at:
      typeof state.client_updated_at === "string" && state.client_updated_at.trim()
        ? state.client_updated_at.trim()
        : new Date().toISOString(),
    label:
      typeof state.label === "string" && state.label.trim() ? state.label.trim() : "Draft",
  };
}

/**
 * Normalize a server draft payload into studio-safe state.
 * Malformed payloads never throw; missing fields get safe defaults.
 */
export function deserializeStudioDraft(
  record: StudioDraftRecord | null | undefined
): StudioDraftState | null {
  if (!record || typeof record !== "object") return null;
  const drive_file_id = String(record.drive_file_id || "").trim();
  if (!drive_file_id) return null;
  const payload =
    record.payload && typeof record.payload === "object" && !Array.isArray(record.payload)
      ? record.payload
      : {};
  try {
    return serializeStudioDraft({
      drive_file_id,
      phase: asPhase(payload.phase ?? 1),
      selected_themes: payload.selected_themes,
      selected_topics: payload.selected_topics,
      selected_hooks: payload.selected_hooks,
      intent: payload.intent as string | null,
      intent_score: payload.intent_score as number | null,
      themes: payload.themes,
      topic_tree: payload.topic_tree,
      hooks: payload.hooks,
      topics: payload.topics,
      carousels: payload.carousels,
      slides: payload.slides,
      layouts: (payload.layouts as Record<string, unknown> | null) ?? null,
      layout_mode: String(payload.layout_mode || record.layout_mode || "single_1"),
      images_ready: Boolean(payload.images_ready),
      base_save_id: (payload.base_save_id as number | null) ?? null,
      client_updated_at:
        (payload.client_updated_at as string | null) ??
        record.updated_at ??
        record.created_at ??
        null,
      label: record.label ?? (payload.label as string | null) ?? "Draft",
    });
  } catch {
    return serializeStudioDraft({ drive_file_id, phase: 1 });
  }
}

function parseTimeMs(iso: string | null | undefined): number | null {
  if (!iso || typeof iso !== "string") return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

/**
 * True when the local client_updated_at is older than the server draft updated_at.
 * Equal or missing timestamps are not considered stale (allow overwrite).
 */
export function isDraftStale(
  clientUpdatedAt: string | null | undefined,
  serverUpdatedAt: string | null | undefined
): boolean {
  const clientMs = parseTimeMs(clientUpdatedAt);
  const serverMs = parseTimeMs(serverUpdatedAt);
  if (clientMs == null || serverMs == null) return false;
  return clientMs < serverMs;
}
