/** Human-readable labels for indexer skip-reason keys from /index/skip-stats. */
const SKIP_REASON_META: Record<
  string,
  { label: string; hint: string; retryable: boolean; retryLabel: string }
> = {
  indexing_paused: {
    label: "Indexing paused",
    hint: "Parent folder was stopped from indexing",
    retryable: true,
    retryLabel: "Resume & retry all",
  },
  folder_marker: {
    label: "Folder entries",
    hint: "Drive folders tracked as markers, not media",
    retryable: false,
    retryLabel: "Can't retry",
  },
  unsupported_mime: {
    label: "Unsupported type",
    hint: "File type is not indexed (docs, archives, etc.)",
    retryable: false,
    retryLabel: "Can't retry",
  },
  decode_exhausted: {
    label: "Decode failed",
    hint: "Image/video could not be decoded after retries",
    retryable: true,
    retryLabel: "Retry all",
  },
  corrupt_file: {
    label: "Corrupt file",
    hint: "File appears damaged or unreadable",
    retryable: true,
    retryLabel: "Retry all",
  },
  duplicate_content: {
    label: "Duplicate content",
    hint: "Identical bytes already indexed (cross-folder/user dedupe)",
    retryable: false,
    retryLabel: "Can't retry",
  },
  name_conflict: {
    label: "Same name conflict",
    hint: "Filename collides with different content — use Replace/Skip on dashboard",
    retryable: false,
    retryLabel: "Can't retry",
  },
  unknown: {
    label: "Other",
    hint: "Skipped for an unclassified reason",
    retryable: true,
    retryLabel: "Retry all",
  },
};

export function skipReasonMeta(reason: string): {
  label: string;
  hint: string;
  retryable: boolean;
  retryLabel: string;
} {
  const key = (reason || "unknown").trim();
  return (
    SKIP_REASON_META[key] ?? {
      label: key.replace(/_/g, " "),
      hint: "Skipped during indexing",
      retryable: true,
      retryLabel: "Retry all",
    }
  );
}

export type FriendlyIndexError = {
  /** Short one-line message for the card. */
  summary: string;
  /** Full original text when it differs from summary (for Details). */
  details: string | null;
  /** Coarse category for styling/icons. */
  kind: "db" | "network" | "decode" | "missing" | "other";
};

const TECHNICAL_MARKERS = [
  "sqlalchemy",
  "asyncpg",
  "traceback",
  "infailedsqltransaction",
  "integrityerror",
  "operationalerror",
  "psycopg",
  "greenlet",
  "psycopg2",
  "exception:",
  "typeerror",
  "referenceerror",
  "syntaxerror",
  "httpexception",
  "starlette",
  "uvicorn",
  "fastapi",
  "pydantic",
];

function looksTechnical(raw: string): boolean {
  const lower = raw.toLowerCase();
  if (!raw.trim()) return false;
  if (raw.startsWith("{") || raw.startsWith("[")) return true;
  if (raw.startsWith("<") || /<html/i.test(raw)) return true;
  if (raw.length > 220) return true;
  if (raw.includes("\n") && raw.length > 80) return true;
  if (TECHNICAL_MARKERS.some((m) => lower.includes(m))) return true;
  if (/file ["'].*["'], line \d+/i.test(raw)) return true;
  if (/traceback \(most recent call last\)/i.test(raw)) return true;
  if (/localhost:\d+|127\.0\.0\.1|\[::1\]/i.test(raw)) return true;
  if (/:\d{4,5}\/?/.test(raw) && /http|econn|refused|connect/i.test(raw)) return true;
  if (/internal server error|status code 50\d/i.test(raw)) return true;
  if (/\b(?:select|insert|update|delete)\b.+\bfrom\b/i.test(raw)) return true;
  if (/format ['"][^'"]+['"] not found/i.test(raw)) return true;
  if (/unknown (?:format|strftime|token)/i.test(raw)) return true;
  if (/invalid format (?:string|code|specifier)/i.test(raw)) return true;
  if (/\b(?:value|key|attribute|name|index|runtime|assertion)error\b/i.test(lower)) return true;
  return false;
}

function unwrapApiPayload(raw: string): string {
  const text = raw.trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return text;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return unwrapApiPayload(parsed.detail.trim());
    }
    if (Array.isArray(parsed.detail)) {
      const first = parsed.detail[0] as { msg?: unknown } | undefined;
      if (typeof first?.msg === "string" && first.msg.trim()) return first.msg.trim();
      return "";
    }
    if (typeof parsed.message === "string" && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    return text;
  }
  return text;
}

/**
 * Never returns stack traces, SQLAlchemy, FastAPI JSON, or HTML.
 * Known indexer failures become a short explanation; everything else falls back.
 */
export function sanitizeUserFacingError(
  raw: string | null | undefined,
  fallback = "Something went wrong. Please try again."
): string {
  const unwrapped = unwrapApiPayload((raw ?? "").trim());
  if (!unwrapped) return fallback;
  if (looksTechnical(unwrapped)) {
    const mapped = humanizeIndexError(unwrapped);
    if (mapped.kind === "db" || mapped.kind === "network" || mapped.details) {
      return mapped.summary;
    }
    return fallback;
  }
  if (unwrapped.length > 180) return fallback;
  return unwrapped;
}

/**
 * Map raw indexer / SQLAlchemy error_message strings to a short friendly summary.
 * Preserves the original as details when truncated or rewritten.
 */
export function humanizeIndexError(raw: string | null | undefined): FriendlyIndexError {
  const text = (raw ?? "").trim();
  if (!text) {
    return { summary: "Indexing failed", details: null, kind: "other" };
  }

  const lower = text.toLowerCase();

  if (
    lower.includes("infailedsqltransaction") ||
    lower.includes("current transaction is aborted") ||
    lower.includes("transaction is aborted")
  ) {
    return {
      summary: "Database transaction aborted during face clustering. Retry this file.",
      details: text,
      kind: "db",
    };
  }

  if (lower.includes("deadlock detected") || lower.includes("deadlockdetected")) {
    return {
      summary: "Database deadlock while updating face clusters. Retry this file.",
      details: text,
      kind: "db",
    };
  }

  if (
    lower.includes("uniqueviolation") ||
    lower.includes("unique constraint") ||
    lower.includes("duplicate key")
  ) {
    return {
      summary: "Database conflict while saving face data. Retry this file.",
      details: text,
      kind: "db",
    };
  }

  if (
    lower.includes("connection refused") ||
    lower.includes("connection reset") ||
    lower.includes("timeout") ||
    lower.includes("timed out") ||
    lower.includes("temporarily unavailable")
  ) {
    return {
      summary: "Temporary network or service timeout. Retry this file.",
      details: looksTechnical(text) ? text : null,
      kind: "network",
    };
  }

  if (lower.startsWith("decode_exhausted") || lower.includes("decode failed")) {
    return {
      summary: "Could not decode this media file.",
      details: text.length > 80 ? text : null,
      kind: "decode",
    };
  }

  if (
    lower.includes("drive_not_connected") ||
    lower.includes("no drive folder selected") ||
    lower.includes("drive is not connected")
  ) {
    return {
      summary: "Connect Google Drive and choose a folder, then retry.",
      details: text.length > 80 ? text : null,
      kind: "missing",
    };
  }

  if (lower.includes("404") || lower.includes("not found") || lower.includes("file not found")) {
    return {
      summary: "File no longer found on Drive.",
      details: text.length > 80 ? text : null,
      kind: "missing",
    };
  }

  if (
    lower.includes("not a bot") ||
    lower.includes("no cookies configured") ||
    lower.includes("invalid cookies") ||
    (lower.includes("youtube") && lower.includes("cookies") && lower.includes("netscape"))
  ) {
    return {
      summary: "YouTube blocked this download. Please retry later, or ask an admin to refresh login cookies.",
      details: text,
      kind: "other",
    };
  }

  if (lower.startsWith("unsupported mime")) {
    return {
      summary: "Unsupported file type for indexing.",
      details: text.length > 80 ? text : null,
      kind: "other",
    };
  }

  if (looksTechnical(text) || TECHNICAL_MARKERS.some((m) => lower.includes(m))) {
    return {
      summary: "Indexing failed. Please retry this file.",
      details: text,
      kind: "other",
    };
  }

  if (text.length > 160) {
    return {
      summary: "Indexing failed. Please retry this file.",
      details: text,
      kind: "other",
    };
  }

  return { summary: text, details: null, kind: "other" };
}

export function formatCount(n: number): string {
  return n.toLocaleString();
}
