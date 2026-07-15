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

from app.schemas import SearchMoment, SearchResultFile

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
    *,
    strict: bool,
) -> str:
    lines = [
        "You are a video search result validator.",
        "",
        f'Search query: "{query}"',
    ]
    if person_name:
        lines.append(f'Person filter: results must show "{person_name}".')
    if folder_context:
        lines.append(f"Folder context: {folder_context}")
    lines += [
        "",
        f"I am showing you {n_frames} video frame(s) below (Frame 1 … Frame {n_frames}).",
        "",
        "For EACH frame decide:",
    ]
    if strict:
        lines += [
            "  true  — the frame CLEARLY shows what the query asks for.",
            "  false — the frame is unrelated, ambiguous, or only loosely similar",
            "           (e.g. query 'students cooking' but frame shows people eating, talking, or a lecture).",
            "Be STRICT. When uncertain, return false.",
        ]
    else:
        lines += [
            "  true  — keep if the frame has a clear connection to the query.",
            "  false — discard only if completely unrelated.",
            "When uncertain, return false.",
        ]
    lines += [
        "",
        "Reply ONLY with a compact JSON array of booleans, one per frame, in order.",
        "No extra text.",
        "Example: [true, false, true]",
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
    *,
    strict: bool,
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

    # If no images at all, reject in strict mode
    if valid_count == 0:
        return [False] * len(moments) if strict else [True] * len(moments)

    parts.insert(0, types.Part(text=_build_prompt(
        query, len(moments), person_name, folder_context, strict=strict,
    )))

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
        logger.warning("Gemini re-rank: could not parse response %r — %s", text[:200], "rejecting all" if strict else "keeping all")
        return [False] * len(moments) if strict else [True] * len(moments)
    except Exception as exc:
        logger.warning("Gemini re-rank failed (%s) — %s", exc, "rejecting all" if strict else "keeping all")
        return [False] * len(moments) if strict else [True] * len(moments)


async def rerank_moments(
    query: str,
    moments: list[SearchMoment],
    *,
    person_name: str | None = None,
    face_thumbnail_path: str | None = None,
    folder_context: str | None = None,
    thumbnail_dir: str = "./data/thumbnails",
    strict: bool = True,
) -> list[SearchMoment]:
    """
    Filter video frame hits using Gemini multimodal re-ranking.

    When strict=True (default), every candidate must pass Gemini validation —
    no automatic keep-top bypass.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.gemini_api_key or not moments:
        return moments

    keep_top = 0 if strict else _ALWAYS_KEEP_TOP
    guaranteed = moments[:keep_top]
    candidates = moments[keep_top:]

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
            strict=strict,
        )
        kept.extend(m for m, ok in zip(batch, mask) if ok)

    return guaranteed + kept


def _build_moment_snippet_prompt(
    query: str,
    n_moments: int,
    *,
    strict: bool,
) -> str:
    lines = [
        "You are a video transcript/description validator.",
        f'Search query: "{query}"',
        "",
        f"I am giving you {n_moments} video moment description(s).",
        "For EACH moment decide:",
    ]
    if strict:
        lines += [
            "  true  — the description clearly matches the query topic/action.",
            "  false — unrelated, ambiguous, or a different activity.",
            "Be STRICT. When uncertain, return false.",
        ]
    else:
        lines += [
            "  true  — plausible match.",
            "  false — clearly unrelated.",
        ]
    lines += [
        "",
        "Reply ONLY with a JSON array of booleans.",
        "Example: [true, false]",
    ]
    return "\n".join(lines)


def _rerank_moments_snippet_sync(
    query: str,
    moments: list[SearchMoment],
    *,
    strict: bool,
    model: str,
    api_key: str,
) -> list[bool]:
    from google import genai
    from google.genai import types

    if not moments:
        return []

    client = genai.Client(api_key=api_key)
    lines = [_build_moment_snippet_prompt(query, len(moments), strict=strict), ""]
    for i, moment in enumerate(moments, start=1):
        desc = (moment.snippet or "[no description]").strip()
        lines.append(f'Moment {i} ({moment.name} @ {moment.timestamp_sec:.1f}s): "{desc}"')

    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text="\n".join(lines))])],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = response.text or ""
        result = _parse_booleans(text, len(moments))
        if result is not None:
            kept = sum(result)
            logger.info("Gemini transcript re-rank: %d/%d kept for query %r", kept, len(moments), query)
            return result
        logger.warning("Gemini transcript re-rank parse failed — %s", "rejecting all" if strict else "keeping all")
        return [False] * len(moments) if strict else [True] * len(moments)
    except Exception as exc:
        logger.warning("Gemini transcript re-rank failed (%s)", exc)
        return [False] * len(moments) if strict else [True] * len(moments)


async def rerank_transcript_moments(
    query: str,
    moments: list[SearchMoment],
    *,
    strict: bool = True,
) -> list[SearchMoment]:
    """Filter transcript-based moments using their text descriptions."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.gemini_api_key or not moments:
        return moments

    kept: list[SearchMoment] = []
    for batch_start in range(0, len(moments), _BATCH):
        batch = moments[batch_start: batch_start + _BATCH]
        mask = await asyncio.to_thread(
            _rerank_moments_snippet_sync,
            query,
            batch,
            strict=strict,
            model=settings.gemini_model,
            api_key=settings.gemini_api_key,
        )
        kept.extend(m for m, ok in zip(batch, mask) if ok)
    return kept


_IMAGE_BATCH = 25
_IMAGE_ALWAYS_KEEP_TOP = 3


def _build_image_prompt(
    query: str,
    n_images: int,
    person_name: str | None,
    folder_context: str | None,
    *,
    strict_action: bool,
) -> str:
    lines = [
        "You are an image search result validator.",
        "",
        f'Search query: "{query}"',
    ]
    if person_name:
        lines.append(f'Person filter: results should contain "{person_name}".')
    if folder_context:
        lines.append(f"Folder context: {folder_context}")
    lines += [
        "",
        f"I am giving you {n_images} image description(s) (Image 1 … Image {n_images}).",
        "",
        "For EACH image decide:",
    ]
    if strict_action:
        lines += [
            "  true  — the description shows the SPECIFIC ACTION in the query",
            "           (e.g. query 'students cooking' → chopping, grilling, at a stove, food prep).",
            "  false — the description shows a different activity",
            "           (e.g. sitting and eating, talking, workshop, market, foosball, lecture).",
            "Be STRICT on the action. When the action does not match, return false.",
        ]
    else:
        lines += [
            "  true  — keep if the description has ANY connection to the query.",
            "  false — discard ONLY if completely unrelated.",
            "When in doubt, return true.",
        ]
    lines += [
        "",
        "Reply ONLY with a compact JSON array of booleans, one per image, in order.",
        "No extra text.",
        "Example: [true, false, true]",
    ]
    return "\n".join(lines)


def _rerank_images_caption_sync(
    query: str,
    files: list[SearchResultFile],
    *,
    person_name: str | None,
    folder_context: str | None,
    strict_action: bool,
    model: str,
    api_key: str,
) -> list[bool]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    lines = [
        _build_image_prompt(
            query, len(files), person_name, folder_context, strict_action=strict_action
        ),
        "",
    ]
    for i, item in enumerate(files, start=1):
        cap = (item.caption or "").strip() or "[no description available]"
        lines.append(f'Image {i} ({item.name}): "{cap}"')

    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text="\n".join(lines))])],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = response.text or ""
        result = _parse_booleans(text, len(files))
        if result is not None:
            kept = sum(result)
            logger.info("Gemini image re-rank: %d/%d kept for query %r", kept, len(files), query)
            return result
        logger.warning("Gemini image re-rank: could not parse %r — keeping all", text[:200])
        return [True] * len(files)
    except Exception as exc:
        logger.warning("Gemini image re-rank failed (%s) — keeping all candidates", exc)
        return [True] * len(files)


async def rerank_image_files(
    query: str,
    files: list[SearchResultFile],
    *,
    person_name: str | None = None,
    folder_context: str | None = None,
    strict_action: bool = False,
) -> list[SearchResultFile]:
    """Filter image hits using Gemini caption validation."""
    from app.concurrency.pools import effective_cpu_workers
    from app.config import get_settings

    settings = get_settings()
    if not settings.gemini_api_key or not files:
        return files

    keep_top = 0 if strict_action else _IMAGE_ALWAYS_KEEP_TOP
    guaranteed = files[:keep_top]
    candidates = files[keep_top:]
    if not candidates:
        return guaranteed

    batches = [
        candidates[i : i + _IMAGE_BATCH]
        for i in range(0, len(candidates), _IMAGE_BATCH)
    ]
    parallel = (
        settings.search_llm_batch_parallel
        if settings.search_llm_batch_parallel > 0
        else min(4, effective_cpu_workers(settings.cpu_thread_pool_size))
    )
    sem = asyncio.Semaphore(max(1, parallel))

    async def _run_batch(batch: list[SearchResultFile]) -> list[SearchResultFile]:
        async with sem:
            mask = await asyncio.to_thread(
                _rerank_images_caption_sync,
                query,
                batch,
                person_name=person_name,
                folder_context=folder_context,
                strict_action=strict_action,
                model=settings.gemini_model,
                api_key=settings.gemini_api_key,
            )
        return [item for item, ok in zip(batch, mask) if ok]

    batch_kept = await asyncio.gather(*[_run_batch(batch) for batch in batches])
    kept: list[SearchResultFile] = []
    for items in batch_kept:
        kept.extend(items)

    return guaranteed + kept
