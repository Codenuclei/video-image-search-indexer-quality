/**
 * Instagram-style caption with sparse bright-yellow keyword highlights.
 * Shared by /test studio (and safe to reuse from production IgPost later).
 */

import type { ReactNode } from "react";
import { resolveHighlightIndices } from "@/lib/carousel-export";

export type HighlightSpec = {
  highlight?: number[] | null;
  highlight_words?: string[] | null;
};

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
  const marked = resolveHighlightIndices(cleaned, highlight, highlight_words);
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
