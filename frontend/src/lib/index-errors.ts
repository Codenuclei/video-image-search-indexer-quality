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
];

function firstMeaningfulLine(raw: string): string {
  for (const line of raw.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    if (t.startsWith("File ") || t.startsWith("Traceback")) continue;
    return t;
  }
  return raw.trim();
}

function isTechnicalWall(raw: string): boolean {
  const lower = raw.toLowerCase();
  if (raw.length > 280) return true;
  if (raw.includes("\n") && raw.length > 120) return true;
  return TECHNICAL_MARKERS.some((m) => lower.includes(m));
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
      details: isTechnicalWall(text) ? text : null,
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
      summary:
        "YouTube blocked the download (bot check). Set YTDLP_COOKIES_FILE or YTDLP_COOKIES on the backend.",
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

  if (isTechnicalWall(text) || TECHNICAL_MARKERS.some((m) => lower.includes(m))) {
    const head = firstMeaningfulLine(text);
    const short =
      head.length > 160
        ? `${head.slice(0, 157)}…`
        : head.length > 0
          ? head
          : "Database error during indexing.";
    // Prefer a generic DB line over dumping SQLAlchemy class names as the summary.
    const looksLikeSqlAlchemy =
      /sqlalchemy|asyncpg|psycopg|InFailedSQL|IntegrityError|OperationalError/i.test(head);
    return {
      summary: looksLikeSqlAlchemy
        ? "Database error during indexing. Retry this file."
        : short,
      details: text,
      kind: "db",
    };
  }

  if (text.length > 160) {
    return {
      summary: `${text.slice(0, 157)}…`,
      details: text,
      kind: "other",
    };
  }

  return { summary: text, details: null, kind: "other" };
}

export function formatCount(n: number): string {
  return n.toLocaleString();
}
