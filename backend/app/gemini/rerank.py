"""
app/gemini/rerank.py
====================
Post-Qdrant Gemini re-ranking filter.

Sends candidate video frames (already retrieved by vector search) back to
Gemini 2.5 Flash as a single multimodal batch prompt.  Gemini decides which
frames genuinely match the query — removing irrelevant / noisy results.

If a person filter is active the named person's face thumbnail is prepended
to the prompt so Gemini knows who to look for.

Usage (async):
    kept = await rerank_moments(query, moments, person_name="Tony Stark",
                                 face_thumbnail_path="/data/thumbnails/face_3.jpg")
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from app.schemas import SearchMoment

logger = logging.getLogger(__name__)

_BATCH = 20          # max frames per Gemini call
_MAX_JPEG_BYTES = 512 * 1024  # skip frames larger than 512 KB (shouldn't happen)
_ALWAYS_KEEP_TOP = 5  # top-N Qdrant results always pass regardless of re-rank


def _thumbnail_path(thumbnail_dir: str, drive_file_id: str, timestamp: float) -> str:
    return str(Path(thumbnail_dir) / "video" / drive_file_id / f"{timestamp:.3f}.jpg")


def _load_jpeg(path: str) -> bytes | None:
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    if len(data) > _MAX_JPEG_BYTES:
        return None
    return data


def _build_prompt(
    query: str,
    n_frames: int,
    person_name: str | None,
    folder_context: str | None,
) -> str:
    lines = [
        "You are a video search result validator. Your job is to KEEP results unless they clearly do NOT belong.",
        "",
        f'Search query: "{query}"',
    ]
    if person_name:
        lines.append(f'Person filter: the user is looking for frames containing "{person_name}".')
    if folder_context:
        lines.append(f"Folder context (for disambiguation): {folder_context}")
    lines += [
        "",
        f"I am showing you {n_frames} video frame(s) below (Frame 1 … Frame {n_frames}).",
        "",
        "For EACH frame decide:",
        "  true  — keep this frame. It has ANY visual connection to the query:",
        "           • exact match (the thing is clearly visible)",
        "           • related/conceptual match (e.g. 'flying car' → any flying vehicle, futuristic craft, sci-fi flight scene)",
        "           • contextually plausible (scene type, setting, or action is related to what the user wants to find)",
        "  false — discard this frame ONLY IF it has absolutely NO connection to the query.",
        "           Examples of discard: a static document/title screen, a black frame, or a completely unrelated scene",
        "           with no overlap whatsoever with the query topic.",
        "",
        "Be LENIENT. When in doubt, return true.",
        "Do NOT be literal — interpret the query broadly and generously.",
        "",
        "Reply ONLY with a compact JSON array of booleans, one per frame, in order.",
        "No extra text, no markdown fences, no explanation.",
        f"Example: [true, true, false, true]",
    ]
    return "\n".join(lines)


def _parse_booleans(text: str, expected: int) -> list[bool] | None:
    """Extract the first JSON bool array from Gemini's response."""
    m = re.search(r"\[[\s\S]*?\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group())
        if isinstance(arr, list) and len(arr) == expected:
            return [bool(v) for v in arr]
    except json.JSONDecodeError:
        pass
    return None


def _rerank_batch_sync(
    query: str,
    moments: list[SearchMoment],
    frame_images: list[bytes | None],
    person_name: str | None,
    face_jpeg: bytes | None,
    folder_context: str | None,
    model: str,
    api_key: str,
) -> list[bool]:
    """Synchronous Gemini call — run via asyncio.to_thread."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    parts: list = []

    # Optional: prepend reference face image for person-filter context
    if person_name and face_jpeg:
        parts.append(types.Part(text=f'Reference face for "{person_name}":'))
        parts.append(types.Part.from_bytes(data=face_jpeg, mime_type="image/jpeg"))

    # Build frame-by-frame parts (skip None images but keep index alignment via placeholder)
    valid_count = 0
    frame_labels: list[str] = []
    for i, (moment, img) in enumerate(zip(moments, frame_images), start=1):
        label = f"Frame {i} ({moment.name} @ {moment.timestamp_sec:.1f}s):"
        frame_labels.append(label)
        parts.append(types.Part(text=label))
        if img:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
            valid_count += 1
        else:
            parts.append(types.Part(text="[frame image unavailable]"))

    # If no images at all, return all True (can't filter without content)
    if valid_count == 0:
        return [True] * len(moments)

    parts.insert(0, types.Part(text=_build_prompt(query, len(moments), person_name, folder_context)))

    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = response.text or ""
        result = _parse_booleans(text, len(moments))
        if result is not None:
            kept = sum(result)
            logger.info("Gemini re-rank: %d/%d frames kept for query %r", kept, len(moments), query)
            return result
        logger.warning("Gemini re-rank: could not parse response %r — keeping all", text[:200])
        return [True] * len(moments)
    except Exception as exc:
        logger.warning("Gemini re-rank failed (%s) — keeping all candidates", exc)
        return [True] * len(moments)


async def rerank_moments(
    query: str,
    moments: list[SearchMoment],
    *,
    person_name: str | None = None,
    face_thumbnail_path: str | None = None,
    folder_context: str | None = None,
    thumbnail_dir: str = "./data/thumbnails",
) -> list[SearchMoment]:
    """
    Filter *moments* using Gemini multimodal re-ranking.

    The top _ALWAYS_KEEP_TOP moments (by Qdrant score) always pass through
    unconditionally — they represent the strongest vector matches and should
    not be discarded even if Gemini is uncertain.

    The remaining moments are sent to Gemini in batches of up to _BATCH
    frames.  Gemini's prompt is deliberately lenient: it only discards frames
    with zero visual connection to the query.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.gemini_api_key or not moments:
        return moments

    # Always keep top N — never send to Gemini for filtering
    guaranteed = moments[:_ALWAYS_KEEP_TOP]
    candidates = moments[_ALWAYS_KEEP_TOP:]

    if not candidates:
        return guaranteed

    face_jpeg: bytes | None = None
    if face_thumbnail_path:
        face_jpeg = _load_jpeg(face_thumbnail_path)

    kept: list[SearchMoment] = []

    for batch_start in range(0, len(candidates), _BATCH):
        batch = candidates[batch_start: batch_start + _BATCH]
        images = [
            _load_jpeg(_thumbnail_path(thumbnail_dir, m.drive_file_id, m.timestamp_sec))
            for m in batch
        ]
        mask = await asyncio.to_thread(
            _rerank_batch_sync,
            query, batch, images,
            person_name, face_jpeg, folder_context,
            settings.gemini_model, settings.gemini_api_key,
        )
        kept.extend(m for m, ok in zip(batch, mask) if ok)

    return guaranteed + kept
