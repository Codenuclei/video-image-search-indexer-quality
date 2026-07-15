"""
app/gemini/video_embeddings.py
==============================
Gemini Embedding 2 — frame-level video embedding.

Embeds JPEG frames (RETRIEVAL_DOCUMENT) and text queries (RETRIEVAL_QUERY)
into a shared 3072-dim vector space.  Both functions are synchronous and
intended to be called via asyncio.to_thread() from async FastAPI handlers.
"""
from __future__ import annotations

import base64
import logging
import time
from functools import lru_cache
from pathlib import Path

from app.gemini.rate_limit import gemini_embed_slot

logger = logging.getLogger(__name__)

_DIM   = 3072
_MODEL = "gemini-embedding-2"


@lru_cache(maxsize=1)
def _get_client():
    from google import genai
    from app.config import get_settings
    return genai.Client(api_key=get_settings().gemini_api_key)


def _embed_with_retry(contents, task_type: str) -> list[float]:
    from google.genai.types import EmbedContentConfig
    client = _get_client()
    for attempt in range(8):
        try:
            with gemini_embed_slot():
                result = client.models.embed_content(
                    model=_MODEL,
                    contents=contents,
                    config=EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=_DIM,
                    ),
                )
            return result.embeddings[0].values
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                wait = 5 * (2 ** attempt)
                logger.warning("Gemini embed transient error (attempt %d) — retrying in %ds: %s", attempt + 1, wait, msg[:120])
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Gemini embed failed after 8 retries")


def embed_frame_sync(jpeg_path: str) -> list[float]:
    """
    Embed a single JPEG frame as a 3072-dim Gemini vector.
    Call via asyncio.to_thread() from async code.
    """
    data = Path(jpeg_path).read_bytes()
    b64  = base64.b64encode(data).decode()
    return _embed_with_retry(
        contents={"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": b64}}]},
        task_type="RETRIEVAL_DOCUMENT",
    )


def embed_text_sync(text: str) -> list[float]:
    """
    Embed a text search query as a 3072-dim Gemini vector.
    Call via asyncio.to_thread() from async code.
    """
    return _embed_with_retry(contents=text, task_type="RETRIEVAL_QUERY")
