"use client";

export type TranscriptStatus = {
  ok: boolean;
  status: "ready" | "running" | "missing" | "failed" | "not_found" | string;
  drive_file_id?: string;
  name?: string;
  cue_count?: number;
  has_captions?: boolean;
  phase?: string | null;
  message?: string;
};

export type EnsureEnglishResult = {
  ok: boolean;
  drive_file_id: string;
  media_id: number;
  cue_count: number;
  translated: boolean;
  already_english: boolean;
  language: string;
  source: string;
  message: string;
  llm_provider?: string | null;
  deleted_non_english?: number;
};

function extractApiDetail(raw: string, fallback: string): string {
  const text = (raw || "").trim();
  if (!text) return fallback;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
    if (typeof parsed.message === "string" && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    // plain text body
  }
  // Avoid dumping raw HTML / stack traces into the UI.
  if (text.startsWith("<") || text.length > 400) return fallback;
  return text;
}

export async function ensureVideoTranscript(
  apiBase: string,
  driveFileId: string,
  opts?: { force?: boolean }
): Promise<TranscriptStatus> {
  const res = await fetch(`${apiBase}/search/carousel/videos/ensure-transcript`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      drive_file_id: driveFileId,
      force: Boolean(opts?.force),
    }),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      extractApiDetail(text, "We couldn’t get transcripts from this video. Please try again.")
    );
  }
  return res.json();
}

export async function pollVideoTranscriptStatus(
  apiBase: string,
  driveFileId: string
): Promise<TranscriptStatus> {
  const res = await fetch(
    `${apiBase}/search/carousel/videos/${encodeURIComponent(driveFileId)}/transcript-status`,
    { cache: "no-store" }
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      extractApiDetail(text, "We couldn’t check transcript progress. Please try again.")
    );
  }
  return res.json();
}

/** Translate / ensure the stored transcript is English (complete sentences). */
export async function ensureEnglishTranscript(
  apiBase: string,
  driveFileId: string,
  opts?: { force?: boolean; provider?: string; model?: string }
): Promise<EnsureEnglishResult> {
  const res = await fetch(`${apiBase}/transcripts/ensure-english`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      drive_file_id: driveFileId,
      force: Boolean(opts?.force),
      provider: opts?.provider || "auto",
      model: opts?.model || null,
    }),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      extractApiDetail(
        text,
        "We couldn’t prepare an English transcript for this video. Please try again."
      )
    );
  }
  return res.json();
}

/** Start Whisper backfill (if needed) and poll until ready/failed. */
export async function waitForVideoTranscript(
  apiBase: string,
  driveFileId: string,
  opts?: {
    force?: boolean;
    intervalMs?: number;
    onUpdate?: (status: TranscriptStatus) => void;
    signal?: AbortSignal;
  }
): Promise<TranscriptStatus> {
  const intervalMs = opts?.intervalMs ?? 2500;
  let status = await ensureVideoTranscript(apiBase, driveFileId, {
    force: opts?.force,
  });
  opts?.onUpdate?.(status);

  while (
    status.status === "running" ||
    status.status === "missing"
  ) {
    if (opts?.signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    await new Promise((r) => setTimeout(r, intervalMs));
    if (opts?.signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    status = await pollVideoTranscriptStatus(apiBase, driveFileId);
    opts?.onUpdate?.(status);
  }
  return status;
}

/**
 * Ensure captions exist, then ensure the stored transcript is English.
 * Progress callbacks use friendly UI copy for the modal.
 */
export async function waitForEnglishTranscript(
  apiBase: string,
  driveFileId: string,
  opts?: {
    force?: boolean;
    intervalMs?: number;
    onUpdate?: (status: TranscriptStatus) => void;
    signal?: AbortSignal;
  }
): Promise<{ status: TranscriptStatus; english: EnsureEnglishResult | null }> {
  const status = await waitForVideoTranscript(apiBase, driveFileId, opts);
  if (status.status === "failed" || status.status === "not_found") {
    return { status, english: null };
  }

  opts?.onUpdate?.({
    ...status,
    status: "running",
    phase: "english",
    message: "Preparing an English transcript…",
  });

  try {
    const english = await ensureEnglishTranscript(apiBase, driveFileId, {
      force: opts?.force,
    });
    const ready: TranscriptStatus = {
      ...status,
      status: "ready",
      cue_count: english.cue_count ?? status.cue_count,
      has_captions: true,
      phase: "english_ready",
      message:
        english.message ||
        `English transcript ready (${english.cue_count} sentences).`,
    };
    opts?.onUpdate?.(ready);
    return { status: ready, english };
  } catch (e) {
    const msg =
      e instanceof Error
        ? e.message
        : "We couldn’t prepare an English transcript for this video. Please try again.";
    const failed: TranscriptStatus = {
      ...status,
      status: "failed",
      phase: "english_failed",
      message: msg,
    };
    opts?.onUpdate?.(failed);
    return { status: failed, english: null };
  }
}
