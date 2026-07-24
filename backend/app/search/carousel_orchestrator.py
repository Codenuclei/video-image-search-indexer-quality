"""
Carousel Orchestrator — 6-phase pipeline helpers.

Non-negotiable rules (enforced across phases):
1. Non-Repetition — no overlapping theme content / duplicate phrasing across themes or cards.
2. Contextual Integrity — theme starts only at logical context boundaries (never mid-sentence/dialogue).
3. Entity-Theme Harmonization — when Search_Entity is provided, themes align around that entity
   (role, actions, dialogue, narrative), not generic timestamp buckets.

Phase 3 hooks/topics are VERBATIM transcript excerpts only — never LLM-paraphrased.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_MAX_THEMES = 8
_MAX_VERBATIM = 8
_MIN_CUE_CHARS = 18
_SESSION_TTL_SEC = 60 * 60 * 6  # 6 hours

# In-memory harmonized theme sessions (Phase 6 delete). Does NOT touch raw video / transcript.
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def create_session(
    *,
    drive_file_id: str,
    search_entity: str | None,
    themes: list[dict[str, Any]],
) -> str:
    """Store a theme segmentation session for later selective delete of harmonized themes."""
    session_id = str(uuid.uuid4())
    with _sessions_lock:
        _purge_expired_unlocked()
        _sessions[session_id] = {
            "created_at": time.time(),
            "drive_file_id": drive_file_id,
            "search_entity": (search_entity or "").strip() or None,
            "themes": {t["theme_id"]: dict(t) for t in themes},
            "carousel_drafts": {},  # theme_id -> list of card dicts
        }
    return session_id


def get_session(session_id: str) -> dict[str, Any] | None:
    with _sessions_lock:
        _purge_expired_unlocked()
        return _sessions.get(session_id)


def attach_carousel_draft(session_id: str, theme_id: str, cards: list[dict[str, Any]]) -> bool:
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            return False
        if theme_id not in sess["themes"]:
            return False
        sess["carousel_drafts"][theme_id] = list(cards)
        return True


def delete_harmonized_theme(session_id: str, theme_id: str) -> dict[str, Any]:
    """
    Phase 6: delete a harmonized theme + its carousel drafts only.
    Never deletes raw video, full transcript, or non-harmonized themes.
    """
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            return {"ok": False, "error": "session_not_found"}
        theme = sess["themes"].get(theme_id)
        if not theme:
            return {"ok": False, "error": "theme_not_found"}
        if not theme.get("harmonized"):
            return {
                "ok": False,
                "error": "not_harmonized",
                "detail": "Only Search_Entity-harmonized themes can be deleted via this endpoint.",
            }
        del sess["themes"][theme_id]
        sess["carousel_drafts"].pop(theme_id, None)
        remaining = list(sess["themes"].values())
        return {
            "ok": True,
            "deleted_theme_id": theme_id,
            "remaining_themes": remaining,
            "drive_file_id": sess["drive_file_id"],
            "search_entity": sess.get("search_entity"),
        }


def _purge_expired_unlocked() -> None:
    now = time.time()
    dead = [sid for sid, s in _sessions.items() if now - float(s.get("created_at", 0)) > _SESSION_TTL_SEC]
    for sid in dead:
        _sessions.pop(sid, None)


# ── Cue loading helpers ─────────────────────────────────────────────────────


def cues_in_range(
    cues: list[tuple[float, float | None, str]],
    start_sec: float,
    end_sec: float | None,
) -> list[tuple[float, float | None, str]]:
    end = float(end_sec) if end_sec is not None else float("inf")
    out: list[tuple[float, float | None, str]] = []
    for s, e, text in cues:
        if s < start_sec - 0.05:
            continue
        if s > end + 0.05:
            break
        cleaned = " ".join((text or "").split())
        if cleaned:
            out.append((float(s), float(e) if e is not None else None, cleaned))
    return out


def snap_to_boundary(
    cues: list[tuple[float, float | None, str]],
    target_sec: float,
    *,
    prefer: str = "start",
) -> float:
    """Snap a timestamp to the nearest cue start (contextual integrity)."""
    if not cues:
        return max(0.0, float(target_sec))
    best = float(cues[0][0])
    best_dist = abs(best - target_sec)
    for s, _e, _t in cues:
        d = abs(float(s) - target_sec)
        if d < best_dist or (abs(d - best_dist) < 1e-6 and prefer == "start" and float(s) <= target_sec):
            best = float(s)
            best_dist = d
    return best


# ── Phase 2: Themes ─────────────────────────────────────────────────────────


def segment_themes(
    cues: list[tuple[float, float | None, str]],
    *,
    search_entity: str | None = None,
    video_name: str = "",
) -> list[dict[str, Any]]:
    """
    Segment transcript into non-overlapping themes at cue boundaries.
    If search_entity is set, prefer entity-bearing spans and harmonize titles/summaries.
    """
    usable = [(float(s), float(e) if e is not None else None, " ".join((t or "").split())) for s, e, t in cues if (t or "").strip()]
    if not usable:
        return []

    entity = (search_entity or "").strip()
    if entity:
        themes = _entity_harmonized_themes(usable, entity=entity, video_name=video_name)
    else:
        themes = _bucket_themes(usable)

    return _dedupe_themes(themes)


def _bucket_themes(cues: list[tuple[float, float | None, str]], max_themes: int = _MAX_THEMES) -> list[dict[str, Any]]:
    n = min(max_themes, max(1, len(cues) // 5 or 1))
    n = min(n, len(cues))
    chunk = max(1, len(cues) // n)
    themes: list[dict[str, Any]] = []
    used_phrases: set[str] = set()

    for i in range(n):
        start_i = i * chunk
        end_i = len(cues) if i == n - 1 else min(len(cues), (i + 1) * chunk)
        bucket = cues[start_i:end_i]
        if not bucket:
            continue
        start_sec = float(bucket[0][0])
        end_sec = float(bucket[-1][1] if bucket[-1][1] is not None else bucket[-1][0])
        joined = " ".join(row[2] for row in bucket)
        title = _unique_title(_title_from_text(joined) or f"Moment {i + 1}", used_phrases)
        summary = _clip_summary(joined)
        theme_id = _theme_id(i, start_sec, title, harmonized=False)
        themes.append(
            {
                "theme_id": theme_id,
                "start_timestamp": start_sec,
                "end_timestamp": end_sec if end_sec >= start_sec else start_sec,
                "summary": summary,
                "title": title,
                "harmonized": False,
                "search_entity": None,
            }
        )
    return themes


def _entity_harmonized_themes(
    cues: list[tuple[float, float | None, str]],
    *,
    entity: str,
    video_name: str,
) -> list[dict[str, Any]]:
    """Build themes around entity mentions; expand to surrounding cue context boundaries."""
    entity_l = entity.lower()
    hit_indices = [i for i, (_s, _e, t) in enumerate(cues) if entity_l in t.lower()]
    if not hit_indices:
        # Soft fallback: still mark as harmonized attempt with full-span theme
        base = _bucket_themes(cues, max_themes=4)
        for t in base:
            t["harmonized"] = True
            t["search_entity"] = entity
            t["title"] = f"{t['title']} featuring {entity}"
            t["summary"] = f"Segment reviewed for {entity}. {t['summary']}"
            t["theme_id"] = _theme_id(hash(t["theme_id"]) % 1000, t["start_timestamp"], t["title"], harmonized=True)
        return base

    # Cluster nearby hits into scenes (gap > 45s starts new cluster)
    clusters: list[list[int]] = []
    current: list[int] = [hit_indices[0]]
    for idx in hit_indices[1:]:
        prev = current[-1]
        gap = float(cues[idx][0]) - float(cues[prev][0])
        if gap > 45:
            clusters.append(current)
            current = [idx]
        else:
            current.append(idx)
    clusters.append(current)

    themes: list[dict[str, Any]] = []
    used_phrases: set[str] = set()
    covered: list[tuple[float, float]] = []

    for ci, cluster in enumerate(clusters[:_MAX_THEMES]):
        center_lo = cluster[0]
        center_hi = cluster[-1]
        # Expand to context boundaries (±2 cues), without overlapping prior themes
        lo = max(0, center_lo - 2)
        hi = min(len(cues) - 1, center_hi + 2)
        start_sec = float(cues[lo][0])
        end_sec = float(cues[hi][1] if cues[hi][1] is not None else cues[hi][0])

        # Avoid overlap with previous theme ranges (Non-Repetition)
        if covered:
            prev_end = covered[-1][1]
            if start_sec < prev_end:
                # Snap start forward to next unused cue after prev_end
                for j in range(lo, len(cues)):
                    if float(cues[j][0]) >= prev_end - 0.05:
                        lo = j
                        start_sec = float(cues[j][0])
                        break
                if start_sec < prev_end:
                    continue

        bucket = cues[lo : hi + 1]
        if not bucket:
            continue
        joined = " ".join(row[2] for row in bucket)
        role_hint = _entity_role_hint(joined, entity)
        raw_title = _title_from_text(joined) or f"Scene with {entity}"
        title = _unique_title(f"{raw_title} featuring {entity}", used_phrases)
        if role_hint:
            summary = f"{entity}'s {role_hint}. {_clip_summary(joined)}"
        else:
            summary = f"Moment featuring {entity}. {_clip_summary(joined)}"
        if video_name:
            summary = summary[:500]

        theme_id = _theme_id(ci, start_sec, title, harmonized=True)
        themes.append(
            {
                "theme_id": theme_id,
                "start_timestamp": start_sec,
                "end_timestamp": end_sec if end_sec >= start_sec else start_sec,
                "summary": summary[:500],
                "title": title[:160],
                "harmonized": True,
                "search_entity": entity,
            }
        )
        covered.append((start_sec, end_sec))

    return themes


async def llm_refine_themes(
    *,
    transcript: str,
    video_name: str,
    search_entity: str | None,
    api_key: str,
    model: str,
    seed_themes: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Optional Gemini refinement of theme titles/boundaries; preserves rules."""
    import json

    from google import genai
    from google.genai import types

    entity = (search_entity or "").strip()
    entity_rule = (
        f"Search_Entity is '{entity}'. Every theme MUST center on this entity's role, actions, "
        "dialogue, or narrative impact. Titles like 'Office Meeting featuring {entity}'s critical input'."
        if entity
        else "No Search_Entity — segment by natural scene/topic boundaries."
    )
    prompt = (
        "You segment video transcripts into carousel Themes (moments/scenes).\n"
        "RULES:\n"
        "1. Non-Repetition: no overlapping content or duplicate phrasing across themes.\n"
        "2. Contextual Integrity: start_timestamp MUST align to cue starts; never mid-sentence.\n"
        f"3. {entity_rule}\n"
        f"Video: {video_name or '(untitled)'}\n"
        f"Seed themes (JSON):\n{json.dumps(seed_themes, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON array of objects with keys:\n"
        "theme_id, start_timestamp, end_timestamp, title, summary"
        + (", harmonized (true), search_entity" if entity else "")
        + ".\n"
        f"Transcript:\n{transcript[:12000]}"
    )
    try:
        client = genai.Client(api_key=api_key)
        resp = await __import__("asyncio").to_thread(
            client.models.generate_content,
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.35, response_mime_type="application/json"),
        )
        text = (resp.text or "").strip()
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return None
        raw = json.loads(m.group())
        if not isinstance(raw, list):
            return None
        out: list[dict[str, Any]] = []
        used: set[str] = set()
        for i, row in enumerate(raw):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            title = _unique_title(title, used)
            start = float(row.get("start_timestamp") or row.get("start_sec") or 0)
            end = float(row.get("end_timestamp") or row.get("end_sec") or start)
            summary = str(row.get("summary") or "").strip()[:500] or title
            tid = str(row.get("theme_id") or "").strip() or _theme_id(i, start, title, harmonized=bool(entity))
            item = {
                "theme_id": tid[:80],
                "start_timestamp": start,
                "end_timestamp": end if end >= start else start,
                "title": title[:160],
                "summary": summary,
                "harmonized": bool(entity),
                "search_entity": entity or None,
            }
            out.append(item)
            if len(out) >= _MAX_THEMES:
                break
        return _dedupe_themes(out) if out else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm theme refine failed: %s", exc)
        return None


# ── Phase 3: Verbatim hooks & topics ────────────────────────────────────────


def extract_verbatim(
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    search_entity: str | None = None,
) -> dict[str, Any]:
    """
    Extract Verbatim_Hooks and Verbatim_Topics as exact transcript excerpts.
    No paraphrase / summarize / alter of cue text.
    """
    window = cues_in_range(cues, start_sec, end_sec)
    if not window:
        return {"verbatim_hooks": [], "verbatim_topics": [], "source": "empty"}

    entity = (search_entity or "").strip().lower()
    hooks: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    seen_norm: set[str] = set()

    # Prefer entity-bearing cues for hooks when Search_Entity is set
    ordered = sorted(
        window,
        key=lambda row: (
            0 if entity and entity in row[2].lower() else 1,
            -_hookiness(row[2]),
            row[0],
        ),
    )

    for s, e, text in ordered:
        if len(text) < _MIN_CUE_CHARS:
            continue
        norm = _norm_phrase(text)
        if norm in seen_norm:
            continue
        # Non-Repetition across hooks/topics
        if any(_jaccard(norm, prev) > 0.72 for prev in seen_norm):
            continue
        seen_norm.add(norm)
        item = {
            "id": f"v_{len(hooks) + len(topics)}_{int(s)}",
            "text": text,  # VERBATIM
            "start_sec": float(s),
            "end_sec": float(e) if e is not None else None,
            "preview_url": None,
        }
        if len(hooks) < _MAX_VERBATIM and (_hookiness(text) >= 0.35 or (entity and entity in text.lower())):
            hooks.append({**item, "kind": "hook"})
        elif len(topics) < _MAX_VERBATIM:
            topics.append({**item, "kind": "topic"})
        if len(hooks) >= 5 and len(topics) >= 5:
            break

    # Fill from remaining cues if thin
    for s, e, text in window:
        if len(hooks) >= 4 and len(topics) >= 4:
            break
        if len(text) < _MIN_CUE_CHARS:
            continue
        norm = _norm_phrase(text)
        if norm in seen_norm or any(_jaccard(norm, prev) > 0.72 for prev in seen_norm):
            continue
        seen_norm.add(norm)
        item = {
            "id": f"v_{len(hooks) + len(topics)}_{int(s)}",
            "text": text,
            "start_sec": float(s),
            "end_sec": float(e) if e is not None else None,
            "preview_url": None,
            "kind": "hook" if len(hooks) < len(topics) else "topic",
        }
        if item["kind"] == "hook" and len(hooks) < _MAX_VERBATIM:
            hooks.append(item)
        elif len(topics) < _MAX_VERBATIM:
            item["kind"] = "topic"
            topics.append(item)

    return {
        "verbatim_hooks": hooks,
        "verbatim_topics": topics,
        "source": "cues",
    }


# ── Phase 4: Intent (no script) ─────────────────────────────────────────────


def deduce_intent(
    *,
    theme: dict[str, Any],
    hooks: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    search_entity: str | None = None,
) -> dict[str, Any]:
    """Directional intent + score from Theme+Hook+Topic(+Entity). Does NOT write a script."""
    entity = (search_entity or theme.get("search_entity") or "").strip()
    hook_texts = [h.get("text") or "" for h in hooks]
    topic_texts = [t.get("text") or "" for t in topics]
    summary = str(theme.get("summary") or theme.get("title") or "")

    markers: list[dict[str, Any]] = []
    for kind, items in (("theme", [theme]), ("hook", hooks), ("topic", topics)):
        for it in items:
            start = float(it.get("start_sec") or it.get("start_timestamp") or 0)
            end = it.get("end_sec") if "end_sec" in it else it.get("end_timestamp")
            markers.append(
                {
                    "kind": kind,
                    "label": (it.get("title") or it.get("text") or kind)[:120],
                    "start_sec": start,
                    "end_sec": float(end) if end is not None else None,
                    "preview_url": it.get("preview_url"),
                }
            )

    # Heuristic directional intent
    blob = " ".join([summary, *hook_texts[:3], *topic_texts[:3], entity]).lower()
    if any(w in blob for w in ("how", "step", "learn", "teach", "guide")):
        direction = "Educational walkthrough"
        score = 0.78
    elif any(w in blob for w in ("why", "because", "truth", "myth", "never")):
        direction = "Contrarian insight"
        score = 0.74
    elif any(w in blob for w in ("story", "when i", "remember", "happened")):
        direction = "Narrative anecdote"
        score = 0.72
    elif entity:
        direction = f"Entity spotlight — {entity}"
        score = 0.8
    else:
        direction = "Moment highlight reel"
        score = 0.65

    # Boost score with coverage density
    n = len(hooks) + len(topics)
    score = min(0.95, score + min(0.12, n * 0.015))

    intent_score_text = (
        f"{direction} (score {score:.2f}). "
        f"Anchored on theme “{theme.get('title') or 'Untitled'}”"
        + (f" with entity {entity}" if entity else "")
        + f"; {len(hooks)} verbatim hooks, {len(topics)} verbatim topics."
    )

    return {
        "directional_intent": direction,
        "intent_score": round(score, 3),
        "intent_score_text": intent_score_text,
        "preview_markers": markers,
        "source": "heuristic",
    }


async def llm_intent(
    *,
    theme: dict[str, Any],
    hooks: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    search_entity: str | None,
    api_key: str,
    model: str,
) -> dict[str, Any] | None:
    import json

    from google import genai
    from google.genai import types

    prompt = (
        "Deduce Directional Intent for a short-form video carousel. Do NOT write a script.\n"
        "Return ONLY JSON with keys: directional_intent (short phrase), intent_score (0-1), "
        "intent_score_text (1-2 sentences explaining score).\n"
        f"Theme: {json.dumps(theme, ensure_ascii=False)}\n"
        f"Verbatim hooks: {json.dumps([h.get('text') for h in hooks[:6]], ensure_ascii=False)}\n"
        f"Verbatim topics: {json.dumps([t.get('text') for t in topics[:6]], ensure_ascii=False)}\n"
        f"Search_Entity: {(search_entity or theme.get('search_entity') or '') or '(none)'}\n"
    )
    try:
        client = genai.Client(api_key=api_key)
        resp = await __import__("asyncio").to_thread(
            client.models.generate_content,
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.4, response_mime_type="application/json"),
        )
        text = (resp.text or "").strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        raw = json.loads(m.group())
        if not isinstance(raw, dict):
            return None
        direction = str(raw.get("directional_intent") or "").strip()
        if not direction:
            return None
        try:
            score = float(raw.get("intent_score") or 0.7)
        except (TypeError, ValueError):
            score = 0.7
        score = max(0.0, min(1.0, score))
        score_text = str(raw.get("intent_score_text") or "").strip() or f"{direction} (score {score:.2f})"
        return {
            "directional_intent": direction[:160],
            "intent_score": round(score, 3),
            "intent_score_text": score_text[:500],
            "source": "llm",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm intent failed: %s", exc)
        return None


# ── Phase 5: Carousel cards ─────────────────────────────────────────────────


def build_carousel_cards(
    *,
    drive_file_id: str,
    video_name: str,
    theme: dict[str, Any],
    hooks: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    intent: dict[str, Any],
    slide_count: int = 6,
) -> list[dict[str, Any]]:
    """Slide cards with unique non-repetitive timestamps and verbatim transcript text."""
    n = max(3, min(int(slide_count), 8))
    # Interleave hooks then topics for variety; unique timestamps only
    pool: list[dict[str, Any]] = []
    for h in hooks:
        pool.append({**h, "_role": "hook"})
    for t in topics:
        pool.append({**t, "_role": "topic"})
    pool.sort(key=lambda x: float(x.get("start_sec") or 0))

    used_ts: set[float] = set()
    used_phrases: set[str] = set()
    cards: list[dict[str, Any]] = []

    # Opening card from theme summary still uses first available verbatim cue if possible
    for item in pool:
        if len(cards) >= n:
            break
        ts = round(float(item.get("start_sec") or 0), 2)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if ts in used_ts:
            continue
        norm = _norm_phrase(text)
        if any(_jaccard(norm, prev) > 0.7 for prev in used_phrases):
            continue
        used_ts.add(ts)
        used_phrases.add(norm)
        end = item.get("end_sec")
        cards.append(
            {
                "index": len(cards) + 1,
                "verbatim_text": text,  # VERBATIM
                "role": item.get("_role") or "beat",
                "drive_file_id": drive_file_id,
                "name": video_name,
                "timestamp_sec": float(item.get("start_sec") or 0),
                "end_timestamp_sec": float(end) if end is not None else None,
                "preview_url": f"/media/video/{drive_file_id}/frame?ts={float(item.get('start_sec') or 0)}",
                "theme_id": theme.get("theme_id"),
                "intent": intent.get("directional_intent"),
            }
        )

    # Pad from theme window evenly if short (still verbatim from leftover pool)
    if len(cards) < n:
        for item in pool:
            if len(cards) >= n:
                break
            ts = round(float(item.get("start_sec") or 0) + 0.01 * len(cards), 2)
            text = str(item.get("text") or "").strip()
            if not text or ts in used_ts:
                continue
            norm = _norm_phrase(text)
            if norm in used_phrases:
                continue
            used_ts.add(ts)
            used_phrases.add(norm)
            end = item.get("end_sec")
            cards.append(
                {
                    "index": len(cards) + 1,
                    "verbatim_text": text,
                    "role": item.get("_role") or "beat",
                    "drive_file_id": drive_file_id,
                    "name": video_name,
                    "timestamp_sec": float(item.get("start_sec") or 0),
                    "end_timestamp_sec": float(end) if end is not None else None,
                    "preview_url": f"/media/video/{drive_file_id}/frame?ts={float(item.get('start_sec') or 0)}",
                    "theme_id": theme.get("theme_id"),
                    "intent": intent.get("directional_intent"),
                }
            )

    return cards


# ── Utilities ───────────────────────────────────────────────────────────────


def _theme_id(index: int, start: float, title: str, *, harmonized: bool) -> str:
    raw = f"{'h' if harmonized else 't'}:{index}:{start:.2f}:{title.lower()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{'hz' if harmonized else 'th'}_{digest}"


def _title_from_text(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    piece = re.split(r"[.!?;:\n]", cleaned, maxsplit=1)[0].strip() or cleaned
    words = piece.split()
    if len(words) > 8:
        piece = " ".join(words[:8])
    return piece[:80]


def _clip_summary(text: str, limit: int = 280) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"


def _unique_title(title: str, used: set[str]) -> str:
    base = " ".join((title or "").split())[:160] or "Theme"
    key = base.lower()
    if key not in used:
        used.add(key)
        return base
    i = 2
    while f"{key} ({i})" in used:
        i += 1
    labeled = f"{base} ({i})"
    used.add(labeled.lower())
    return labeled


def _dedupe_themes(themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure non-overlapping time ranges and non-duplicate summaries."""
    if not themes:
        return []
    ordered = sorted(themes, key=lambda t: float(t.get("start_timestamp") or 0))
    out: list[dict[str, Any]] = []
    last_end = -1.0
    seen_sum: set[str] = set()
    for t in ordered:
        start = float(t.get("start_timestamp") or 0)
        end = float(t.get("end_timestamp") or start)
        if start < last_end - 0.5:
            start = last_end
        if end < start:
            end = start
        summary_key = _norm_phrase(str(t.get("summary") or t.get("title") or ""))
        if summary_key in seen_sum:
            continue
        if any(_jaccard(summary_key, prev) > 0.75 for prev in seen_sum):
            continue
        seen_sum.add(summary_key)
        item = dict(t)
        item["start_timestamp"] = start
        item["end_timestamp"] = end
        out.append(item)
        last_end = end
    return out


def _entity_role_hint(text: str, entity: str) -> str:
    lower = text.lower()
    ent = entity.lower()
    if "said" in lower or "asked" in lower or "?" in text:
        return "dialogue"
    if "decid" in lower or "lead" in lower or "propos" in lower:
        return "critical input"
    if ent in lower:
        return "presence"
    return ""


def _hookiness(text: str) -> float:
    t = text.lower()
    score = 0.2
    if "?" in text:
        score += 0.35
    if any(w in t for w in ("you", "we", "never", "always", "secret", "why", "how")):
        score += 0.25
    if len(text) < 120:
        score += 0.1
    return min(1.0, score)


def _norm_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (text or "").lower())).strip()


def _jaccard(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
