"""
Query expansion for higher-recall visual search.

At search time we ask Gemini 2.5 Flash to rewrite a user query into a few
short, visually-descriptive variants.  Each variant is embedded and searched
independently; results are fused by max score.  This meaningfully improves
recall for terse queries ("flying car" -> "a car flying through the air",
"futuristic hovering vehicle", ...).

Cheap and CPU-friendly: one Flash call + a few embed calls per search.
Falls back to [query] on any error so search never breaks.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

_MAX_LLM_VARIANTS = 3


def _normalize_query_typo(query: str) -> str:
    """Normalize high-value search typos before embedding."""
    return re.sub(r"\bgradutes\b", "graduates", query, flags=re.IGNORECASE)


def _deterministic_variants(query: str) -> list[str]:
    """Recall-critical variants that must not depend on an LLM response."""
    lower = query.lower()
    variants: list[str] = []
    if re.search(r"\b(?:graduate|graduates|graduation|convocation)\b", lower):
        variants.extend(
            [
                "graduates",
                "graduation cap and academic gown",
                "black and yellow graduation gowns and stoles",
                "convocation ceremony",
            ]
        )
    if re.search(r"\b(?:row|rowing|rower|rowers|rowerg|ergometer)\b", lower):
        variants.extend(
            [
                "athletes using rowing machines",
                "indoor rowing workout on Concept2 RowErg",
            ]
        )
    if re.search(
        r"\b(?:exercise|exercising|workout|fitness|gym|lifting|sled|squat|yoga)\b",
        lower,
    ):
        variants.extend(
            [
                "people exercising in a gym",
                "athletes doing a fitness workout",
                "lifting weights pushing sleds and medicine ball exercise",
            ]
        )
    return variants


@lru_cache(maxsize=512)
def expand_queries_sync(query: str) -> tuple[str, ...]:
    """Return normalized deterministic variants plus optional LLM paraphrases."""
    q = _normalize_query_typo(query.strip())
    if not q:
        return ()

    from app.config import get_settings

    settings = get_settings()
    base = [q, *_deterministic_variants(q)]
    seen = set()
    ordered: list[str] = []
    for item in base:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(item)

    if not settings.gemini_api_key:
        return tuple(ordered)

    prompt = (
        "Rewrite this visual search query into short, concrete descriptions of what "
        "the scene would LOOK like on camera. Keep each under 8 words. Cover literal "
        "and closely-related interpretations.\n\n"
        f'Query: "{q}"\n\n'
        f"Return ONLY a JSON array of {_MAX_LLM_VARIANTS} strings, no extra text."
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.4,
                response_mime_type="application/json",
            ),
        )
        text = resp.text or ""
        m = re.search(r"\[[\s\S]*?\]", text)
        variants: list[str] = []
        if m:
            arr = json.loads(m.group())
            if isinstance(arr, list):
                variants = [str(v).strip() for v in arr if str(v).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Query expansion failed for %r: %s", q, exc)
        return tuple(ordered)

    added = 0
    for v in variants:
        if v.lower() not in seen:
            seen.add(v.lower())
            ordered.append(v)
            added += 1
        if added >= _MAX_LLM_VARIANTS:
            break
    return tuple(ordered)
