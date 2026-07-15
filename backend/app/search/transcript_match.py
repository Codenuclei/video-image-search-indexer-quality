from __future__ import annotations

import re


def _query_words(query: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"\w+", query) if len(w) >= 3]
    if words:
        return words
    stripped = query.strip().lower()
    return [stripped] if stripped else []


def score_transcript_match(text: str, query: str) -> tuple[float, str] | None:
    """
    Regex / phrase match on transcript text. No embeddings or LLM.

    Returns (score, match_kind) or None if no confident match.
    """
    hay = (text or "").strip()
    q = (query or "").strip()
    if not hay or not q:
        return None

    hay_lower = hay.lower()
    q_lower = q.lower()

    # Exact phrase (substring) — highest confidence
    if len(q_lower) >= 3 and q_lower in hay_lower:
        return 1.0, "phrase"

    words = _query_words(q)
    if not words:
        return None

    matched = 0
    for word in words:
        if re.search(rf"\b{re.escape(word)}\b", hay_lower):
            matched += 1

    if matched == 0:
        return None
    if matched == len(words):
        return 0.92, "all_words"

    ratio = matched / len(words)
    if ratio >= 0.5:
        return 0.45 + ratio * 0.4, "partial_words"
    return None
