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

_MAX_VARIANTS = 3


@lru_cache(maxsize=512)
def expand_queries_sync(query: str) -> tuple[str, ...]:
    """Return the original query plus up to _MAX_VARIANTS visual paraphrases."""
    q = query.strip()
    if not q:
        return ()

    from app.config import get_settings

    settings = get_settings()
    if not settings.gemini_api_key:
        return (q,)

    prompt = (
        "Rewrite this visual search query into short, concrete descriptions of what "
        "the scene would LOOK like on camera. Keep each under 8 words. Cover literal "
        "and closely-related interpretations.\n\n"
        f'Query: "{q}"\n\n'
        f"Return ONLY a JSON array of {_MAX_VARIANTS} strings, no extra text."
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
        return (q,)

    seen = {q.lower()}
    ordered = [q]
    for v in variants:
        if v.lower() not in seen:
            seen.add(v.lower())
            ordered.append(v)
        if len(ordered) >= _MAX_VARIANTS + 1:
            break
    return tuple(ordered)
