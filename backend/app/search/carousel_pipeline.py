"""Carousel video pipeline: themes, hooks/topics extract, intent.

Rules enforced here:
- Contextual integrity: theme boundaries snap to cue starts (never mid-cue).
- Zero repetition: non-overlapping theme ranges; unique hook/topic strings.
- Person filter (when used) is presence-only: themes are never reframed around a person.
- Hooks are complete spoken lines; topics are theme-derived labels (not raw dumps).
- Hooks/topics prefer English: use parallel English cues when present, else translate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.search.english_text import (
    cues_need_english,
    english_text_for_window,
    is_english_text,
    needs_english,
    prefer_english_cues,
)
from app.search.transcript_topics import (
    compact_transcript,
    fallback_topics_from_cues,
    format_cue_line,
)

logger = logging.getLogger(__name__)

_MAX_THEMES = 8
_MAX_HOOKS = 8
_MAX_TOPICS = 8
_MAX_MERGED_HOOKS = 16
_MAX_MERGED_TOPICS = 16


def snap_themes_to_cues(
    themes: list[dict[str, Any]],
    cues: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    """Snap starts to cue beginnings; remove overlaps; keep chronological order."""
    if not themes:
        return []
    cue_starts = sorted({float(s) for s, _, t in cues if (t or "").strip()})
    if not cue_starts:
        return themes[:_MAX_THEMES]

    cleaned: list[dict[str, Any]] = []
    prev_end = -1.0
    for raw in sorted(themes, key=lambda t: float(t.get("start_sec") or 0)):
        start = float(raw.get("start_sec") or 0)
        end = raw.get("end_sec")
        end_f = float(end) if end is not None else None
        # Snap start to nearest cue at or before requested start (context start).
        snapped = max((c for c in cue_starts if c <= start + 0.05), default=cue_starts[0])
        if snapped < prev_end - 0.05:
            snapped = prev_end
        if end_f is not None and end_f <= snapped:
            end_f = None
        if end_f is not None and prev_end >= 0 and end_f <= prev_end:
            continue
        item = dict(raw)
        item["start_sec"] = snapped
        item["end_sec"] = end_f
        item["theme_id"] = item.get("theme_id") or f"theme_{len(cleaned) + 1}"
        cleaned.append(item)
        prev_end = end_f if end_f is not None else snapped + 1.0
        if len(cleaned) >= _MAX_THEMES:
            break
    return cleaned


async def build_harmonized_themes(
    *,
    cues: list[tuple[float, float | None, str]],
    video_name: str,
    search_entity: str | None = None,
    api_key: str | None,
    model: str,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Return normal narrative themes (search_entity is ignored — no reframing)."""
    del search_entity  # presence checks live in the router; themes stay video-native
    transcript = compact_transcript(cues)
    if not transcript.strip():
        return [], "empty", "No transcript cues for this video"

    warning: str | None = None
    if api_key:
        try:
            themes = await _llm_themes(
                transcript=transcript,
                video_name=video_name,
                api_key=api_key,
                model=model,
            )
            if themes:
                themes = snap_themes_to_cues(themes, cues)
                for t in themes:
                    t["harmonized"] = False
                    t["search_entity"] = None
                return themes, "llm", None
        except Exception as exc:  # noqa: BLE001
            logger.warning("carousel theme LLM failed: %s", exc)
            warning = str(exc)[:160]
    else:
        warning = "Gemini unavailable — using transcript buckets"

    fallback = fallback_topics_from_cues(cues, max_topics=6)
    themes = []
    for i, row in enumerate(fallback):
        title = str(row.get("title") or f"Segment {i + 1}")
        themes.append(
            {
                "theme_id": f"theme_{i + 1}",
                "title": title[:120],
                "start_sec": float(row.get("start_sec") or 0),
                "end_sec": row.get("end_sec"),
                "summary": str(row.get("explanation") or "")[:500],
                "harmonized": False,
                "search_entity": None,
            }
        )
    themes = snap_themes_to_cues(themes, cues)
    return themes, "fallback", warning


async def _llm_themes(
    *,
    transcript: str,
    video_name: str,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    import asyncio

    from google import genai
    from google.genai import types

    prompt = (
        "You segment a video transcript into distinct narrative themes for a carousel studio.\n"
        f"Video: {video_name or '(untitled)'}\n"
        "Hard rules:\n"
        "- Start each theme at the beginning of a logical context (never mid-sentence).\n"
        "- Theme titles MUST be complete phrases (never end with to/be/in/of/and/the…).\n"
        "- Theme titles and summaries MUST be in natural English "
        "(translate if the transcript is Hindi/Hinglish/other).\n"
        "- Zero overlap between themes; chronological; no duplicate phrasing.\n"
        "- Group by natural narrative shifts.\n"
        "Return ONLY JSON array (3–8 objects). Each object:\n"
        '- theme_id (string like "theme_1")\n'
        "- title (short, English)\n"
        "- start_sec (number)\n"
        "- end_sec (number or null)\n"
        "- summary (1–2 sentences, English)\n\n"
        f"Transcript:\n{transcript}"
    )

    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )
    return _parse_themes_json((resp.text or "").strip())


def _parse_themes_json(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    raw = json.loads(m.group())
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        title = _complete_theme_title(title)
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        start = _as_float(row.get("start_sec", row.get("start")))
        end = _as_float(row.get("end_sec", row.get("end")))
        summary = str(row.get("summary") or row.get("explanation") or "").strip()
        out.append(
            {
                "theme_id": str(row.get("theme_id") or f"theme_{i + 1}")[:64],
                "title": title[:120],
                "start_sec": start if start is not None else 0.0,
                "end_sec": end if end is not None and (start is None or end > start) else None,
                "summary": (summary or f"Covers {title}.")[:500],
            }
        )
        if len(out) >= _MAX_THEMES:
            break
    return out


def extract_hooks_and_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    theme_title: str = "",
    theme_summary: str = "",
    english_cues: list[tuple[float, float | None, str]] | None = None,
) -> dict[str, Any]:
    """
    Hooks: complete contextual spoken lines from the theme window (never mid-thought scraps).
    Topics: thematic labels derived from the selected theme — not raw transcript dumps.

    When english_cues are provided (parallel English caption track), hooks are pulled
    from that track for the same time window. Otherwise prefers English lines already
    present in `cues` when the window is mixed-language.
    """
    primary = english_cues if english_cues else cues
    window = prefer_english_cues(_cues_in_range(primary, start_sec, end_sec))
    # Fall back to indexed cues if English alternate is empty in this window.
    if not window and english_cues:
        window = prefer_english_cues(_cues_in_range(cues, start_sec, end_sec))
    stitched = _stitch_complete_utterances(window)
    hooks = _pick_contextual_hooks(stitched)
    if english_cues:
        for h in hooks:
            h["english_source"] = "caption_track"
            h["translated"] = False
    topics = _topics_from_theme(
        theme_title=theme_title,
        theme_summary=theme_summary,
        hooks=hooks,
        stitched=stitched,
        theme_start=float(start_sec or 0),
        theme_end=end_sec,
    )
    return {
        "hooks": hooks[:_MAX_HOOKS],
        "topics": topics[:_MAX_TOPICS],
        "cue_count": len(window),
        "english_source": "caption_track" if english_cues else "indexed",
    }


async def extract_hooks_and_topics_async(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    theme_title: str = "",
    theme_summary: str = "",
    search_entity: str | None = None,
    api_key: str | None = None,
    model: str = "",
    english_cues: list[tuple[float, float | None, str]] | None = None,
) -> dict[str, Any]:
    """Same as extract_hooks_and_topics, with English preference + LLM topics."""
    # Prefer a parallel English track when the indexed window is non-English.
    window_indexed = _cues_in_range(cues, start_sec, end_sec)
    use_english_track = bool(english_cues) and (
        cues_need_english(window_indexed) or not window_indexed
    )
    active_english = english_cues if use_english_track else None

    base = extract_hooks_and_topics(
        cues,
        start_sec=start_sec,
        end_sec=end_sec,
        theme_title=theme_title,
        theme_summary=theme_summary,
        english_cues=active_english,
    )

    # If hooks still non-English but we have english_cues, map by timestamp window.
    if english_cues and any(needs_english(h.get("text", "")) for h in base["hooks"]):
        base["hooks"] = _swap_hooks_with_english_cues(base["hooks"], english_cues)

    if api_key:
        window = _cues_in_range(
            active_english or prefer_english_cues(cues),
            start_sec,
            end_sec,
        ) or window_indexed
        transcript = compact_transcript(window, max_chars=6000)
        try:
            llm_topics = await _llm_topics_from_theme(
                theme_title=theme_title,
                theme_summary=theme_summary,
                transcript=transcript,
                search_entity=search_entity,
                api_key=api_key,
                model=model,
                theme_start=float(start_sec or 0),
                theme_end=end_sec,
                hooks=base.get("hooks") or [],
                stitched=_stitch_complete_utterances(window),
            )
            if llm_topics:
                base["topics"] = llm_topics[:_MAX_TOPICS]
        except Exception as exc:  # noqa: BLE001
            logger.warning("theme topic generation failed: %s", exc)

    base = await ensure_english_display_texts(
        base,
        english_cues=english_cues,
        api_key=api_key,
        model=model,
    )
    return base


def _swap_hooks_with_english_cues(
    hooks: list[dict[str, Any]],
    english_cues: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in hooks:
        row = dict(h)
        if is_english_text(str(row.get("text") or "")):
            out.append(row)
            continue
        alt = english_text_for_window(
            english_cues,
            start_sec=float(row.get("start_sec") or 0),
            end_sec=row.get("end_sec"),
        )
        if alt:
            row["original_text"] = row.get("text")
            row["text"] = alt if len(alt.split()) <= 30 else _trim_to_clause(alt, 30)
            row["translated"] = False
            row["english_source"] = "caption_track"
            row["verbatim"] = True
        out.append(row)
    return out


async def ensure_english_display_texts(
    payload: dict[str, Any],
    *,
    english_cues: list[tuple[float, float | None, str]] | None = None,
    api_key: str | None,
    model: str,
) -> dict[str, Any]:
    """Translate remaining non-English hooks/topics to natural English for display."""
    hooks = [dict(h) for h in (payload.get("hooks") or [])]
    topics = [dict(t) for t in (payload.get("topics") or [])]

    # Last chance: map hooks to English cue windows before LLM translate.
    if english_cues:
        hooks = _swap_hooks_with_english_cues(hooks, english_cues)

    to_translate: list[tuple[str, int, str]] = []
    for i, h in enumerate(hooks):
        text = str(h.get("text") or "").strip()
        if text and needs_english(text):
            to_translate.append(("hook", i, text))
    for i, t in enumerate(topics):
        text = str(t.get("text") or "").strip()
        if text and needs_english(text):
            to_translate.append(("topic", i, text))

    translations: list[str] = []
    if to_translate and api_key:
        try:
            translations = await _llm_translate_lines(
                [text for _, _, text in to_translate],
                api_key=api_key,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hook/topic English translation failed: %s", exc)

    any_translated = False
    for n, (kind, idx, original) in enumerate(to_translate):
        eng = translations[n] if n < len(translations) else ""
        eng = " ".join((eng or "").split()).strip()
        if not eng or not is_english_text(eng):
            continue
        if kind == "hook":
            hooks[idx]["original_text"] = hooks[idx].get("original_text") or original
            hooks[idx]["text"] = eng[:400]
            hooks[idx]["translated"] = True
            hooks[idx]["english_source"] = "llm_translate"
            hooks[idx]["verbatim"] = False
            any_translated = True
        else:
            topics[idx]["original_text"] = topics[idx].get("original_text") or original
            topics[idx]["text"] = eng[:120]
            topics[idx]["translated"] = True
            topics[idx]["english_source"] = "llm_translate"
            any_translated = True

    for h in hooks:
        h.setdefault("translated", False)
        h.setdefault("english_source", payload.get("english_source") or "indexed")
    for t in topics:
        t.setdefault("translated", False)
        t.setdefault("english_source", "generated")

    payload = dict(payload)
    payload["hooks"] = hooks
    payload["topics"] = topics
    payload["hooks_english"] = (
        all(is_english_text(str(h.get("text") or "")) for h in hooks) if hooks else True
    )
    payload["topics_english"] = (
        all(is_english_text(str(t.get("text") or "")) for t in topics) if topics else True
    )
    payload["any_translated"] = any_translated or any(bool(h.get("translated")) for h in hooks)
    return payload


async def _llm_translate_lines(
    lines: list[str],
    *,
    api_key: str,
    model: str,
) -> list[str]:
    """Translate lines to natural English; returns list aligned to input order."""
    import asyncio

    from google import genai
    from google.genai import types

    if not lines:
        return []

    numbered = [{"i": i, "text": line} for i, line in enumerate(lines)]
    prompt = (
        "Translate each line into natural, spoken English for a video carousel hook/topic.\n"
        "Rules:\n"
        "- Preserve meaning; do NOT transliterate (no Romanized Hindi dumps).\n"
        "- Keep roughly the same length; complete sentences when the source is a sentence.\n"
        "- Return ONLY a JSON array of objects: {\"i\": number, \"text\": \"English\"}.\n\n"
        f"Lines:\n{json.dumps(numbered, ensure_ascii=False)}"
    )
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    raw = json.loads((resp.text or "").strip() or "[]")
    out = [""] * len(lines)
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if text and 0 <= i < len(lines):
            out[i] = text
    return out


# Back-compat alias
def extract_verbatim_hooks_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
) -> dict[str, Any]:
    return extract_hooks_and_topics(cues, start_sec=start_sec, end_sec=end_sec)


def _stitch_complete_utterances(
    window: list[tuple[float, float | None, str]],
) -> list[dict[str, Any]]:
    """Merge adjacent cues into complete thoughts (avoid incomplete / context-less scraps)."""
    chunks: list[dict[str, Any]] = []
    buf_text: list[str] = []
    buf_start: float | None = None
    buf_end: float | None = None

    def flush() -> None:
        nonlocal buf_text, buf_start, buf_end
        if not buf_text or buf_start is None:
            buf_text, buf_start, buf_end = [], None, None
            return
        text = " ".join(buf_text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text.split()) >= 4:
            chunks.append(
                {
                    "text": text,
                    "start_sec": float(buf_start),
                    "end_sec": float(buf_end) if buf_end is not None else None,
                }
            )
        buf_text, buf_start, buf_end = [], None, None

    for s, e, raw in window:
        piece = " ".join((raw or "").split())
        if not piece:
            continue
        if buf_start is None:
            buf_start = float(s)
        buf_text.append(piece)
        buf_end = float(e) if e is not None else float(s)
        joined = " ".join(buf_text)
        words = len(joined.split())
        ends_thought = bool(re.search(r"[.!?…][\"')\]]*$", piece)) or words >= 22
        if ends_thought and words >= 6:
            flush()
        elif words >= 36:
            flush()
    flush()
    return chunks


def _pick_contextual_hooks(stitched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer complete, self-contained spoken lines with enough context for a carousel card."""
    hooks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in stitched:
        text = str(row.get("text") or "").strip()
        words = text.split()
        if len(words) < 6:
            continue
        # Drop obvious mid-clause scraps
        if text[:1].islower() and not re.match(r"^(I|I'm|I've|I'd|we|we're|you|it's)\b", text, re.I):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        # Cap length but keep a full clause
        if len(words) > 28:
            text = _trim_to_clause(text, max_words=28)
        hooks.append(
            {
                "id": f"hook_{len(hooks) + 1}",
                "text": text,
                "start_sec": float(row["start_sec"]),
                "end_sec": row.get("end_sec"),
                "verbatim": True,
                "contextual": True,
            }
        )
        if len(hooks) >= _MAX_HOOKS:
            break

    # If still thin, relax filters but merge more context
    if len(hooks) < 3:
        for row in stitched:
            text = str(row.get("text") or "").strip()
            if len(text.split()) < 5:
                continue
            key = text.lower()
            if key in {h["text"].lower() for h in hooks}:
                continue
            hooks.append(
                {
                    "id": f"hook_{len(hooks) + 1}",
                    "text": text if len(text.split()) <= 30 else _trim_to_clause(text, 30),
                    "start_sec": float(row["start_sec"]),
                    "end_sec": row.get("end_sec"),
                    "verbatim": True,
                    "contextual": True,
                }
            )
            if len(hooks) >= _MAX_HOOKS:
                break
    return hooks


def _spread_topic_spans(
    count: int,
    *,
    theme_start: float,
    theme_end: float | None,
    hooks: list[dict[str, Any]] | None = None,
    stitched: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float | None]]:
    """Distinct time spans for topic labels — never stamp every topic at theme start."""
    n = max(0, int(count))
    if n == 0:
        return []

    refs: list[tuple[float, float | None]] = []
    for row in hooks or []:
        try:
            s = float(row.get("start_sec") or 0)
        except (TypeError, ValueError):
            continue
        end = row.get("end_sec")
        e = float(end) if end is not None else None
        if e is not None and e <= s:
            e = None
        refs.append((s, e))
    if not refs:
        for row in stitched or []:
            try:
                s = float(row.get("start_sec") or 0)
            except (TypeError, ValueError):
                continue
            end = row.get("end_sec")
            e = float(end) if end is not None else None
            if e is not None and e <= s:
                e = None
            refs.append((s, e))

    start = float(theme_start or 0)
    end = float(theme_end) if theme_end is not None else None
    if end is None or end <= start:
        if refs:
            last_e = max((e if e is not None else s + 4.0) for s, e in refs)
            end = max(last_e, start + max(4.0, n * 4.0))
        else:
            end = start + max(8.0, n * 5.0)

    if refs and len(refs) >= n:
        step = len(refs) / n
        return [refs[min(len(refs) - 1, int(i * step))] for i in range(n)]

    window = max(end - start, float(n))
    seg = window / n
    spans: list[tuple[float, float | None]] = []
    for i in range(n):
        s = round(start + i * seg, 2)
        e = round(start + (i + 1) * seg, 2)
        if refs and i < len(refs):
            hs, he = refs[i]
            s = hs
            e = float(he) if he is not None else round(hs + max(3.0, seg), 2)
        if e <= s:
            e = round(s + max(3.0, seg), 2)
        spans.append((s, e))
    return spans


def _topics_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    hooks: list[dict[str, Any]] | list[str],
    stitched: list[dict[str, Any]],
    theme_start: float = 0.0,
    theme_end: float | None = None,
) -> list[dict[str, Any]]:
    """Heuristic thematic topics from the selected theme (labels, not transcript dumps)."""
    title = (theme_title or "").strip() or "Theme"
    summary = (theme_summary or "").strip()
    hook_rows: list[dict[str, Any]] = []
    hook_texts: list[str] = []
    for h in hooks or []:
        if isinstance(h, dict):
            hook_rows.append(h)
            hook_texts.append(str(h.get("text") or ""))
        else:
            hook_texts.append(str(h))

    seeds: list[str] = []
    if title:
        seeds.append(title)
    for part in re.split(r"[.;\n]", summary):
        cleaned = " ".join(part.split()).strip(" -–—")
        if 3 <= len(cleaned.split()) <= 12:
            seeds.append(cleaned)
    for h in hook_texts[:4]:
        angle = _topic_angle_from_hook(h)
        if angle:
            seeds.append(angle)

    labels: list[str] = []
    seen: set[str] = set()
    for label in seeds:
        key = label.lower()
        if key in seen or len(label) < 4:
            continue
        seen.add(key)
        if len(label.split()) > 14 or (label.startswith('"') or label.count(",") > 2):
            continue
        labels.append(label[:120])
        if len(labels) >= _MAX_TOPICS:
            break

    if not labels:
        labels = [title[:120]]

    spans = _spread_topic_spans(
        len(labels),
        theme_start=theme_start,
        theme_end=theme_end,
        hooks=hook_rows,
        stitched=stitched,
    )
    topics: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        s, e = spans[i] if i < len(spans) else (float(theme_start or 0), None)
        topics.append(
            {
                "id": f"topic_{i + 1}",
                "text": label,
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "verbatim": False,
                "generated": True,
            }
        )
    return topics


async def _llm_topics_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    transcript: str,
    search_entity: str | None,
    api_key: str,
    model: str,
    theme_start: float = 0.0,
    theme_end: float | None = None,
    hooks: list[dict[str, Any]] | None = None,
    stitched: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    import asyncio

    from google import genai
    from google.genai import types

    entity = (search_entity or "").strip()
    prompt = (
        "You invent short thematic TOPIC LABELS for a video carousel from one selected theme.\n"
        "Topics must be generated ideas/angles grounded in the theme — NOT raw transcript quotes.\n"
        "Rules:\n"
        "- 4–8 topics\n"
        "- Each topic: 2–8 words, title-case or short phrase\n"
        "- MUST be natural English (translate ideas if transcript is Hindi/Hinglish/other)\n"
        "- No incomplete thoughts; each must stand alone as a slide theme\n"
        "- Do not paste spoken dialogue as a topic\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of strings.\n\n"
        f"Theme transcript context (for grounding only):\n{transcript[:5000]}"
    )
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.4,
            response_mime_type="application/json",
        ),
    )
    raw = json.loads((resp.text or "").strip() or "[]")
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for item in raw:
        label = str(
            item if not isinstance(item, dict) else item.get("text") or item.get("topic") or ""
        ).strip()
        if not label or len(label.split()) > 10:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label[:120])
        if len(labels) >= _MAX_TOPICS:
            break

    spans = _spread_topic_spans(
        len(labels),
        theme_start=theme_start,
        theme_end=theme_end,
        hooks=hooks,
        stitched=stitched,
    )
    out: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        s, e = spans[i] if i < len(spans) else (float(theme_start or 0), None)
        out.append(
            {
                "id": f"topic_{i + 1}",
                "text": label,
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "verbatim": False,
                "generated": True,
            }
        )
    return out


def _topic_angle_from_hook(hook: str) -> str:
    words = " ".join((hook or "").split()).split()
    if len(words) < 4:
        return ""
    # Take a noun-ish slice without dumping the whole quote
    slice_words = words[0:6] if words[0][0:1].isupper() else words[:5]
    label = " ".join(slice_words).rstrip(".,;:!?")
    if len(label.split()) < 2:
        return ""
    return label[:80]


def _trim_to_clause(text: str, max_words: int = 28) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    # Prefer ending at punctuation inside the cut
    m = list(re.finditer(r"[.!?]", cut))
    if m:
        return cut[: m[-1].end()].strip()
    return cut.rstrip(",;:") + "…"


def _cues_in_range(
    cues: list[tuple[float, float | None, str]],
    start_sec: float,
    end_sec: float | None,
) -> list[tuple[float, float | None, str]]:
    out: list[tuple[float, float | None, str]] = []
    for s, e, t in cues:
        if not (t or "").strip():
            continue
        if s < start_sec - 0.05:
            continue
        if end_sec is not None and s > float(end_sec) + 0.25:
            continue
        out.append((s, e, t))
    return out


async def deduce_directional_intent(
    *,
    theme_title: str,
    theme_summary: str,
    hooks: list[str],
    topics: list[str],
    search_entity: str | None,
    api_key: str | None,
    model: str,
) -> dict[str, Any]:
    """Intent discovery only — does not write a script."""
    entity = (search_entity or "").strip()
    fallback_label = _fallback_intent(theme_title, hooks, topics, entity)
    if not api_key:
        return {"intent": fallback_label, "intent_score": 0.55, "source": "fallback"}

    try:
        import asyncio

        from google import genai
        from google.genai import types

        prompt = (
            "Deduce the creator's directional intent for a video carousel segment. "
            "Do NOT write a script. Return ONLY JSON: "
            '{"intent": "one sentence", "intent_score": 0.0-1.0}\n'
            f"Theme: {theme_title}\nSummary: {theme_summary}\n"
            f"Entity: {entity or '(none)'}\n"
            f"Hooks (verbatim): {hooks}\nTopics (verbatim): {topics}\n"
        )
        client = genai.Client(api_key=api_key)
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        parsed = json.loads((resp.text or "").strip() or "{}")
        intent = str(parsed.get("intent") or fallback_label).strip()[:400]
        score = parsed.get("intent_score", 0.7)
        try:
            score_f = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            score_f = 0.7
        return {"intent": intent, "intent_score": score_f, "source": "llm"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent deduction failed: %s", exc)
        return {"intent": fallback_label, "intent_score": 0.5, "source": "fallback"}


def _fallback_intent(title: str, hooks: list[str], topics: list[str], entity: str) -> str:
    bits = [f"Tune into “{title}”"]
    if entity:
        bits.append(f"centered on {entity}")
    if hooks:
        bits.append(f"opening on “{hooks[0][:80]}”")
    if topics:
        bits.append(f"developing “{topics[0][:80]}”")
    return " — ".join(bits)


def _complete_theme_title(title: str, *, max_words: int = 12) -> str:
    cleaned = " ".join((title or "").split()).strip()
    if not cleaned:
        return ""
    words = cleaned.split()
    dangling = {
        "to", "be", "in", "on", "at", "of", "for", "and", "or", "the", "a", "an",
        "with", "from", "as", "is", "are", "was", "were", "their", "our", "my",
    }
    while words and words[-1].lower().strip(".,;:!?") in dangling:
        words.pop()
    if len(words) > max_words:
        words = words[:max_words]
        while words and words[-1].lower().strip(".,;:!?") in dangling:
            words.pop()
    return " ".join(words) if words else cleaned[:80]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cue_preview_lines(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows = []
    for s, e, t in _cues_in_range(cues, start_sec, end_sec)[:limit]:
        rows.append(
            {
                "start_sec": float(s),
                "end_sec": float(e) if e is not None else None,
                "text": " ".join((t or "").split()),
                "label": format_cue_line(s, e, t or ""),
            }
        )
    return rows


def merge_theme_extracts(
    extracts: list[dict[str, Any]],
    *,
    max_hooks: int = _MAX_MERGED_HOOKS,
    max_topics: int = _MAX_MERGED_TOPICS,
) -> dict[str, Any]:
    """Merge per-theme extracts: unique hooks/topics sorted by start time."""
    hooks: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    seen_hooks: set[str] = set()
    seen_topics: set[str] = set()
    any_translated = False
    english_source: str | None = None

    for payload in extracts:
        if not isinstance(payload, dict):
            continue
        if payload.get("any_translated"):
            any_translated = True
        if english_source is None and payload.get("english_source"):
            english_source = str(payload.get("english_source"))
        for row in payload.get("hooks") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_hooks:
                continue
            seen_hooks.add(key)
            item = dict(row)
            item["text"] = text
            hooks.append(item)
            if item.get("translated"):
                any_translated = True
        for row in payload.get("topics") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_topics:
                continue
            seen_topics.add(key)
            item = dict(row)
            item["text"] = text
            topics.append(item)
            if item.get("translated"):
                any_translated = True

    hooks.sort(key=lambda r: (float(r.get("start_sec") or 0), str(r.get("text") or "")))
    topics.sort(key=lambda r: (float(r.get("start_sec") or 0), str(r.get("text") or "")))
    hooks = hooks[: max(1, int(max_hooks))]
    topics = topics[: max(1, int(max_topics))]
    for i, h in enumerate(hooks):
        h["id"] = f"hook_{i + 1}"
    for i, t in enumerate(topics):
        t["id"] = f"topic_{i + 1}"

    return {
        "hooks": hooks,
        "topics": topics,
        "any_translated": any_translated,
        "english_source": english_source,
        "hooks_english": (
            all(is_english_text(str(h.get("text") or "")) for h in hooks) if hooks else True
        ),
        "topics_english": (
            all(is_english_text(str(t.get("text") or "")) for t in topics) if topics else True
        ),
    }


def merge_preview_windows(
    cues: list[tuple[float, float | None, str]],
    windows: list[tuple[float, float | None]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Union of cue preview lines across theme windows, time-ordered unique."""
    seen: set[tuple[float, str]] = set()
    rows: list[dict[str, Any]] = []
    for start, end in windows:
        for row in cue_preview_lines(cues, start_sec=float(start or 0), end_sec=end, limit=limit):
            key = (round(float(row["start_sec"]), 2), str(row.get("text") or "")[:80].lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda r: float(r.get("start_sec") or 0))
    return rows[:limit]
