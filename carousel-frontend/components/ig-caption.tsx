/**
 * Instagram-style caption with sparse bright-yellow keyword highlights.
 * Shared by /test studio (and safe to reuse from production IgPost later).
 */

import type { ReactNode } from "react";

export type HighlightSpec = {
  highlight?: number[] | null;
  highlight_words?: string[] | null;
};

function normalizeWords(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

function resolveHighlightIndices(
  text: string,
  spec?: HighlightSpec | null
): Set<number> {
  const words = normalizeWords(text);
  const out = new Set<number>();
  const indices = spec?.highlight;
  if (Array.isArray(indices)) {
    for (const raw of indices) {
      const i = Number(raw);
      if (Number.isInteger(i) && i >= 0 && i < words.length) out.add(i);
    }
  }
  if (!out.size && Array.isArray(spec?.highlight_words)) {
    const lowered = words.map((w) => w.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/gi, ""));
    for (const rw of spec!.highlight_words!) {
      const token = String(rw || "")
        .toLowerCase()
        .replace(/^[^a-z0-9']+|[^a-z0-9']+$/gi, "");
      if (!token) continue;
      const idx = lowered.findIndex((w, i) => w === token && !out.has(i));
      if (idx >= 0) out.add(idx);
    }
  }
  return out;
}

/** Render white caption text with selected words in bright Instagram yellow. */
export function IgHighlightedCaption({
  text,
  highlight,
  highlight_words,
  className,
}: {
  text: string;
  highlight?: number[] | null;
  highlight_words?: string[] | null;
  className?: string;
}) {
  const cleaned = (text || "").trim();
  if (!cleaned) return null;
  const marked = resolveHighlightIndices(cleaned, { highlight, highlight_words });
  const tokens = cleaned.split(/(\s+)/);
  let wordIndex = 0;
  const nodes: ReactNode[] = [];
  for (let t = 0; t < tokens.length; t++) {
    const tok = tokens[t];
    if (/^\s+$/.test(tok)) {
      nodes.push(tok);
      continue;
    }
    const i = wordIndex++;
    if (marked.has(i)) {
      nodes.push(
        <span key={`hl-${i}-${t}`} className="ig-hl">
          {tok}
        </span>
      );
    } else {
      nodes.push(<span key={`w-${i}-${t}`}>{tok}</span>);
    }
  }
  return <span className={className}>{nodes}</span>;
}
