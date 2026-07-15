"""Index-time image captioning via Gemini Flash.

Produces concise factual descriptions of images so we can embed the *text*
and search captions (query→caption text cosine is far better calibrated than
query→image). Supports batching multiple images into one Gemini call.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time

logger = logging.getLogger(__name__)

_DESCRIBE_INSTRUCTION = (
    "You are an image cataloguer. For EACH image below, write ONE concise, factual "
    "description (1-2 sentences) capturing: the main subjects, what they are doing, "
    "the setting/scene type, notable objects, and any clearly legible text/signage. "
    "Be literal and specific; do not speculate or add commentary. "
    "Reply with ONLY a JSON array of strings, one description per image, in order. "
    "No markdown, no extra keys."
)


def _downscale_jpeg(jpeg_bytes: bytes, max_dim: int) -> bytes:
    """Resize so the longest side <= max_dim; re-encode JPEG. Best-effort."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg_bytes))
        img = img.convert("RGB")
        w, h = img.size
        scale = max(w, h) / float(max_dim)
        if scale > 1.0:
            img = img.resize((max(1, int(w / scale)), max(1, int(h / scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Caption downscale failed, using original bytes: %s", exc)
        return jpeg_bytes


def _parse_string_array(text: str, expected: int) -> list[str] | None:
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list):
        return None
    out = [str(v).strip() for v in arr]
    if len(out) == expected:
        return out
    return None


def describe_images_batch_sync(images: list[bytes]) -> list[str]:
    """Describe a batch of JPEG images in a single Gemini call.

    Returns one caption per image (same order). On failure returns "" for the
    affected batch so indexing can continue (visual embedding still works).
    """
    from google import genai
    from google.genai import types

    from app.config import get_settings

    settings = get_settings()
    if not settings.gemini_api_key or not images:
        return ["" for _ in images]

    client = genai.Client(api_key=settings.gemini_api_key)
    small = [_downscale_jpeg(b, settings.image_caption_max_dim) for b in images]

    parts: list = [types.Part(text=_DESCRIBE_INSTRUCTION)]
    for i, b in enumerate(small, start=1):
        parts.append(types.Part(text=f"Image {i}:"))
        parts.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))

    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=settings.image_caption_model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            parsed = _parse_string_array(resp.text or "", len(images))
            if parsed is not None:
                return parsed
            logger.warning("Caption batch: unparseable/mismatched response — retrying")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if any(c in msg for c in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "500")):
                time.sleep(3 * (attempt + 1))
                continue
            logger.warning("Caption batch failed: %s", msg[:160])
            break
    return ["" for _ in images]


def describe_image_sync(jpeg_bytes: bytes) -> str:
    """Describe a single image (used by the live per-file pipeline)."""
    return describe_images_batch_sync([jpeg_bytes])[0]
