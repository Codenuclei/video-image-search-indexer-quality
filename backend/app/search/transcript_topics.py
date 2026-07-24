"""Analyze a video transcript into topics / subtopics with timestamps + explanations."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 14_000
_MAX_TOPICS = 8
_MAX_SUBTOPICS = 5


def format_cue_line(start_sec: float, end_sec: float | None, text: str) -> str:
    start = _fmt_ts(start_sec)
    end = _fmt_ts(end_sec) if end_sec is not None and end_sec > start_sec + 0.25 else None
    span = f"{start}–{end}" if end else start
    cleaned = " ".join((text or "").split())
    return f"[{span}] {cleaned}"


def compact_transcript(
    cues: list[tuple[float, float | None, str]],
    *,
    max_chars: int = _MAX_TRANSCRIPT_CHARS,
) -> str:
    """Join timed cues into a compact transcript string for LLM prompts."""
    lines: list[str] = []
    total = 0
    for start, end, text in cues:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            continue
        line = format_cue_line(start, end, cleaned)
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def fallback_topics_from_cues(
    cues: list[tuple[float, float | None, str]],
    *,
    max_topics: int = 6,
) -> list[dict[str, Any]]:
    """Heuristic topic buckets when Gemini is unavailable."""
    usable = [(s, e, " ".join((t or "").split())) for s, e, t in cues if (t or "").strip()]
    if not usable:
        return []

    n = min(max_topics, max(1, len(usable) // 4 or 1))
    n = min(n, len(usable))
    chunk = max(1, len(usable) // n)
    topics: list[dict[str, Any]] = []

    for i in range(n):
        start_i = i * chunk
        end_i = len(usable) if i == n - 1 else min(len(usable), (i + 1) * chunk)
        bucket = usable[start_i:end_i]
        if not bucket:
            continue
        start_sec = float(bucket[0][0])
        end_sec = float(bucket[-1][1] if bucket[-1][1] is not None else bucket[-1][0])
        joined = " ".join(row[2] for row in bucket)
        title = _title_from_text(joined) or f"Segment {i + 1}"
        explanation = joined[:220].rstrip()
        if len(joined) > 220:
            explanation = explanation.rsplit(" ", 1)[0] + "…"

        subtopics: list[dict[str, Any]] = []
        mid = len(bucket) // 2
        if mid > 0 and len(bucket) >= 4:
            for j, slice_rows in enumerate((bucket[:mid], bucket[mid:])):
                if not slice_rows:
                    continue
                sub_joined = " ".join(r[2] for r in slice_rows)
                subtopics.append(
                    {
                        "title": _title_from_text(sub_joined) or f"Part {j + 1}",
                        "start_sec": float(slice_rows[0][0]),
                        "end_sec": float(
                            slice_rows[-1][1] if slice_rows[-1][1] is not None else slice_rows[-1][0]
                        ),
                        "explanation": (sub_joined[:160] + ("…" if len(sub_joined) > 160 else "")),
                    }
                )

        topics.append(
            {
                "title": title[:120],
                "start_sec": start_sec,
                "end_sec": end_sec if end_sec > start_sec else None,
                "explanation": explanation[:400],
                "subtopics": subtopics[:_MAX_SUBTOPICS],
            }
        )
    return topics


async def analyze_transcript_topics(
    *,
    transcript: str,
    video_name: str,
    api_key: str | None,
    model: str,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """
    Returns (topics, source, warning).
    source is "llm" | "fallback".
    """
    if not transcript.strip():
        return [], "empty", None

    if not api_key:
        # Caller should pass cues for fallback; empty here means they only gave text.
        return [], "fallback", "Gemini unavailable"

    try:
        topics = await _llm_topics(transcript=transcript, video_name=video_name, api_key=api_key, model=model)
        if topics:
            return topics, "llm", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript topic analysis failed: %s", exc)
        return [], "fallback", str(exc)[:160]

    return [], "fallback", "Could not parse topic outline"


async def _llm_topics(
    *,
    transcript: str,
    video_name: str,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    from google import genai
    from google.genai import types

    prompt = (
        "You analyze video transcripts and produce a topic outline for creators.\n"
        f"Video: {video_name or '(untitled)'}\n\n"
        "Transcript cues are timed as [mm:ss] or [mm:ss–mm:ss] followed by spoken text.\n"
        "Return ONLY a JSON array of topics (3–8). Each topic object must have:\n"
        '- title (short, max ~8 words)\n'
        "- start_sec (number, seconds from video start)\n"
        "- end_sec (number or null)\n"
        "- explanation (1–2 sentences: what this section covers and why it matters)\n"
        "- subtopics (array, 0–4 objects with the same keys except no nested subtopics)\n"
        "Use cue timestamps; estimate ranges that cover each topic. Prefer chronological order.\n"
        "Explanations should give the viewer context, not just repeat the title.\n\n"
        f"Transcript:\n{transcript}"
    )

    client = genai.Client(api_key=api_key)
    resp = await __import__("asyncio").to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.35,
            response_mime_type="application/json",
        ),
    )
    text = (resp.text or "").strip()
    return _parse_topics_json(text)


def _parse_topics_json(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    raw = json.loads(m.group())
    if not isinstance(raw, list):
        return []

    topics: list[dict[str, Any]] = []
    for row in raw:
        topic = _normalize_topic(row, allow_subtopics=True)
        if topic:
            topics.append(topic)
        if len(topics) >= _MAX_TOPICS:
            break
    return topics


def _normalize_topic(row: Any, *, allow_subtopics: bool) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or row.get("topic") or "").strip()
    if not title:
        return None
    start = _as_float(row.get("start_sec", row.get("start")))
    end = _as_float(row.get("end_sec", row.get("end")))
    explanation = str(row.get("explanation") or row.get("summary") or row.get("context") or "").strip()
    if not explanation:
        explanation = f"Covers {title}."

    subtopics: list[dict[str, Any]] = []
    if allow_subtopics:
        raw_subs = row.get("subtopics") or row.get("sub_topics") or []
        if isinstance(raw_subs, list):
            for sub in raw_subs:
                normalized = _normalize_topic(sub, allow_subtopics=False)
                if normalized:
                    # Drop nested key if present
                    normalized.pop("subtopics", None)
                    subtopics.append(normalized)
                if len(subtopics) >= _MAX_SUBTOPICS:
                    break

    out: dict[str, Any] = {
        "title": title[:120],
        "start_sec": start if start is not None else 0.0,
        "end_sec": end if end is not None and (start is None or end > start) else None,
        "explanation": explanation[:500],
    }
    if allow_subtopics:
        out["subtopics"] = subtopics
    return out


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_ts(sec: float | None) -> str:
    if sec is None:
        return "0:00"
    s = max(0, int(sec))
    m, r = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{r:02d}"
    return f"{m}:{r:02d}"


def _title_from_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    # First clause / sentence, truncated
    piece = re.split(r"[.!?;:\n]", cleaned, maxsplit=1)[0].strip() or cleaned
    words = piece.split()
    if len(words) > 8:
        piece = " ".join(words[:8])
    return piece[:80]
