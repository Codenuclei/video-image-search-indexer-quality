"""
app/gemini/video_embeddings.py
==============================
Gemini Embedding 2 — frame-level video embedding.

Embeds JPEG frames (RETRIEVAL_DOCUMENT) and text queries (RETRIEVAL_QUERY)
into a shared 3072-dim vector space.  Both functions are synchronous and
intended to be called via asyncio.to_thread() from async FastAPI handlers.

Image indexing uses ``embed_content`` with a list of Contents (REST
``:batchEmbedContents``) for ~50 img/s at batch=5 × parallel≈20.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path

from app.gemini.rate_limit import gemini_embed_slot

logger = logging.getLogger(__name__)

_DIM = 3072
_MODEL = "gemini-embedding-2"
_thread_local = threading.local()


def _get_client():
    """Per-thread Client — a process-global google-genai client serializes callers."""
    from google import genai
    from app.config import get_settings

    client = getattr(_thread_local, "client", None)
    if client is None:
        client = genai.Client(api_key=get_settings().gemini_api_key)
        _thread_local.client = client
    return client


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
                wait = 5 * (2**attempt)
                logger.warning(
                    "Gemini embed transient error (attempt %d) — retrying in %ds: %s",
                    attempt + 1,
                    wait,
                    msg[:120],
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini embed failed after 8 retries")


def _downscale_jpeg_bytes(data: bytes, *, max_edge: int | None = None) -> bytes:
    """Optionally downscale longest edge (production ~1024)."""
    from app.config import get_settings

    edge = max_edge if max_edge is not None else get_settings().image_embed_max_edge
    if edge <= 0 or not data:
        return data
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if max(w, h) <= edge:
            return data
        scale = edge / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embed downscale failed: %s", exc)
        return data


def _jpeg_bytes_for_embed(jpeg_path: str | Path, *, max_edge: int | None = None) -> bytes:
    """Read JPEG bytes, optionally downscaling longest edge (production ~1024)."""
    data = Path(jpeg_path).read_bytes()
    return _downscale_jpeg_bytes(data, max_edge=max_edge)


def embed_frame_sync(jpeg_path: str) -> list[float]:
    """
    Embed a single JPEG frame as a 3072-dim Gemini vector.
    Call via asyncio.to_thread() from async code.
    """
    data = _jpeg_bytes_for_embed(jpeg_path)
    b64 = base64.b64encode(data).decode()
    return _embed_with_retry(
        contents={"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": b64}}]},
        task_type="RETRIEVAL_DOCUMENT",
    )


def embed_frames_batch_sync(jpeg_paths: list[str]) -> list[list[float]]:
    """Embed multiple JPEGs in one ``embed_content`` → batchEmbedContents call."""
    if not jpeg_paths:
        return []
    payloads = [_jpeg_bytes_for_embed(path) for path in jpeg_paths]
    return embed_frames_batch_bytes_sync(payloads)


def embed_frames_batch_bytes_sync(
    jpeg_payloads: list[bytes],
    *,
    downscale: bool = True,
) -> list[list[float]]:
    """Embed in-memory JPEGs via one batchEmbedContents call (no temp files).

    Pass ``downscale=False`` when callers already resized (critical on 1-vCPU hosts:
    parallel PIL inside the embed pool collapses ~50/s → ~5/s).
    """
    from google.genai import types
    from google.genai.types import EmbedContentConfig

    if not jpeg_payloads:
        return []

    contents = []
    for raw in jpeg_payloads:
        data = _downscale_jpeg_bytes(raw) if downscale else raw
        b64 = base64.b64encode(data).decode("ascii")
        contents.append(
            types.Content(
                parts=[
                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=b64))
                ]
            )
        )

    client = _get_client()
    for attempt in range(8):
        try:
            with gemini_embed_slot():
                result = client.models.embed_content(
                    model=_MODEL,
                    contents=contents,
                    config=EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                        output_dimensionality=_DIM,
                    ),
                )
            emb = list(result.embeddings or [])
            if len(emb) != len(jpeg_payloads):
                raise RuntimeError(
                    f"batch embed returned {len(emb)} vectors for {len(jpeg_payloads)} images"
                )
            return [list(e.values or []) for e in emb]
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if any(code in msg for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                wait = 5 * (2**attempt)
                logger.warning(
                    "Gemini batch embed transient error (attempt %d) — retrying in %ds: %s",
                    attempt + 1,
                    wait,
                    msg[:120],
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Gemini batch embed failed after 8 retries")


def embed_text_sync(text: str) -> list[float]:
    """
    Embed a text search query as a 3072-dim Gemini vector.
    Call via asyncio.to_thread() from async code.
    """
    return _embed_with_retry(contents=text, task_type="RETRIEVAL_QUERY")
