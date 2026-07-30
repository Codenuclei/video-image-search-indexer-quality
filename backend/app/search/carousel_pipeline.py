"""Carousel video pipeline: themes, hooks/topics extract, intent.

Rules enforced here:
- Contextual integrity: theme boundaries snap to cue starts (never mid-cue).
- Zero repetition: non-overlapping theme ranges; unique hook/topic strings.
- Person filter (when used) is presence-only: themes are never reframed around a person.
- Generation order: cohesive topics → optional subtopics → hooks (one topic at a time).
- Topics are true thematic clusters from the transcript (where the speaker takes a
  direction), not scattered keyword tags. Subtopics nest under a parent when natural.
- Hooks are crafted for a singular topic and must not reuse another topic's angle.
- Time spans stay aligned to spoken utterances for frames.
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
)  # noqa: F401 — format_cue_line used by chunk helpers

logger = logging.getLogger(__name__)

_MAX_THEMES = 8
_MAX_HOOKS = 20
_MAX_TOPICS = 14
_MAX_MERGED_HOOKS = 24
_MAX_MERGED_TOPICS = 24
# Per-chunk transcript budget for Gemini (full timed cues; chunk+merge for long talks).
_TOPIC_CHUNK_CHARS = 12_000
_TOPIC_CHUNK_OVERLAP_CUES = 6


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
    Hooks: candidate spoken windows (later analysed into punchy display hooks).
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
    """Topics → subtopics → hooks (one topic at a time), with English preference."""
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

    # Prefer indexed cues when the English alternate track is sparse/empty in-range
    # (that bug made Gemini "not read" the talk — 1–2 cues instead of dozens).
    primary_pool = prefer_english_cues(cues)
    if active_english:
        eng_window = _cues_in_range(active_english, start_sec, end_sec)
        idx_chars = len(compact_transcript(window_indexed, max_chars=200_000))
        eng_chars = len(compact_transcript(eng_window, max_chars=200_000))
        if eng_chars >= max(800, int(idx_chars * 0.45)) and len(eng_window) >= max(
            8, len(window_indexed) // 4
        ):
            primary_pool = active_english
        else:
            logger.warning(
                "english cue window too thin (eng=%d/%d chars vs idx=%d/%d) — using indexed transcript",
                len(eng_window),
                eng_chars,
                len(window_indexed),
                idx_chars,
            )
            primary_pool = cues

    window = _cues_in_range(primary_pool, start_sec, end_sec) or window_indexed
    window, used_start, used_end = _expand_thin_topic_window(
        primary_pool if primary_pool else cues,
        window,
        start_sec=float(start_sec or 0),
        end_sec=end_sec,
    )
    # Full timed cues for this theme window (chunked inside the topic LLM helper).
    full_transcript = compact_transcript(window, max_chars=200_000)
    stitched = _stitch_complete_utterances(window)
    cue_chars = len(full_transcript)
    cue_count = len(window)
    start_sec = used_start
    end_sec = used_end
    logger.info(
        "carousel topic extract: cues=%d chars=%d theme=%r window=%.1f–%s",
        cue_count,
        cue_chars,
        (theme_title or "")[:80],
        float(start_sec or 0),
        f"{float(end_sec):.1f}" if end_sec is not None else "end",
    )

    topic_tree: list[dict[str, Any]] = []
    topic_source = "none"
    chunks_used = 0
    if api_key and window:
        try:
            topic_tree, chunks_used = await _llm_topic_tree_from_cues(
                cues=window,
                theme_title=theme_title,
                theme_summary=theme_summary,
                search_entity=search_entity,
                api_key=api_key,
                model=model,
                theme_start=float(start_sec or 0),
                theme_end=end_sec,
            )
            topic_source = "llm_chunked" if chunks_used > 1 else "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("topic tree generation failed: %s", exc)
            topic_tree = []
            topic_source = "error"

    if not topic_tree:
        # Fall back: heuristic buckets from ALL cues in window (not a truncated sample).
        fb = fallback_topics_from_cues(window, max_topics=min(10, max(4, cue_count // 8 or 4)))
        if fb:
            topic_tree = _flat_topics_to_tree(
                [
                    {
                        "id": f"topic_{i + 1}",
                        "text": t.get("title"),
                        "start_sec": t.get("start_sec"),
                        "end_sec": t.get("end_sec"),
                        "explanation": t.get("explanation"),
                        "subtopics": t.get("subtopics") or [],
                    }
                    for i, t in enumerate(fb)
                ]
            )
            # Promote nested fallback subtopics into tree shape.
            for node, raw in zip(topic_tree, fb):
                subs = []
                for j, sub in enumerate(raw.get("subtopics") or []):
                    if not isinstance(sub, dict):
                        continue
                    title = str(sub.get("title") or "").strip()
                    if not title:
                        continue
                    subs.append(
                        {
                            "id": f"{node['id']}_sub_{j + 1}",
                            "text": title[:120],
                            "start_sec": float(sub.get("start_sec") or node["start_sec"]),
                            "end_sec": sub.get("end_sec"),
                            "explanation": str(sub.get("explanation") or "")[:300],
                            "hooks": [],
                        }
                    )
                node["subtopics"] = subs
            topic_source = "fallback_cues"
        elif api_key and full_transcript.strip():
            try:
                flat = await _llm_topics_from_theme(
                    theme_title=theme_title,
                    theme_summary=theme_summary,
                    transcript=full_transcript[:_TOPIC_CHUNK_CHARS],
                    search_entity=search_entity,
                    api_key=api_key,
                    model=model,
                    theme_start=float(start_sec or 0),
                    theme_end=end_sec,
                    hooks=base.get("hooks") or [],
                    stitched=stitched,
                ) or []
                topic_tree = _flat_topics_to_tree(flat)
                topic_source = "llm_flat"
            except Exception as exc:  # noqa: BLE001
                logger.warning("theme topic generation failed: %s", exc)

    # Light heuristic dedupe only — never collapse a rich talk into 2–3 vague labels.
    if len(topic_tree) >= 2:
        before = len(topic_tree)
        labels = heuristic_topic_dedupe(
            [
                {
                    "id": t.get("id"),
                    "text": t.get("text"),
                    "start_sec": t.get("start_sec"),
                    "end_sec": t.get("end_sec"),
                    "explanation": t.get("explanation"),
                    "subtopics": t.get("subtopics"),
                    "hooks": t.get("hooks"),
                }
                for t in topic_tree
            ],
            threshold=0.62,
        )
        # Preserve subtopics/hooks from originals by text key.
        by_text = {str(t.get("text") or "").strip().lower(): t for t in topic_tree}
        topic_tree = []
        for lab in labels:
            key = str(lab.get("text") or "").strip().lower()
            src = by_text.get(key) or lab
            topic_tree.append(src)
        logger.info(
            "carousel topic dedupe (heuristic): %d → %d",
            before,
            len(topic_tree),
        )

    # Hooks: craft for ONE singular topic at a time (no cross-topic reuse).
    cue_corpus = [str(t or "") for _s, _e, t in window if (t or "").strip()]
    all_hooks: list[dict[str, Any]] = []
    used_angles: list[str] = []
    verbatim_stats_total = {
        "checked": 0,
        "rejected_verbatim": 0,
        "rewritten": 0,
        "dropped": 0,
    }
    hook_pool = primary_pool if primary_pool else cues
    for topic in topic_tree:
        topic_window = _cues_for_topic_ranges(
            hook_pool,
            topic,
            fallback_start=float(start_sec or 0),
            fallback_end=end_sec,
        ) or window
        topic_stitched = _stitch_complete_utterances(topic_window)
        candidates = _pick_contextual_hooks(topic_stitched)[:4]
        if not candidates:
            # Emergency: longest cues in the topic window so we never emit empty topics.
            candidates = _emergency_hook_candidates(topic_stitched or topic_window, limit=3)
        if not candidates:
            topic["hooks"] = []
            continue
        for h in candidates:
            h["topic_id"] = topic.get("id")
            h["topic_text"] = topic.get("text")
        crafted: list[dict[str, Any]] | None = None
        if api_key:
            try:
                crafted = await _llm_hooks_for_singular_topic(
                    hooks=candidates,
                    topic_title=str(topic.get("text") or ""),
                    topic_explanation=str(topic.get("explanation") or ""),
                    theme_title=theme_title,
                    theme_summary=theme_summary,
                    used_angles=used_angles,
                    api_key=api_key,
                    model=model,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("per-topic hook craft failed for %s: %s", topic.get("id"), exc)
        if crafted:
            topic_hooks = crafted
        else:
            topic_hooks = heuristic_craft_hooks(
                candidates, theme_title=str(topic.get("text") or theme_title)
            )
            for h in topic_hooks:
                h["topic_id"] = topic.get("id")
                h["topic_text"] = topic.get("text")
        # Hard verbatim guard (LLM + heuristic)
        local_corpus = cue_corpus + [str(c.get("text") or "") for c in candidates]
        topic_hooks, vstats = enforce_non_verbatim_hooks(
            topic_hooks,
            local_corpus,
            theme_title=str(topic.get("text") or theme_title),
        )
        for k, v in vstats.items():
            verbatim_stats_total[k] = verbatim_stats_total.get(k, 0) + v
        # Keep 1–2 hooks per topic to avoid batch overlap.
        topic_hooks = topic_hooks[:2]
        for h in topic_hooks:
            used_angles.append(str(h.get("text") or ""))
            all_hooks.append(h)
        topic["hooks"] = topic_hooks
        # Attach hooks to best-matching subtopic by time overlap when present.
        subs = list(topic.get("subtopics") or [])
        if subs:
            for h in topic_hooks:
                best = _best_subtopic_for_hook(h, subs)
                if best is not None:
                    h["subtopic_id"] = best.get("id")
                    h["subtopic_text"] = best.get("text")
                    best.setdefault("hooks", []).append(h)
            for sub in subs:
                sub.setdefault("hooks", [])

    # Structural guard: every returned topic/subtopic section must have ≥1 hook.
    topic_tree, all_hooks, prune_stats = _ensure_hooks_on_every_section(
        topic_tree,
        all_hooks=all_hooks,
        cues=hook_pool,
        theme_title=theme_title,
        cue_corpus=cue_corpus,
    )
    verbatim_stats_total["sections_pruned"] = prune_stats.get("pruned", 0)
    verbatim_stats_total["hooks_backfilled"] = prune_stats.get("backfilled", 0)

    if not all_hooks:
        # Legacy path: craft from theme-wide candidates.
        if english_cues and any(needs_english(h.get("text", "")) for h in base["hooks"]):
            base["hooks"] = _swap_hooks_with_english_cues(base["hooks"], english_cues)
        if base.get("hooks"):
            crafted_all: list[dict[str, Any]] | None = None
            if api_key:
                try:
                    crafted_all = await _llm_craft_hooks(
                        hooks=base["hooks"],
                        theme_title=theme_title,
                        theme_summary=theme_summary,
                        api_key=api_key,
                        model=model,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hook analysis failed, using heuristic craft: %s", exc)
            base["hooks"] = crafted_all or heuristic_craft_hooks(
                base["hooks"], theme_title=theme_title
            )
            base["hooks"], vstats = enforce_non_verbatim_hooks(
                list(base.get("hooks") or []),
                cue_corpus + [str(h.get("original_text") or h.get("text") or "") for h in base["hooks"]],
                theme_title=theme_title,
            )
            for k, v in vstats.items():
                verbatim_stats_total[k] = verbatim_stats_total.get(k, 0) + v
        all_hooks = list(base.get("hooks") or [])

    # Final verbatim sweep across the full cue corpus.
    all_hooks, final_vstats = enforce_non_verbatim_hooks(
        all_hooks,
        cue_corpus,
        theme_title=theme_title,
    )
    for k, v in final_vstats.items():
        verbatim_stats_total[k] = verbatim_stats_total.get(k, 0) + v

    # Re-id chronologically and flatten topics for legacy consumers.
    all_hooks.sort(key=lambda r: float(r.get("start_sec") or 0))
    for i, h in enumerate(all_hooks[:_MAX_HOOKS]):
        h["id"] = f"hook_{i + 1}"
        h["verbatim"] = False
    base["hooks"] = all_hooks[:_MAX_HOOKS]
    base["topics"] = _flatten_topic_tree(topic_tree)[:_MAX_TOPICS]
    base["topic_tree"] = _reindex_topic_tree(topic_tree)[:_MAX_TOPICS]
    # Final structural proof: zero empty hook sections in the payload we ship.
    empty_sections = _count_empty_hook_sections(base["topic_tree"])
    if empty_sections:
        logger.warning("pruning %d empty-hook sections after reindex", empty_sections)
        base["topic_tree"] = _drop_empty_hook_sections(base["topic_tree"])
        base["topics"] = _flatten_topic_tree(base["topic_tree"])[:_MAX_TOPICS]
        # Rebuild flat hooks from tree so they stay in sync.
        rebuilt: list[dict[str, Any]] = []
        for t in base["topic_tree"]:
            rebuilt.extend(list(t.get("hooks") or []))
            for sub in t.get("subtopics") or []:
                rebuilt.extend(list(sub.get("hooks") or []))
        # Prefer chronologically unique by text
        seen_txt: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for h in sorted(rebuilt, key=lambda r: float(r.get("start_sec") or 0)):
            key = str(h.get("text") or "").strip().lower()
            if not key or key in seen_txt:
                continue
            seen_txt.add(key)
            uniq.append(h)
        for i, h in enumerate(uniq[:_MAX_HOOKS]):
            h["id"] = f"hook_{i + 1}"
        base["hooks"] = uniq[:_MAX_HOOKS]
    multi_range = sum(
        1 for t in base["topic_tree"] if len(t.get("time_ranges") or []) >= 2
    )
    empty_after = _count_empty_hook_sections(base["topic_tree"])
    base["transcript_meta"] = {
        "cue_count": cue_count,
        "transcript_chars": cue_chars,
        "chunks_used": chunks_used,
        "topic_source": topic_source,
        "topic_tree_count": len(base["topic_tree"]),
        "flat_topic_count": len(base["topics"]),
        "hook_count": len(base["hooks"]),
        "topics_with_multi_ranges": multi_range,
        "verbatim_guard": verbatim_stats_total,
        "empty_hook_sections": empty_after,
    }
    logger.info("carousel topic extract done: %s", base["transcript_meta"])

    base = await ensure_english_display_texts(
        base,
        english_cues=english_cues,
        api_key=api_key,
        model=model,
    )
    return base


async def _llm_craft_hooks(
    *,
    hooks: list[dict[str, Any]],
    theme_title: str,
    theme_summary: str,
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    """Rewrite spoken windows into punchy carousel hook lines (keep time spans).

    Display text is analysed/crafted from the transcript — never a verbatim cue dump.
    start_sec / end_sec stay aligned to the original spoken span for frame selection.
    """
    import asyncio

    from google import genai
    from google.genai import types

    if not hooks:
        return []

    payload = []
    for i, h in enumerate(hooks):
        payload.append(
            {
                "i": i,
                "spoken": str(h.get("text") or "").strip()[:400],
                "start_sec": h.get("start_sec"),
                "end_sec": h.get("end_sec"),
            }
        )

    prompt = (
        "You are an expert Instagram carousel copywriter. Analyse spoken transcript windows "
        "and derive GENUINE, high-engagement HOOK lines for 4:5 feed carousels.\n"
        "CRITICAL — hooks must be analysed, never pasted:\n"
        "- Derive a punchy hook FROM each spoken window — do NOT copy the transcript verbatim.\n"
        "- Rewrite into a complete, self-contained slide overlay (roughly 6–14 words).\n"
        "- Prefer scroll-stopping formulas: bold claim, curiosity gap, number, or counterintuitive take.\n"
        "- ONE idea per hook; readable on a phone at ~48pt; no mid-clause scraps or cue dumps.\n"
        "- Prefer natural English; translate meaning if spoken line is Hindi/Hinglish/other.\n"
        "- Keep the true claim/energy of what was said — sharpen for Instagram, do not invent facts.\n"
        "- Hooks must be DISTINCT from each other (no near-paraphrase twins).\n"
        "- Return one hook per input index; keep the same order.\n"
        f"Theme: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        "Return ONLY a JSON array of objects: "
        '{"i": number, "hook": "crafted English hook"}.\n\n'
        f"Spoken windows:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.45,
            response_mime_type="application/json",
        ),
    )
    raw = json.loads((resp.text or "").strip() or "[]")
    if not isinstance(raw, list):
        return []

    by_i: dict[int, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        hook = str(row.get("hook") or row.get("text") or "").strip()
        hook = " ".join(hook.split())
        if hook and 0 <= i < len(hooks):
            by_i[i] = hook[:280]

    if not by_i:
        return []

    out: list[dict[str, Any]] = []
    for i, h in enumerate(hooks):
        row = dict(h)
        crafted = by_i.get(i)
        if crafted:
            spoken = str(row.get("text") or "").strip()
            # If LLM echoed the transcript, fall back to a local punchy rewrite.
            if _nearly_verbatim(crafted, spoken):
                crafted = _heuristic_hook_line(spoken, theme_title=theme_title) or _trim_to_clause(
                    crafted, 16
                )
            row["original_text"] = row.get("original_text") or spoken
            row["text"] = crafted
            row["verbatim"] = False
            row["analysed"] = True
            row["contextual"] = True
        out.append(row)
    return out


def heuristic_craft_hooks(
    hooks: list[dict[str, Any]],
    *,
    theme_title: str = "",
) -> list[dict[str, Any]]:
    """Local punchy rewrite when Gemini craft is unavailable — never ship raw cue dumps."""
    out: list[dict[str, Any]] = []
    corpus = [str(h.get("text") or "") for h in hooks if isinstance(h, dict)]
    for h in hooks:
        row = dict(h)
        spoken = str(row.get("text") or "").strip()
        if not spoken:
            continue
        crafted = _heuristic_hook_line(spoken, theme_title=theme_title)
        if not crafted or is_verbatim_transcript_leak(crafted, [spoken, *corpus]):
            crafted = _force_non_verbatim_hook(spoken, theme_title=theme_title)
        # Last resort still non-verbatim by construction.
        if not crafted:
            crafted = _force_non_verbatim_hook(spoken, theme_title=theme_title or "the talk")
        row["original_text"] = row.get("original_text") or spoken
        row["text"] = crafted
        row["verbatim"] = False
        row["analysed"] = True
        row["contextual"] = True
        out.append(row)
    guarded, _stats = enforce_non_verbatim_hooks(out, corpus, theme_title=theme_title)
    return guarded


def _heuristic_hook_line(spoken: str, *, theme_title: str = "") -> str:
    """Derive a short carousel hook from a spoken window without an LLM."""
    text = " ".join((spoken or "").split()).strip().strip("\"'")
    if not text:
        return ""
    # Prefer first complete sentence / clause.
    m = re.search(r"^(.+?[.!?])(?:\s|$)", text)
    clause = (m.group(1) if m else text).strip()
    words = clause.split()
    if len(words) > 16:
        clause = _trim_to_clause(clause, 16)
        words = clause.split()
    # Drop filler openers that make dumps feel like captions.
    clause = re.sub(
        r"^(?:so|and|but|well|um+|uh+|you know|like|okay|ok|right)[,:\s]+",
        "",
        clause,
        flags=re.I,
    ).strip()
    if not clause:
        clause = " ".join(words[:12])
    # Title-case lightly only when the line is a short claim.
    if 4 <= len(clause.split()) <= 10 and theme_title and theme_title.lower() in clause.lower():
        return clause[:280]
    if not clause[0:1].isupper() and clause:
        clause = clause[0].upper() + clause[1:]
    return clause[:280]


def _nearly_verbatim(crafted: str, spoken: str, *, threshold: float = 0.82) -> bool:
    """True when crafted text largely copies the spoken line token-for-token."""
    a = {t for t in re.findall(r"[a-z0-9']+", (crafted or "").lower()) if len(t) > 2}
    b = {t for t in re.findall(r"[a-z0-9']+", (spoken or "").lower()) if len(t) > 2}
    if not a or not b:
        return False
    overlap = len(a & b) / max(len(a), 1)
    return overlap >= threshold and abs(len(a) - len(b)) <= max(3, len(b) // 4)


def _normalize_hook_cmp(text: str) -> str:
    """Lowercase alphanumerics only — for exact/near transcript matching."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def is_verbatim_transcript_leak(
    hook: str,
    corpus_texts: list[str],
    *,
    token_threshold: float = 0.78,
    lcs_ratio: float = 0.72,
) -> bool:
    """True if hook is exact/near-exact copy or long substring of any transcript cue/window."""
    from difflib import SequenceMatcher

    hook = " ".join((hook or "").split()).strip()
    if not hook:
        return True
    hn = _normalize_hook_cmp(hook)
    if len(hn) < 10:
        return False
    hw = {t for t in re.findall(r"[a-z0-9']+", hook.lower()) if len(t) > 2}
    for cue in corpus_texts:
        cue_s = " ".join((cue or "").split()).strip()
        if not cue_s:
            continue
        cn = _normalize_hook_cmp(cue_s)
        if not cn:
            continue
        # Exact or containment on normalized strings
        if hn == cn:
            return True
        if len(hn) >= 14 and (hn in cn or (len(cn) >= 14 and cn in hn)):
            return True
        # High token overlap with similar length (near-paraphrase dump)
        cw = {t for t in re.findall(r"[a-z0-9']+", cue_s.lower()) if len(t) > 2}
        if hw and cw:
            overlap = len(hw & cw) / max(len(hw), 1)
            if overlap >= token_threshold and abs(len(hw) - len(cw)) <= max(3, len(cw) // 4):
                return True
        # Long common substring on normalized forms
        if len(hn) >= 16 and len(cn) >= 16:
            lcs = SequenceMatcher(None, hn, cn).find_longest_match(0, len(hn), 0, len(cn)).size
            if lcs >= max(16, int(lcs_ratio * len(hn))):
                return True
    return False


def _force_non_verbatim_hook(spoken: str, *, theme_title: str = "") -> str:
    """Aggressively rewrite a spoken window into a non-verbatim carousel claim."""
    spoken = " ".join((spoken or "").split()).strip()
    theme_bit = (theme_title or "this story").strip()[:48] or "this story"
    base = _heuristic_hook_line(spoken, theme_title=theme_title)
    if base and not is_verbatim_transcript_leak(base, [spoken]):
        return base
    # Pull at most 2 content words so we never reassemble the cue.
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "have", "been",
        "they", "their", "about", "into", "every", "company", "largest",
    }
    words = [
        w for w in re.findall(r"[A-Za-z][A-Za-z0-9']+", spoken)
        if len(w) > 3 and w.lower() not in stop
    ][:2]
    if words:
        spark = " / ".join(words)
        candidate = f"The hidden pattern behind {spark}"
        if not is_verbatim_transcript_leak(candidate, [spoken]):
            return candidate[:280]
    # Guaranteed divergence from any cue (theme-framed, no spoken tokens required).
    return f"What most people miss about {theme_bit}"[:280]


def enforce_non_verbatim_hooks(
    hooks: list[dict[str, Any]],
    corpus_texts: list[str],
    *,
    theme_title: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Reject or rewrite hooks that leak transcript verbatim (LLM + heuristic paths)."""
    kept: list[dict[str, Any]] = []
    stats = {"checked": 0, "rejected_verbatim": 0, "rewritten": 0, "dropped": 0}
    corpus = [c for c in corpus_texts if (c or "").strip()]
    for h in hooks:
        if not isinstance(h, dict):
            continue
        stats["checked"] += 1
        text = str(h.get("text") or "").strip()
        spoken = str(h.get("original_text") or "").strip()
        local_corpus = list(corpus)
        if spoken and spoken != text:
            local_corpus.append(spoken)

        if text and not is_verbatim_transcript_leak(text, local_corpus):
            row = dict(h)
            row["verbatim"] = False
            row["analysed"] = True
            kept.append(row)
            continue

        stats["rejected_verbatim"] += 1
        source = spoken or text
        rewritten = _force_non_verbatim_hook(source, theme_title=theme_title)
        if rewritten and not is_verbatim_transcript_leak(rewritten, local_corpus + [source]):
            row = dict(h)
            row["original_text"] = source
            row["text"] = rewritten
            row["verbatim"] = False
            row["analysed"] = True
            row["verbatim_guard"] = "rewritten"
            kept.append(row)
            stats["rewritten"] += 1
            logger.info("verbatim guard rewrote hook: %r → %r", text[:70], rewritten[:70])
        else:
            stats["dropped"] += 1
            logger.info("verbatim guard dropped hook: %r", text[:90])
    return kept, stats


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


def _expand_thin_topic_window(
    all_cues: list[tuple[float, float | None, str]],
    window: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    min_cues: int = 16,
    min_chars: int = 1_200,
) -> tuple[list[tuple[float, float | None, str]], float, float | None]:
    """Grow a catastrophically thin theme window so topic extraction can read the talk.

    Only expands when the slice is nearly empty (bad timestamps / sparse English track).
    Healthy theme windows are left unchanged so topics stay on-theme.
    """
    usable = [(s, e, t) for s, e, t in all_cues if (t or "").strip()]
    if not usable:
        return window, start_sec, end_sec

    def _chars(rows: list[tuple[float, float | None, str]]) -> int:
        return len(compact_transcript(rows, max_chars=200_000))

    cur = list(window) if window else _cues_in_range(usable, start_sec, end_sec)
    cur_start = float(start_sec or 0)
    cur_end = float(end_sec) if end_sec is not None else None
    if len(cur) >= min_cues and _chars(cur) >= min_chars:
        return cur, cur_start, cur_end

    # Index bounds in the full cue list
    starts = [float(s) for s, _, _ in usable]
    # Find leftmost cue index at/after theme start
    lo = 0
    for i, s in enumerate(starts):
        if s >= cur_start - 0.05:
            lo = i
            break
    hi = len(usable) - 1
    if cur_end is not None:
        for i, s in enumerate(starts):
            if s <= float(cur_end) + 0.25:
                hi = i
            else:
                break
    # Expand outward until thresholds met or we hit video edges
    while (hi - lo + 1) < len(usable) and (
        (hi - lo + 1) < min_cues or _chars(usable[lo : hi + 1]) < min_chars
    ):
        grew = False
        if hi + 1 < len(usable):
            hi += 1
            grew = True
        if (hi - lo + 1) < min_cues or _chars(usable[lo : hi + 1]) < min_chars:
            if lo > 0:
                lo -= 1
                grew = True
        if not grew:
            break

    expanded = usable[lo : hi + 1]
    new_start = float(expanded[0][0]) if expanded else cur_start
    new_end = (
        float(expanded[-1][1] if expanded[-1][1] is not None else expanded[-1][0])
        if expanded
        else cur_end
    )
    logger.info(
        "expanded thin topic window: cues %d→%d chars %d→%d span %.1f–%s → %.1f–%.1f",
        len(window),
        len(expanded),
        _chars(window),
        _chars(expanded),
        cur_start,
        f"{cur_end:.1f}" if cur_end is not None else "end",
        new_start,
        new_end,
    )
    return expanded, new_start, new_end


def _chunk_cues_for_topics(
    cues: list[tuple[float, float | None, str]],
    *,
    max_chars: int = _TOPIC_CHUNK_CHARS,
    overlap_cues: int = _TOPIC_CHUNK_OVERLAP_CUES,
) -> list[list[tuple[float, float | None, str]]]:
    """Split timed cues into overlapping chunks so Gemini can read the full talk."""
    usable = [(s, e, t) for s, e, t in cues if (t or "").strip()]
    if not usable:
        return []
    chunks: list[list[tuple[float, float | None, str]]] = []
    current: list[tuple[float, float | None, str]] = []
    total = 0
    for cue in usable:
        line = format_cue_line(cue[0], cue[1], cue[2])
        line_len = len(line) + 1
        if current and total + line_len > max_chars:
            chunks.append(current)
            overlap = current[-overlap_cues:] if overlap_cues > 0 else []
            current = list(overlap)
            total = 0
            for oc in current:
                total += len(format_cue_line(oc[0], oc[1], oc[2])) + 1
        current.append(cue)
        total += line_len
    if current:
        chunks.append(current)
    return chunks


def _merge_topic_trees(parts: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge chunk topic trees chronologically; drop only near-duplicate titles."""
    merged: list[dict[str, Any]] = []
    for part in parts:
        merged.extend(part)
    merged.sort(key=lambda t: float(t.get("start_sec") or 0))
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    for row in merged:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        toks = _token_set(text)
        if any(_jaccard(toks, existing) >= 0.62 for existing in kept_tokens):
            continue
        kept.append(row)
        kept_tokens.append(toks)
        if len(kept) >= _MAX_TOPICS:
            break
    return kept


def _condense_transcript_outline(
    cues: list[tuple[float, float | None, str]],
    *,
    max_chars: int = 9_000,
) -> str:
    """Evenly sample timed cues into a condensed full-talk outline for global synthesis."""
    usable = [(s, e, t) for s, e, t in cues if (t or "").strip()]
    if not usable:
        return ""
    full = compact_transcript(usable, max_chars=max_chars)
    if len(full) <= max_chars:
        return full
    target_lines = max(40, max_chars // 90)
    step = max(1, len(usable) // target_lines)
    sampled = usable[::step]
    # Always keep first/last beats for arc coverage
    if usable[0] not in sampled:
        sampled = [usable[0], *sampled]
    if usable[-1] not in sampled:
        sampled = [*sampled, usable[-1]]
    return compact_transcript(sampled, max_chars=max_chars)


def _cues_for_topic_ranges(
    cues: list[tuple[float, float | None, str]],
    topic: dict[str, Any],
    *,
    fallback_start: float,
    fallback_end: float | None,
) -> list[tuple[float, float | None, str]]:
    """Gather cues for a topic, supporting multiple non-contiguous time_ranges."""
    ranges = topic.get("time_ranges") if isinstance(topic.get("time_ranges"), list) else []
    collected: list[tuple[float, float | None, str]] = []
    seen_starts: set[float] = set()
    for raw in ranges:
        if not isinstance(raw, dict):
            continue
        rs = _as_float(raw.get("start_sec", raw.get("start")))
        re_ = _as_float(raw.get("end_sec", raw.get("end")))
        if rs is None:
            continue
        for cue in _cues_in_range(cues, float(rs), re_):
            key = float(cue[0])
            if key in seen_starts:
                continue
            seen_starts.add(key)
            collected.append(cue)
    if collected:
        collected.sort(key=lambda c: float(c[0]))
        return collected
    start = _as_float(topic.get("start_sec"))
    end = _as_float(topic.get("end_sec"))
    return _cues_in_range(
        cues,
        float(start if start is not None else fallback_start),
        end if end is not None else fallback_end,
    )


async def _llm_synthesize_global_topics(
    *,
    candidates: list[dict[str, Any]],
    outline: str,
    theme_title: str,
    theme_summary: str,
    search_entity: str | None,
    api_key: str,
    model: str,
    theme_start: float,
    theme_end: float | None,
) -> list[dict[str, Any]]:
    """Global pass: turn fragmentary chunk topics into cohesive transcript-spanning threads."""
    import asyncio

    from google import genai
    from google.genai import types

    if not outline.strip() or not candidates:
        return candidates

    cand_payload = []
    for c in candidates[:40]:
        cand_payload.append(
            {
                "title": c.get("text"),
                "start_sec": c.get("start_sec"),
                "end_sec": c.get("end_sec"),
                "explanation": c.get("explanation"),
                "subtopics": [
                    {
                        "title": s.get("text"),
                        "start_sec": s.get("start_sec"),
                        "end_sec": s.get("end_sec"),
                        "explanation": s.get("explanation"),
                    }
                    for s in (c.get("subtopics") or [])[:4]
                    if isinstance(s, dict)
                ],
            }
        )

    entity = (search_entity or "").strip()
    prompt = (
        "You synthesize COHESIVE, transcript-spanning TOPICS for a video carousel.\n"
        "You are given (1) a condensed timed outline of the FULL talk and (2) candidate "
        "local topics extracted from chunks (these may be fragmentary single-moment labels).\n\n"
        "Goal: produce parent topics that are COHERENT THREADS across the talk — narrative arcs, "
        "recurring ideas, and how the speaker's direction evolves — NOT isolated moment tags.\n\n"
        "Hard rules:\n"
        "- Output 5–12 parent topics (keep good granularity; do NOT collapse to 2–3 vague umbrellas)\n"
        "- Each topic = one coherent thread that can recur; use time_ranges for EVERY span where "
        "that thread appears (non-contiguous ranges are encouraged when the speaker returns to it)\n"
        "- Also set start_sec / end_sec to the earliest start and latest end across its ranges\n"
        "- subtopics: 1–4 local direction-shift beats nested under the parent thread "
        "(reuse/refine chunk candidates where they fit)\n"
        "- Titles: 2–8 words, concrete, grounded in the outline — not generic chapter names\n"
        "- explanation: 1 sentence on how this thread develops across the talk\n"
        "- Prefer merging duplicate local candidates into one spanning thread\n"
        "- Do NOT invent facts beyond the outline/candidates\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Window: {theme_start}s – {theme_end if theme_end is not None else 'end'}s\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of objects:\n"
        '{"title":"...","start_sec":0,"end_sec":10,'
        '"time_ranges":[{"start_sec":0,"end_sec":20},{"start_sec":120,"end_sec":150}],'
        '"explanation":"...",'
        '"subtopics":[{"title":"...","start_sec":0,"end_sec":10,"explanation":"..."}]}\n\n'
        f"Candidate local topics:\n{json.dumps(cand_payload, ensure_ascii=False)}\n\n"
        f"Condensed full-talk outline (READ FOR GLOBAL ARC):\n{outline}"
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
    synthesized = _parse_topic_tree_json(
        (resp.text or "").strip(),
        theme_start=theme_start,
        theme_end=theme_end,
    )
    return synthesized if synthesized else candidates


async def _llm_topic_tree_from_cues(
    *,
    cues: list[tuple[float, float | None, str]],
    theme_title: str,
    theme_summary: str,
    search_entity: str | None,
    api_key: str,
    model: str,
    theme_start: float = 0.0,
    theme_end: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Chunk-read the talk, then globally synthesize cohesive spanning topics."""
    chunks = _chunk_cues_for_topics(cues)
    if not chunks:
        return [], 0
    parts: list[list[dict[str, Any]]] = []
    for idx, chunk in enumerate(chunks):
        transcript = compact_transcript(chunk, max_chars=_TOPIC_CHUNK_CHARS + 2_000)
        c_start = float(chunk[0][0])
        c_end_raw = chunk[-1][1] if chunk[-1][1] is not None else chunk[-1][0]
        c_end = float(c_end_raw)
        logger.info(
            "carousel topic chunk %d/%d: cues=%d chars=%d span=%.1f–%.1f",
            idx + 1,
            len(chunks),
            len(chunk),
            len(transcript),
            c_start,
            c_end,
        )
        part = await _llm_topic_tree_from_theme(
            theme_title=theme_title,
            theme_summary=theme_summary,
            transcript=transcript,
            search_entity=search_entity,
            api_key=api_key,
            model=model,
            theme_start=c_start,
            theme_end=c_end,
            chunk_index=idx,
            chunk_count=len(chunks),
        )
        if part:
            parts.append(part)
    if not parts:
        return [], len(chunks)

    candidates = parts[0] if len(parts) == 1 else _merge_topic_trees(parts)
    outline = _condense_transcript_outline(cues)
    try:
        synthesized = await _llm_synthesize_global_topics(
            candidates=candidates,
            outline=outline,
            theme_title=theme_title,
            theme_summary=theme_summary,
            search_entity=search_entity,
            api_key=api_key,
            model=model,
            theme_start=theme_start,
            theme_end=theme_end,
        )
        if synthesized:
            logger.info(
                "global topic synthesis: candidates=%d → cohesive=%d (outline_chars=%d)",
                len(candidates),
                len(synthesized),
                len(outline),
            )
            return synthesized[:_MAX_TOPICS], len(chunks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("global topic synthesis failed, using chunk merge: %s", exc)
    return candidates[:_MAX_TOPICS], len(chunks)


async def _llm_topic_tree_from_theme(
    *,
    theme_title: str,
    theme_summary: str,
    transcript: str,
    search_entity: str | None,
    api_key: str,
    model: str,
    theme_start: float = 0.0,
    theme_end: float | None = None,
    chunk_index: int = 0,
    chunk_count: int = 1,
) -> list[dict[str, Any]]:
    """Infer cohesive topic clusters (+ optional subtopics) from one transcript chunk."""
    import asyncio

    from google import genai
    from google.genai import types

    entity = (search_entity or "").strip()
    chunk_note = (
        f"This is chunk {chunk_index + 1} of {chunk_count} of the same talk. "
        "Extract EVERY distinct direction shift in THIS chunk — do not summarize the whole video "
        "into a handful of vague themes.\n"
        if chunk_count > 1
        else ""
    )
    prompt = (
        "You carefully READ the timed transcript below and extract COHESIVE TOPICS.\n"
        "A cohesive topic = a real thematic cluster where the speaker takes a clear direction "
        "or develops one idea for a stretch of time (a narrative chapter), NOT keyword tags, "
        "NOT scattered buzzwords, NOT one vague umbrella for the whole talk.\n\n"
        f"{chunk_note}"
        "These are LOCAL candidate beats for a later global synthesis pass. Still be specific.\n"
        "Flow: Topics → optional Subtopics (no hooks here).\n"
        "Hard rules:\n"
        "- Extract MORE candidates when the speaker pivots: aim for 5–10 top-level topics in this "
        "chunk when the talk supports it (minimum 4 if the chunk is substantive). "
        "Do NOT collapse a long discussion into 2–3 generic labels.\n"
        "- Follow the transcript chronologically; each topic must map to real cue timestamps\n"
        "- Each topic title: 2–8 words, natural English, ONE concrete idea from what was said\n"
        "- start_sec / end_sec must cover that direction using the cue times in the transcript\n"
        "- explanation: 1 sentence grounded in the spoken content (what they actually develop)\n"
        "- subtopics: 0–3 nested beats ONLY when the speaker subdivides the same direction\n"
        "- Distinct topics only (no near-duplicate labels), but keep adjacent distinct angles\n"
        "- READ the lines — titles must reflect specific claims/stories in the transcript, "
        "not invent generic chapter names that could fit any video\n"
        "- Do NOT paste dialogue as titles; do NOT invent facts beyond the transcript\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Chunk window: {theme_start}s – {theme_end if theme_end is not None else 'end'}s\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of objects:\n"
        '{"title":"...","start_sec":0,"end_sec":10,"explanation":"...",'
        '"subtopics":[{"title":"...","start_sec":0,"end_sec":5,"explanation":"..."}]}\n\n'
        f"Timed transcript (READ ALL OF IT):\n{transcript}"
    )
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.35,
            response_mime_type="application/json",
        ),
    )
    return _parse_topic_tree_json(
        (resp.text or "").strip(),
        theme_start=theme_start,
        theme_end=theme_end,
    )


def _parse_topic_tree_json(
    text: str,
    *,
    theme_start: float,
    theme_end: float | None,
) -> list[dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("text") or row.get("topic") or "").strip()
        if not title or len(title.split()) > 12:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        start = _as_float(row.get("start_sec", row.get("start")))
        end = _as_float(row.get("end_sec", row.get("end")))
        time_ranges: list[dict[str, float]] = []
        raw_ranges = row.get("time_ranges") if isinstance(row.get("time_ranges"), list) else []
        for tr in raw_ranges[:8]:
            if not isinstance(tr, dict):
                continue
            rs = _as_float(tr.get("start_sec", tr.get("start")))
            re_ = _as_float(tr.get("end_sec", tr.get("end")))
            if rs is None:
                continue
            time_ranges.append(
                {
                    "start_sec": float(rs),
                    "end_sec": float(re_) if re_ is not None else float(rs),
                }
            )
        if time_ranges:
            start = min(r["start_sec"] for r in time_ranges)
            end = max(r["end_sec"] for r in time_ranges)
        if start is None:
            start = float(theme_start or 0)
        if end is None and theme_end is not None:
            end = float(theme_end)
        if not time_ranges and start is not None:
            time_ranges = [
                {
                    "start_sec": float(start),
                    "end_sec": float(end) if end is not None else float(start),
                }
            ]
        expl = str(row.get("explanation") or row.get("summary") or "").strip()[:400]
        subs_raw = row.get("subtopics") if isinstance(row.get("subtopics"), list) else []
        subtopics: list[dict[str, Any]] = []
        sub_seen: set[str] = set()
        for j, sub in enumerate(subs_raw[:4]):
            if not isinstance(sub, dict):
                continue
            st = str(sub.get("title") or sub.get("text") or "").strip()
            if not st or st.lower() in sub_seen or st.lower() == key:
                continue
            sub_seen.add(st.lower())
            ss = _as_float(sub.get("start_sec", sub.get("start")))
            se = _as_float(sub.get("end_sec", sub.get("end")))
            subtopics.append(
                {
                    "id": f"topic_{i + 1}_sub_{j + 1}",
                    "text": st[:120],
                    "start_sec": float(ss if ss is not None else start),
                    "end_sec": float(se) if se is not None else (float(end) if end is not None else None),
                    "explanation": str(sub.get("explanation") or "").strip()[:300],
                    "hooks": [],
                }
            )
        out.append(
            {
                "id": f"topic_{i + 1}",
                "text": title[:120],
                "start_sec": float(start),
                "end_sec": float(end) if end is not None else None,
                "time_ranges": time_ranges,
                "explanation": expl,
                "verbatim": False,
                "generated": True,
                "subtopics": subtopics,
                "hooks": [],
            }
        )
        if len(out) >= _MAX_TOPICS:
            break
    return out


def _flat_topics_to_tree(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tree: list[dict[str, Any]] = []
    for i, t in enumerate(topics[:_MAX_TOPICS]):
        if not isinstance(t, dict):
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        tree.append(
            {
                "id": t.get("id") or f"topic_{i + 1}",
                "text": text,
                "start_sec": float(t.get("start_sec") or 0),
                "end_sec": t.get("end_sec"),
                "explanation": str(t.get("explanation") or ""),
                "verbatim": False,
                "generated": True,
                "subtopics": [],
                "hooks": [],
            }
        )
    return tree


def _emergency_hook_candidates(
    cues_or_stitched: list[Any],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Last-resort hook seeds from the longest spoken lines in a window."""
    rows: list[dict[str, Any]] = []
    for item in cues_or_stitched or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            start = float(item.get("start_sec") or 0)
            end = item.get("end_sec")
        elif isinstance(item, (tuple, list)) and len(item) >= 3:
            start = float(item[0] or 0)
            end = item[1]
            text = str(item[2] or "").strip()
        else:
            continue
        if len(text.split()) < 4:
            continue
        rows.append(
            {
                "id": f"emerg_{len(rows) + 1}",
                "text": text if len(text.split()) <= 32 else _trim_to_clause(text, 32),
                "start_sec": start,
                "end_sec": end,
                "verbatim": True,
                "contextual": True,
            }
        )
    rows.sort(key=lambda r: len(str(r.get("text") or "").split()), reverse=True)
    return rows[:limit]


def _count_empty_hook_sections(tree: list[dict[str, Any]]) -> int:
    n = 0
    for t in tree or []:
        if not list(t.get("hooks") or []):
            n += 1
        for sub in t.get("subtopics") or []:
            if not list(sub.get("hooks") or []):
                n += 1
    return n


def _drop_empty_hook_sections(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove topics/subtopics that still have zero hooks (never ship empty sections)."""
    out: list[dict[str, Any]] = []
    for t in tree or []:
        row = dict(t)
        subs = []
        for sub in row.get("subtopics") or []:
            if list(sub.get("hooks") or []):
                subs.append(sub)
        row["subtopics"] = subs
        parent_hooks = list(row.get("hooks") or [])
        if not parent_hooks and not subs:
            continue
        if not parent_hooks and subs:
            # Promote first subtopic hook up so the parent section isn't empty.
            first = list(subs[0].get("hooks") or [])[:1]
            row["hooks"] = [dict(h) for h in first]
        out.append(row)
    return out


def _ensure_hooks_on_every_section(
    topic_tree: list[dict[str, Any]],
    *,
    all_hooks: list[dict[str, Any]],
    cues: list[Any],
    theme_title: str,
    cue_corpus: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Backfill or drop so every kept topic/subtopic has ≥1 hook."""
    stats = {"pruned": 0, "backfilled": 0}
    kept: list[dict[str, Any]] = []
    hooks_out = list(all_hooks)

    for topic in topic_tree:
        row = dict(topic)
        topic_hooks = list(row.get("hooks") or [])
        if not topic_hooks:
            window = _cues_for_topic_ranges(
                cues,
                row,
                fallback_start=float(row.get("start_sec") or 0),
                fallback_end=row.get("end_sec"),
            )
            stitched = _stitch_complete_utterances(window) if window else []
            emerg = _emergency_hook_candidates(stitched or window, limit=2)
            if emerg:
                crafted = heuristic_craft_hooks(
                    emerg, theme_title=str(row.get("text") or theme_title)
                )
                crafted, _ = enforce_non_verbatim_hooks(
                    crafted,
                    cue_corpus + [str(c.get("text") or "") for c in emerg],
                    theme_title=str(row.get("text") or theme_title),
                )
                for h in crafted[:1]:
                    h["topic_id"] = row.get("id")
                    h["topic_text"] = row.get("text")
                    topic_hooks.append(h)
                    hooks_out.append(h)
                    stats["backfilled"] += 1
        row["hooks"] = topic_hooks[:2]

        # Subtopics: keep only those with hooks; backfill from their window when possible.
        kept_subs: list[dict[str, Any]] = []
        for sub in row.get("subtopics") or []:
            s = dict(sub)
            sub_hooks = list(s.get("hooks") or [])
            if not sub_hooks:
                # Prefer parent hooks that already point at this subtopic.
                linked = [
                    h
                    for h in topic_hooks
                    if (h.get("subtopic_id") and h.get("subtopic_id") == s.get("id"))
                    or (
                        str(h.get("subtopic_text") or "").strip().lower()
                        == str(s.get("text") or "").strip().lower()
                    )
                ]
                if linked:
                    sub_hooks = [dict(linked[0])]
                    stats["backfilled"] += 1
                else:
                    sw = _cues_for_topic_ranges(
                        cues,
                        {
                            "start_sec": s.get("start_sec"),
                            "end_sec": s.get("end_sec"),
                            "time_ranges": [
                                {
                                    "start_sec": float(s.get("start_sec") or 0),
                                    "end_sec": s.get("end_sec"),
                                }
                            ],
                        },
                        fallback_start=float(s.get("start_sec") or 0),
                        fallback_end=s.get("end_sec"),
                    )
                    emerg = _emergency_hook_candidates(
                        _stitch_complete_utterances(sw) if sw else sw, limit=1
                    )
                    if emerg:
                        crafted = heuristic_craft_hooks(
                            emerg, theme_title=str(s.get("text") or theme_title)
                        )
                        crafted, _ = enforce_non_verbatim_hooks(
                            crafted,
                            cue_corpus + [str(c.get("text") or "") for c in emerg],
                            theme_title=str(s.get("text") or theme_title),
                        )
                        if crafted:
                            h = crafted[0]
                            h["topic_id"] = row.get("id")
                            h["topic_text"] = row.get("text")
                            h["subtopic_id"] = s.get("id")
                            h["subtopic_text"] = s.get("text")
                            sub_hooks = [h]
                            hooks_out.append(h)
                            stats["backfilled"] += 1
            if not sub_hooks:
                stats["pruned"] += 1
                continue
            s["hooks"] = sub_hooks[:2]
            kept_subs.append(s)
        row["subtopics"] = kept_subs

        if not row["hooks"] and not kept_subs:
            stats["pruned"] += 1
            continue
        if not row["hooks"] and kept_subs:
            # Parent must show ≥1 hook — borrow from first subtopic.
            borrow = list(kept_subs[0].get("hooks") or [])[:1]
            row["hooks"] = [dict(h) for h in borrow]
            stats["backfilled"] += 1
        kept.append(row)

    return kept, hooks_out, stats


def _flatten_topic_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for t in tree:
        flat.append(
            {
                "id": t.get("id"),
                "text": t.get("text"),
                "start_sec": t.get("start_sec"),
                "end_sec": t.get("end_sec"),
                "time_ranges": t.get("time_ranges") or [],
                "explanation": t.get("explanation"),
                "verbatim": False,
                "generated": True,
                "has_subtopics": bool(t.get("subtopics")),
            }
        )
        for sub in t.get("subtopics") or []:
            flat.append(
                {
                    "id": sub.get("id"),
                    "text": sub.get("text"),
                    "start_sec": sub.get("start_sec"),
                    "end_sec": sub.get("end_sec"),
                    "explanation": sub.get("explanation"),
                    "verbatim": False,
                    "generated": True,
                    "parent_topic_id": t.get("id"),
                    "is_subtopic": True,
                }
            )
    return flat


def _reindex_topic_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, t in enumerate(tree):
        row = dict(t)
        row["id"] = f"topic_{i + 1}"
        ranges = row.get("time_ranges") if isinstance(row.get("time_ranges"), list) else []
        if not ranges and row.get("start_sec") is not None:
            ranges = [
                {
                    "start_sec": float(row.get("start_sec") or 0),
                    "end_sec": float(row["end_sec"])
                    if row.get("end_sec") is not None
                    else float(row.get("start_sec") or 0),
                }
            ]
        row["time_ranges"] = ranges
        subs = []
        for j, sub in enumerate(row.get("subtopics") or []):
            s = dict(sub)
            s["id"] = f"topic_{i + 1}_sub_{j + 1}"
            s.setdefault("hooks", [])
            subs.append(s)
        row["subtopics"] = subs
        row.setdefault("hooks", [])
        out.append(row)
    return out


def _best_subtopic_for_hook(
    hook: dict[str, Any],
    subtopics: list[dict[str, Any]],
) -> dict[str, Any] | None:
    hs = float(hook.get("start_sec") or 0)
    best: dict[str, Any] | None = None
    best_dist = 1e18
    for sub in subtopics:
        ss = float(sub.get("start_sec") or 0)
        se = sub.get("end_sec")
        if se is not None and ss <= hs <= float(se) + 0.5:
            return sub
        dist = abs(hs - ss)
        if dist < best_dist:
            best_dist = dist
            best = sub
    return best if best_dist < 45 else None


async def _llm_hooks_for_singular_topic(
    *,
    hooks: list[dict[str, Any]],
    topic_title: str,
    topic_explanation: str,
    theme_title: str,
    theme_summary: str,
    used_angles: list[str],
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    """Craft Instagram hooks for ONE topic only — never reuse prior topics' angles."""
    import asyncio

    from google import genai
    from google.genai import types

    payload = []
    for i, h in enumerate(hooks):
        payload.append(
            {
                "i": i,
                "spoken": str(h.get("text") or "").strip()[:400],
                "start_sec": h.get("start_sec"),
                "end_sec": h.get("end_sec"),
            }
        )
    used = [u for u in used_angles if (u or "").strip()][:24]
    prompt = (
        "You are an expert Instagram carousel copywriter.\n"
        "Generate punchy HOOK lines for ONE singular topic only.\n"
        "CRITICAL:\n"
        f"- This batch is ONLY about the topic: “{topic_title}”.\n"
        f"- Topic context: {topic_explanation or '(from transcript window)'}\n"
        "- Do NOT invent hooks about other topics. Do NOT reuse or paraphrase any "
        "already-used hook angles listed below.\n"
        "- Derive each hook FROM the spoken window — NEVER paste or lightly trim the transcript.\n"
        "- FORBIDDEN: returning the spoken line unchanged, a substring of it, or a near-copy "
        "that only drops filler words. You MUST rewrite into a fresh Instagram hook.\n"
        "- 6–14 words; one idea; scroll-stopping (claim, curiosity, number, or counterintuitive take).\n"
        "- Natural English; keep the true claim of what was said, but change the wording.\n"
        "- Prefer returning 1–2 strongest hooks (you may skip weak indices).\n"
        f"Parent theme: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Already-used hook angles (FORBIDDEN to overlap): {json.dumps(used, ensure_ascii=False)}\n"
        'Return ONLY JSON array: {"i": number, "hook": "crafted English hook"}.\n\n'
        f"Spoken windows:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.45,
            response_mime_type="application/json",
        ),
    )
    raw = json.loads((resp.text or "").strip() or "[]")
    if not isinstance(raw, list):
        return []
    by_i: dict[int, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        hook = str(row.get("hook") or "").strip()
        if hook:
            by_i[i] = hook[:200]

    out: list[dict[str, Any]] = []
    used_norm = {" ".join(u.lower().split()) for u in used}
    for i, h in enumerate(hooks):
        crafted = by_i.get(i)
        if not crafted:
            continue
        norm = " ".join(crafted.lower().split())
        if norm in used_norm:
            continue
        if any(_jaccard(_token_set(crafted), _token_set(u)) >= 0.55 for u in used):
            continue
        spoken = str(h.get("text") or "")
        if _nearly_verbatim(crafted, spoken):
            continue
        row = dict(h)
        row["original_text"] = spoken
        row["text"] = crafted
        row["verbatim"] = False
        row["analysed"] = True
        row["topic_id"] = h.get("topic_id")
        row["topic_text"] = topic_title
        out.append(row)
        used_norm.add(norm)
        if len(out) >= 2:
            break
    return out


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
        "You carefully READ the timed transcript and infer COHESIVE TOPICS — real thematic "
        "clusters where the speaker takes a direction — for one selected theme.\n"
        "Topics are thematic chapter titles grounded in what was said — NOT transcript quotes, "
        "NOT vague umbrellas that ignore most of the talk.\n"
        "Rules:\n"
        "- Aim for 5–10 topics when the transcript supports it (order = chronology)\n"
        "- Each topic: 2–8 words; ONE concrete idea; natural English\n"
        "- Cluster by meaning/direction pivots in the transcript\n"
        "- No incomplete thoughts; no near-duplicates "
        "(e.g. 'Student-First Philosophy' ≈ 'Student-Centric Decisions' — keep one)\n"
        "- Keep distinct adjacent angles; do not collapse the talk into 2–3 generic labels\n"
        f"Theme title: {theme_title}\n"
        f"Theme summary: {theme_summary}\n"
        f"Search entity: {entity or '(none)'}\n"
        "Return ONLY a JSON array of strings.\n\n"
        f"Theme transcript (READ IT):\n{transcript[:_TOPIC_CHUNK_CHARS]}"
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


def _token_set(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "that", "this", "it", "as", "at", "by", "from", "be", "you", "your", "our", "we",
        "first", "centric", "based", "driven", "oriented", "focused", "approach", "philosophy",
        "decisions", "decision", "mindset", "way", "ways",
    }
    out: set[str] = set()
    for raw in re.findall(r"[a-z0-9']+", (text or "").lower()):
        if raw in stop or len(raw) <= 2:
            continue
        # Light stemming so "students" ≈ "student".
        token = raw[:-1] if len(raw) > 4 and raw.endswith("s") and not raw.endswith("ss") else raw
        out.add(token)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def heuristic_topic_dedupe(
    topics: list[dict[str, Any]],
    *,
    threshold: float = 0.45,
) -> list[dict[str, Any]]:
    """Drop near-duplicate topic labels by token overlap (e.g. student-first ≈ student-centric)."""
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    for row in topics:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        toks = _token_set(text)
        if any(_jaccard(toks, existing) >= threshold for existing in kept_tokens):
            continue
        kept.append(dict(row))
        kept_tokens.append(toks)
    for i, t in enumerate(kept):
        t["id"] = f"topic_{i + 1}"
    return kept


# Back-compat private alias
_heuristic_topic_dedupe = heuristic_topic_dedupe


async def dedupe_topics_semantic(
    topics: list[dict[str, Any]],
    *,
    theme_title: str = "",
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    """Merge semantically duplicate topics via LLM; fall back to token overlap."""
    if len(topics) < 2:
        return topics

    # Fast path: heuristic first; if nothing merges, still ask LLM for cohesion.
    heuristic = _heuristic_topic_dedupe(topics)

    import asyncio

    from google import genai
    from google.genai import types

    payload = [
        {
            "i": i,
            "text": str(t.get("text") or "").strip(),
            "start_sec": t.get("start_sec"),
            "end_sec": t.get("end_sec"),
        }
        for i, t in enumerate(topics)
        if str(t.get("text") or "").strip()
    ]
    if len(payload) < 2:
        return topics

    prompt = (
        "Merge duplicate / near-duplicate TOPIC LABELS into a unique cohesive set.\n"
        "Examples of duplicates to merge: 'Student-First Philosophy' + "
        "'Student-Centric Decisions' → keep one best label.\n"
        "Rules:\n"
        "- Keep only UNIQUE ideas (semantic similarity / content overlap = merge)\n"
        "- Prefer the clearest, most carousel-ready label when merging\n"
        "- Preserve a coherent narrative set for the theme — drop redundant or scattered labels\n"
        "- Return topics in chronological preference (use earliest start_sec of the merge group)\n"
        f"Theme: {theme_title or '(none)'}\n"
        "Return ONLY a JSON array of objects: "
        '{"text": "label", "from_indices": [i, ...]}.\n\n'
        f"Topics:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM topic dedupe failed: %s", exc)
        return heuristic

    if not isinstance(raw, list) or not raw:
        return heuristic

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        label = str(row.get("text") or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        indices = row.get("from_indices") or row.get("indices") or []
        src = topics[0]
        best_start = None
        best_end = None
        if isinstance(indices, list) and indices:
            for idx in indices:
                try:
                    i = int(idx)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(topics):
                    cand = topics[i]
                    s = cand.get("start_sec")
                    try:
                        s_f = float(s) if s is not None else None
                    except (TypeError, ValueError):
                        s_f = None
                    if s_f is not None and (best_start is None or s_f < best_start):
                        best_start = s_f
                        best_end = cand.get("end_sec")
                        src = cand
        item = dict(src)
        item["text"] = label[:120]
        if best_start is not None:
            item["start_sec"] = float(best_start)
            item["end_sec"] = best_end
        item["verbatim"] = False
        item["generated"] = True
        out.append(item)
        if len(out) >= _MAX_TOPICS:
            break

    if not out:
        return heuristic
    for i, t in enumerate(out):
        t["id"] = f"topic_{i + 1}"
    # Second pass: heuristic on LLM output to catch leftover near-dupes.
    return _heuristic_topic_dedupe(out)


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
    topics = _heuristic_topic_dedupe(topics)[: max(1, int(max_topics))]
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
