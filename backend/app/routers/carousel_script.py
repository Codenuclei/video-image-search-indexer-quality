"""Carousel Search script studio: curated hooks/topics + Gemini script drafts."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus, Face, Media, Person, VideoSegment
from app.db.session import get_db
from app.search.transcript_topics import (
    analyze_transcript_topics,
    compact_transcript,
    fallback_topics_from_cues,
)
from app.search.carousel_pipeline import (
    build_harmonized_themes,
    cue_preview_lines,
    deduce_directional_intent,
    extract_hooks_and_topics_async,
)
from app.search.english_text import cues_need_english, is_english_text
from app.pipelines.common import is_video_mime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search/carousel", tags=["carousel-script"])

# 7 hooks + 7 topics — cohesive for short-form video scripts from indexed moments.
CURATED_HOOKS: list[dict[str, str]] = [
    {"id": "curiosity_gap", "label": "Curiosity gap", "blurb": "Tease a reveal the viewer must stay for."},
    {"id": "bold_claim", "label": "Bold claim", "blurb": "Open with a confident, slightly contrarian statement."},
    {"id": "pain_point", "label": "Relatable pain", "blurb": "Name a frustration the audience already feels."},
    {"id": "stat_shock", "label": "Surprising stat", "blurb": "Lead with a number that reframes the topic."},
    {"id": "direct_question", "label": "Direct question", "blurb": "Ask the viewer something they want answered."},
    {"id": "story_teaser", "label": "Story teaser", "blurb": "Start mid-scene, then rewind to explain."},
    {"id": "challenge", "label": "Challenge", "blurb": "Dare the viewer to try one concrete action."},
]

CURATED_TOPICS: list[dict[str, str]] = [
    {"id": "leadership", "label": "Leadership", "blurb": "Decisions, influence, and owning outcomes."},
    {"id": "learning", "label": "Learning & skills", "blurb": "Growth, practice, and teaching moments."},
    {"id": "collaboration", "label": "Collaboration", "blurb": "Teams, feedback, and working together."},
    {"id": "innovation", "label": "Innovation", "blurb": "Change, experiments, and new ideas."},
    {"id": "personal_brand", "label": "Personal brand", "blurb": "Presence, credibility, and storytelling."},
    {"id": "productivity", "label": "Productivity", "blurb": "Focus, systems, and getting things done."},
    {"id": "career", "label": "Career advice", "blurb": "Paths, interviews, and professional moves."},
]


class SnapshotContext(BaseModel):
    drive_file_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=400)
    timestamp_sec: float = 0
    end_timestamp_sec: float | None = None
    snippet: str | None = Field(default=None, max_length=800)
    match_type: str | None = Field(default=None, max_length=80)
    preview_url: str | None = Field(default=None, max_length=500)


class ScriptTurn(BaseModel):
    role: str = Field(default="assistant", max_length=32)
    content: str = Field(default="", max_length=12_000)


class ScriptRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    hooks: list[str] = Field(default_factory=list, max_length=16)
    topics: list[str] = Field(default_factory=list, max_length=16)
    snapshot: SnapshotContext | None = None
    history: list[ScriptTurn] = Field(default_factory=list, max_length=24)


class ExpandRequest(BaseModel):
    kind: str = Field(default="hooks", pattern="^(hooks|topics)$")
    seed: str = Field(default="", max_length=400)
    count: int = Field(default=4, ge=1, le=8)


class CarouselOutlineRequest(BaseModel):
    """Build a multi-slide carousel from selected timed hooks/topics (or script + moments)."""

    script: str = Field(..., min_length=1, max_length=12_000)
    moments: list[SnapshotContext] = Field(..., min_length=1, max_length=40)
    hooks: list[str] = Field(default_factory=list, max_length=16)
    topics: list[str] = Field(default_factory=list, max_length=16)
    # One slide per selected pick (1–8); do not force-pad when fewer are selected.
    slide_count: int = Field(default=6, ge=1, le=8)
    title: str = Field(default="", max_length=200)


class CueMatchRequest(BaseModel):
    """Match selected hooks/topics to transcript-tied snapshots (moments or DB cues)."""

    hooks: list[str] = Field(default_factory=list, max_length=16)
    topics: list[str] = Field(default_factory=list, max_length=16)
    moments: list[SnapshotContext] = Field(default_factory=list, max_length=80)
    drive_file_id: str = Field(default="", max_length=128)


class CueMatchItem(BaseModel):
    kind: str  # "hook" | "topic"
    id: str
    label: str
    snapshot: SnapshotContext | None = None
    score: float = 0
    cue_text: str | None = None


class TranscriptTopicsRequest(BaseModel):
    """Analyze an indexed video's transcript into timed topics / subtopics."""

    drive_file_id: str = Field(..., min_length=1, max_length=128)


class PipelineThemesRequest(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    search_entity: str = Field(default="", max_length=200)
    # When set: presence-check only — never reframe/harmonize themes around the person.
    person_name: str = Field(default="", max_length=200)


class PipelineThemeSlice(BaseModel):
    theme_id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    start_sec: float = 0
    end_sec: float | None = None
    summary: str = Field(default="", max_length=800)


class PipelineExtractRequest(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    # Single-theme (legacy) fields — used when `themes` is empty.
    theme_id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    start_sec: float = 0
    end_sec: float | None = None
    summary: str = Field(default="", max_length=800)
    search_entity: str = Field(default="", max_length=200)
    # Multi-theme: extract each window then merge hooks/topics/previews in time order.
    themes: list[PipelineThemeSlice] = Field(default_factory=list, max_length=12)


class PipelineIntentRequest(BaseModel):
    theme_title: str = Field(default="", max_length=200)
    theme_summary: str = Field(default="", max_length=800)
    hooks: list[str] = Field(default_factory=list, max_length=16)
    topics: list[str] = Field(default_factory=list, max_length=16)
    search_entity: str = Field(default="", max_length=200)
    # Optional multi-theme titles/summaries for cohesive narrative intent.
    theme_titles: list[str] = Field(default_factory=list, max_length=12)
    theme_summaries: list[str] = Field(default_factory=list, max_length=12)


@router.get("/presets")
async def carousel_presets() -> dict[str, Any]:
    return {
        "hooks": CURATED_HOOKS,
        "topics": CURATED_TOPICS,
    }


@router.post("/presets/expand")
async def expand_presets(body: ExpandRequest) -> dict[str, Any]:
    """Optional Gemini expansion of hooks or topics; falls back to curated extras."""
    settings = get_settings()
    kind = body.kind
    base = CURATED_HOOKS if kind == "hooks" else CURATED_TOPICS
    fallback = [
        {"id": f"extra_{i}", "label": item["label"], "blurb": item["blurb"]}
        for i, item in enumerate(base[: body.count])
    ]

    if not settings.gemini_api_key:
        return {"source": "curated", "kind": kind, "items": fallback}

    seed = body.seed.strip() or (
        "short-form video scripts from lecture / interview moments"
        if kind == "hooks"
        else "professional learning and career content"
    )
    prompt = (
        f"Suggest {body.count} cohesive {kind} for creators writing spoken scripts "
        f"from indexed video moments. Context: {seed}\n"
        "Return ONLY a JSON array of objects with keys: id (snake_case), label (short), blurb (one sentence)."
    )
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = await __import__("asyncio").to_thread(
            client.models.generate_content,
            model=settings.gemini_model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.7,
                response_mime_type="application/json",
            ),
        )
        import json
        import re

        text = (resp.text or "").strip()
        m = re.search(r"\[[\s\S]*\]", text)
        items: list[dict[str, str]] = []
        if m:
            raw = json.loads(m.group())
            if isinstance(raw, list):
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    label = str(row.get("label") or "").strip()
                    if not label:
                        continue
                    items.append(
                        {
                            "id": str(row.get("id") or label.lower().replace(" ", "_"))[:64],
                            "label": label[:80],
                            "blurb": str(row.get("blurb") or "")[:200],
                        }
                    )
        if items:
            return {"source": "llm", "kind": kind, "items": items[: body.count]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel preset expand failed: %s", exc)
        return {"source": "curated", "kind": kind, "items": fallback, "warning": str(exc)[:160]}

    return {"source": "curated", "kind": kind, "items": fallback}


@router.post("/script")
async def generate_script(body: ScriptRequest) -> dict[str, Any]:
    """Generate (or iterate) a spoken script from hooks, topics, snapshot, and prompt."""
    settings = get_settings()
    hook_labels = _resolve_labels(body.hooks, CURATED_HOOKS)
    topic_labels = _resolve_labels(body.topics, CURATED_TOPICS)

    snapshot_block = ""
    if body.snapshot and (body.snapshot.name or body.snapshot.snippet or body.snapshot.drive_file_id):
        end = body.snapshot.end_timestamp_sec
        time_label = f"{body.snapshot.timestamp_sec:.1f}s"
        if end is not None and end > body.snapshot.timestamp_sec + 0.5:
            time_label = f"{body.snapshot.timestamp_sec:.1f}s–{end:.1f}s"
        snapshot_block = (
            f"\nAttached video snapshot:\n"
            f"- file: {body.snapshot.name or body.snapshot.drive_file_id}\n"
            f"- timestamp: {time_label}\n"
            f"- match: {body.snapshot.match_type or 'n/a'}\n"
            f"- snippet: {(body.snapshot.snippet or '').strip() or '(none)'}\n"
        )

    history_block = ""
    if body.history:
        parts: list[str] = []
        for turn in body.history[-12:]:
            role = (turn.role or "assistant").strip() or "assistant"
            content = (turn.content or "").strip()
            if content:
                parts.append(f"{role.upper()}:\n{content}")
        if parts:
            history_block = "\nPrevious drafts (iterate on the latest):\n" + "\n\n".join(parts) + "\n"

    system = (
        "You write short spoken video scripts for creators who pick hooks/topics "
        "and a moment from indexed Drive/YouTube video. Keep language natural for "
        "on-camera delivery. Prefer 80–180 words unless the user asks otherwise. "
        "If a previous draft exists, refine it — do not ignore prior output."
    )
    user_prompt = (
        f"Hooks to lean on: {', '.join(hook_labels) or '(none selected)'}\n"
        f"Topics to cover: {', '.join(topic_labels) or '(none selected)'}\n"
        f"{snapshot_block}"
        f"{history_block}"
        f"User script prompt:\n{body.prompt.strip()}\n\n"
        "Write the next script draft only (no preamble)."
    )

    if not settings.gemini_api_key:
        draft = _fallback_script(body.prompt, hook_labels, topic_labels, body.snapshot)
        return {"source": "fallback", "script": draft, "hooks": hook_labels, "topics": topic_labels}

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        resp = await __import__("asyncio").to_thread(
            client.models.generate_content,
            model=settings.gemini_model,
            contents=[
                types.Content(role="user", parts=[types.Part(text=f"{system}\n\n{user_prompt}")])
            ],
            config=types.GenerateContentConfig(temperature=0.75),
        )
        text = (resp.text or "").strip()
        if not text:
            draft = _fallback_script(body.prompt, hook_labels, topic_labels, body.snapshot)
            return {"source": "fallback", "script": draft, "hooks": hook_labels, "topics": topic_labels}
        return {"source": "llm", "script": text, "hooks": hook_labels, "topics": topic_labels}
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel script generation failed: %s", exc)
        draft = _fallback_script(body.prompt, hook_labels, topic_labels, body.snapshot)
        return {
            "source": "fallback",
            "script": draft,
            "hooks": hook_labels,
            "topics": topic_labels,
            "warning": str(exc)[:160],
        }


def _video_list_item(drive_file: DriveFile, cues: int) -> dict[str, Any]:
    return {
        "id": drive_file.id,
        "name": drive_file.name,
        "mime_type": drive_file.mime_type,
        "path": drive_file.path,
        "size": drive_file.size,
        "modified_time": drive_file.modified_time.isoformat() if drive_file.modified_time else None,
        "last_synced_at": drive_file.last_synced_at.isoformat() if drive_file.last_synced_at else None,
        "created_at": drive_file.created_at.isoformat() if getattr(drive_file, "created_at", None) else None,
        "status": drive_file.status.value if hasattr(drive_file.status, "value") else str(drive_file.status),
        "has_captions": cues > 0,
        "cue_count": cues,
    }


def _video_mime_filter():
    from sqlalchemy import or_

    return or_(
        DriveFile.mime_type.like("video/%"),
        DriveFile.mime_type.in_(
            (
                "video/mp4",
                "video/quicktime",
                "video/x-msvideo",
                "video/webm",
                "application/vnd.google-apps.video",
            )
        ),
    )


@router.get("/recent-videos")
async def carousel_recent_videos(
    limit: int = 5,
    captioned_only: bool = True,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 1: recent videos with transcript captions (most relevant for themes).

    Ordered by last sync / modified. By default only returns videos that have
    non-empty transcript cues; set captioned_only=false to backfill.
    """
    from sqlalchemy import and_, func

    limit = max(1, min(int(limit or 5), 12))
    cue_count = func.count(VideoSegment.id).label("cue_count")
    stmt = (
        select(DriveFile, cue_count)
        .outerjoin(Media, Media.drive_file_id == DriveFile.id)
        .outerjoin(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(
            DriveFile.status == DriveFileStatus.PROCESSED,
            _video_mime_filter(),
        )
        .group_by(DriveFile.id)
        .order_by(
            cue_count.desc(),
            DriveFile.last_synced_at.desc().nulls_last(),
            DriveFile.modified_time.desc().nulls_last(),
            DriveFile.created_at.desc().nulls_last(),
        )
        .limit(max(limit * 4, 20))
    )
    rows = list((await session.execute(stmt)).all())

    captioned: list[tuple[DriveFile, int]] = []
    others: list[tuple[DriveFile, int]] = []
    for drive_file, count in rows:
        if not is_video_mime(drive_file.mime_type):
            continue
        n = int(count or 0)
        if n > 0:
            captioned.append((drive_file, n))
        else:
            others.append((drive_file, n))

    picked = captioned[:limit]
    if not captioned_only and len(picked) < limit:
        picked.extend(others[: limit - len(picked)])

    return {
        "captioned_only": captioned_only,
        "items": [_video_list_item(v, cues) for v, cues in picked],
    }


@router.get("/videos")
async def carousel_videos(
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    captioned_only: bool = True,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List / search transcript-done (captioned) videos by title for Phase 1 picker.

    Same captioned definition as /recent-videos: at least one VideoSegment with
    non-empty text. Uses EXISTS (not HAVING on a label) so Postgres pagination
    stays correct.
    """
    from sqlalchemy import and_, exists, func

    limit = max(1, min(int(limit or 20), 50))
    offset = max(0, int(offset or 0))
    query = (q or "").strip()

    has_cues = exists(
        select(1)
        .select_from(Media)
        .join(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(Media.drive_file_id == DriveFile.id)
    )
    cue_count = (
        select(func.count(VideoSegment.id))
        .select_from(Media)
        .join(
            VideoSegment,
            and_(VideoSegment.media_id == Media.id, VideoSegment.text != ""),
        )
        .where(Media.drive_file_id == DriveFile.id)
        .correlate(DriveFile)
        .scalar_subquery()
        .label("cue_count")
    )

    stmt = select(DriveFile, cue_count).where(
        DriveFile.status == DriveFileStatus.PROCESSED,
        _video_mime_filter(),
    )
    if captioned_only:
        stmt = stmt.where(has_cues)
    if query:
        stmt = stmt.where(DriveFile.name.ilike(f"%{query}%"))
    stmt = stmt.order_by(
        DriveFile.name.asc(),
        DriveFile.last_synced_at.desc().nulls_last(),
    ).offset(offset).limit(limit + 1)

    rows = list((await session.execute(stmt)).all())
    has_more = len(rows) > limit
    picked = rows[:limit]
    items: list[dict[str, Any]] = []
    for drive_file, count in picked:
        if not is_video_mime(drive_file.mime_type):
            continue
        n = int(count or 0)
        if captioned_only and n <= 0:
            continue
        items.append(_video_list_item(drive_file, n))

    return {
        "q": query or None,
        "captioned_only": captioned_only,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "items": items,
    }


async def _person_appears_in_video(
    session: AsyncSession,
    drive_file_id: str,
    person_name: str,
) -> bool:
    """True if a named person has at least one face detection on this video."""
    name = (person_name or "").strip()
    if not name or not drive_file_id:
        return False
    stmt = (
        select(Face.id)
        .join(Media, Face.media_id == Media.id)
        .join(Person, Face.person_id == Person.id)
        .where(
            Media.drive_file_id == drive_file_id,
            Person.name.ilike(name),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


@router.post("/pipeline/themes")
async def carousel_pipeline_themes(
    body: PipelineThemesRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 2: segment transcript into normal themes.

    When person_name is set, only verify that person appears in the video (face match).
    If absent, return person_not_found — never reframe/harmonize themes around the person.
    """
    settings = get_settings()
    # Prefer explicit person_name; search_entity alone is treated as person only when it
    # matches a known Person row used for presence check below.
    explicit_person = (body.person_name or "").strip()
    drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())

    check_name = explicit_person
    if not check_name and (body.search_entity or "").strip():
        candidate = (body.search_entity or "").strip()
        person_row = (
            await session.execute(select(Person.id).where(Person.name.ilike(candidate)).limit(1))
        ).first()
        if person_row:
            check_name = candidate

    if check_name:
        found = await _person_appears_in_video(session, drive_file.id, check_name)
        if not found:
            return {
                "source": "person_not_found",
                "drive_file_id": drive_file.id,
                "name": drive_file.name,
                "search_entity": check_name,
                "person_name": check_name,
                "person_found": False,
                "harmonized": False,
                "themes": [],
                "error": "person_not_found",
                "message": (
                    "Person not found in this video. Try without that person or change video."
                ),
                "warning": (
                    "Person not found in this video. Try without that person or change video."
                ),
            }

    if not cues:
        return {
            "source": "empty",
            "drive_file_id": drive_file.id,
            "name": drive_file.name,
            "search_entity": check_name or None,
            "person_name": check_name or None,
            "person_found": True if check_name else None,
            "harmonized": False,
            "themes": [],
            "warning": "No transcript cues for this video",
        }
    themes, source, warning = await build_harmonized_themes(
        cues=cues,
        video_name=drive_file.name,
        search_entity=None,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return {
        "source": source,
        "drive_file_id": drive_file.id,
        "name": drive_file.name,
        "search_entity": check_name or None,
        "person_name": check_name or None,
        "person_found": True if check_name else None,
        "harmonized": False,
        "cue_count": len(cues),
        "themes": themes,
        **({"warning": warning} if warning else {}),
    }


@router.post("/pipeline/extract")
async def carousel_pipeline_extract(
    body: PipelineExtractRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 3–4: contextual hooks + theme-generated topics + preview markers + intent.

    Hooks prefer English: parallel English caption track when available, else Gemini translate.
    Accepts one theme (legacy fields) or multiple `themes` merged in time order.
    """
    settings = get_settings()
    drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())
    english_cues = await _maybe_load_english_cues(drive_file, cues)

    slices = list(body.themes or [])
    if not slices:
        slices = [
            PipelineThemeSlice(
                theme_id=body.theme_id or "",
                title=body.title or "",
                start_sec=float(body.start_sec or 0),
                end_sec=body.end_sec,
                summary=body.summary or "",
            )
        ]

    all_hooks: list[dict[str, Any]] = []
    all_topics: list[dict[str, Any]] = []
    all_previews: list[dict[str, Any]] = []
    seen_hooks: set[str] = set()
    seen_topics: set[str] = set()
    any_translated = False
    english_source: str | None = None
    hooks_english = True
    topics_english = True

    for sl in sorted(slices, key=lambda t: float(t.start_sec or 0)):
        extracted = await extract_hooks_and_topics_async(
            cues,
            start_sec=float(sl.start_sec or 0),
            end_sec=sl.end_sec,
            theme_title=sl.title or "",
            theme_summary=sl.summary or "",
            search_entity=(body.search_entity or "").strip() or None,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            english_cues=english_cues,
        )
        any_translated = any_translated or bool(extracted.get("any_translated"))
        english_source = english_source or extracted.get("english_source")
        hooks_english = hooks_english and bool(extracted.get("hooks_english", True))
        topics_english = topics_english and bool(extracted.get("topics_english", True))

        for h in extracted.get("hooks") or []:
            key = str(h.get("text") or "").strip().lower()
            if not key or key in seen_hooks:
                continue
            seen_hooks.add(key)
            row = dict(h)
            row["theme_id"] = sl.theme_id or None
            all_hooks.append(row)

        for t in extracted.get("topics") or []:
            key = str(t.get("text") or "").strip().lower()
            if not key or key in seen_topics:
                continue
            seen_topics.add(key)
            row = dict(t)
            row["theme_id"] = sl.theme_id or None
            all_topics.append(row)

        preview_source = english_cues if english_cues else cues
        previews = cue_preview_lines(
            preview_source,
            start_sec=float(sl.start_sec or 0),
            end_sec=sl.end_sec,
        )
        if english_cues and not previews:
            previews = cue_preview_lines(
                cues,
                start_sec=float(sl.start_sec or 0),
                end_sec=sl.end_sec,
            )
        for p in previews:
            item = dict(p)
            item["theme_id"] = sl.theme_id or None
            item["theme_title"] = sl.title or None
            all_previews.append(item)

    all_hooks.sort(key=lambda r: float(r.get("start_sec") or 0))
    all_topics.sort(key=lambda r: float(r.get("start_sec") or 0))
    all_previews.sort(key=lambda r: float(r.get("start_sec") or 0))

    # Cap lists but keep chronological coverage across themes; re-id after merge.
    hooks = all_hooks[:16]
    topics = all_topics[:16]
    for i, h in enumerate(hooks):
        h["id"] = f"hook_{i + 1}"
    for i, t in enumerate(topics):
        t["id"] = f"topic_{i + 1}"
    previews = all_previews[:40]

    theme_titles = [s.title for s in slices if (s.title or "").strip()]
    combined_title = " → ".join(theme_titles[:4]) if theme_titles else (body.title or "Theme")
    combined_summary = " ".join(
        (s.summary or "").strip() for s in slices if (s.summary or "").strip()
    )[:800]

    intent = await deduce_directional_intent(
        theme_title=combined_title,
        theme_summary=combined_summary,
        hooks=[h["text"] for h in hooks],
        topics=[t["text"] for t in topics],
        search_entity=(body.search_entity or "").strip() or None,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return {
        "drive_file_id": drive_file.id,
        "theme_id": slices[0].theme_id if len(slices) == 1 else None,
        "theme_ids": [s.theme_id for s in slices if s.theme_id],
        "hooks": hooks,
        "topics": topics,
        "previews": previews,
        "intent": intent.get("intent"),
        "intent_score": intent.get("intent_score"),
        "intent_source": intent.get("source"),
        "verbatim": False,
        "hooks_contextual": True,
        "topics_generated": True,
        "hooks_english": hooks_english,
        "topics_english": topics_english,
        "any_translated": any_translated,
        "english_source": english_source,
    }


@router.post("/pipeline/intent")
async def carousel_pipeline_intent(body: PipelineIntentRequest) -> dict[str, Any]:
    """Recompute directional intent from the user's selected themes + hooks/topics."""
    settings = get_settings()
    titles = [t.strip() for t in (body.theme_titles or []) if t and t.strip()]
    if (body.theme_title or "").strip() and (body.theme_title or "").strip() not in titles:
        titles.insert(0, (body.theme_title or "").strip())
    summaries = [s.strip() for s in (body.theme_summaries or []) if s and s.strip()]
    if (body.theme_summary or "").strip() and (body.theme_summary or "").strip() not in summaries:
        summaries.insert(0, (body.theme_summary or "").strip())
    theme_title = " → ".join(titles[:4]) if titles else "Theme"
    theme_summary = " ".join(summaries)[:800]
    intent = await deduce_directional_intent(
        theme_title=theme_title,
        theme_summary=theme_summary,
        hooks=list(body.hooks or []),
        topics=list(body.topics or []),
        search_entity=(body.search_entity or "").strip() or None,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    return {
        "intent": intent.get("intent"),
        "intent_score": intent.get("intent_score"),
        "intent_source": intent.get("source"),
    }


async def _maybe_load_english_cues(
    drive_file: DriveFile,
    cues: list[tuple[float, float | None, str]],
) -> list[tuple[float, float | None, str]] | None:
    """Fetch parallel English YouTube captions when indexed cues look non-English."""
    if not cues or not cues_need_english(cues):
        return None

    from app.video.youtube_registry import youtube_id_from_drive_file
    from app.video.youtube_transcript import fetch_youtube_captions, youtube_id_from_filename

    yt_id = youtube_id_from_drive_file(drive_file) or youtube_id_from_filename(drive_file.name or "")
    if not yt_id:
        return None
    try:
        vtt = await fetch_youtube_captions(yt_id, lang="en")
    except Exception as exc:  # noqa: BLE001
        logger.warning("English caption fetch failed for %s: %s", drive_file.id, exc)
        return None
    english = [
        (float(c.start_sec), float(c.end_sec) if c.end_sec is not None else None, c.text or "")
        for c in vtt
        if (c.text or "").strip() and is_english_text(c.text or "")
    ]
    if not english:
        return None
    logger.info(
        "Loaded %d English caption cues for carousel extract (%s)",
        len(english),
        yt_id,
    )
    return english


async def _load_video_cues(
    session: AsyncSession, drive_file_id: str
) -> tuple[DriveFile, list[tuple[float, float | None, str]]]:
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="Drive file not found")
    media_result = await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
    media = media_result.scalar_one_or_none()
    if media is None:
        return drive_file, []
    seg_result = await session.execute(
        select(VideoSegment)
        .where(VideoSegment.media_id == media.id, VideoSegment.text != "")
        .order_by(VideoSegment.start_sec)
    )
    segments = list(seg_result.scalars().all())
    cues = [
        (float(s.start_sec), float(s.end_sec) if s.end_sec is not None else None, s.text or "")
        for s in segments
        if (s.text or "").strip()
    ]
    return drive_file, cues


@router.post("/transcript-topics")
async def generate_transcript_topics(
    body: TranscriptTopicsRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Analyze the selected video's indexed transcript into topics and subtopics
    with start/end timestamps and short explanations for carousel context.
    """
    settings = get_settings()
    drive_file_id = body.drive_file_id.strip()
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")

    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="Drive file not found")

    media_result = await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
    media = media_result.scalar_one_or_none()
    if media is None:
        return {
            "source": "empty",
            "drive_file_id": drive_file_id,
            "name": drive_file.name,
            "cue_count": 0,
            "topics": [],
            "warning": "Video is not indexed yet",
        }

    seg_result = await session.execute(
        select(VideoSegment)
        .where(VideoSegment.media_id == media.id, VideoSegment.text != "")
        .order_by(VideoSegment.start_sec)
    )
    segments = list(seg_result.scalars().all())
    cues: list[tuple[float, float | None, str]] = [
        (float(s.start_sec), float(s.end_sec) if s.end_sec is not None else None, s.text or "")
        for s in segments
        if (s.text or "").strip()
    ]

    if not cues:
        return {
            "source": "empty",
            "drive_file_id": drive_file_id,
            "name": drive_file.name,
            "cue_count": 0,
            "topics": [],
            "warning": "No transcript cues for this video",
        }

    transcript = compact_transcript(cues)
    topics: list[dict[str, Any]] = []
    source = "fallback"
    warning: str | None = None

    if settings.gemini_api_key:
        topics, source, warning = await analyze_transcript_topics(
            transcript=transcript,
            video_name=drive_file.name,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )

    if not topics:
        topics = fallback_topics_from_cues(cues)
        source = "fallback" if topics else "empty"
        if not warning and not topics:
            warning = "Could not derive topics from transcript"
        elif source == "fallback" and settings.gemini_api_key and warning is None:
            warning = "Used local transcript bucketing"

    return {
        "source": source,
        "drive_file_id": drive_file_id,
        "name": drive_file.name,
        "cue_count": len(cues),
        "topics": topics,
        **({"warning": warning} if warning else {}),
    }


@router.post("/cues")
async def match_carousel_cues(body: CueMatchRequest) -> dict[str, Any]:
    """
    For each selected hook/topic, suggest the snapshot (frame + transcript cue)
    where that idea is spoken about. Uses provided search moments; optionally
    enriches from indexed VideoSegment rows for drive_file_id (sibling transcript API stub).
    """
    hook_labels = _resolve_labels(body.hooks, CURATED_HOOKS)
    topic_labels = _resolve_labels(body.topics, CURATED_TOPICS)
    moments = list(body.moments)

    # Soft stub: pull transcript segments when moments are thin but a file id is known.
    if body.drive_file_id and len(moments) < 3:
        extra = await _transcript_moments_for_file(body.drive_file_id)
        moments = _merge_moments(moments, extra)

    cues: list[dict[str, Any]] = []
    for label in hook_labels:
        item = _best_cue_for_label("hook", label, moments)
        cues.append(item)
    for label in topic_labels:
        item = _best_cue_for_label("topic", label, moments)
        cues.append(item)

    return {
        "source": "transcript_moments",
        "hooks": hook_labels,
        "topics": topic_labels,
        "cues": cues,
    }


@router.post("/outline")
async def generate_carousel_outline(
    body: CarouselOutlineRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Build carousel slides from selected timed picks (exact text + span-aligned frames)."""
    selected_hooks = _resolve_labels(body.hooks, CURATED_HOOKS)
    selected_topics = _resolve_labels(body.topics, CURATED_TOPICS)
    moments = list(body.moments)
    # Prefer exact selected picks; do not force 5–8 padding when user chose fewer.
    slide_count = min(max(int(body.slide_count), 1), 8)
    if _moments_are_timed_picks(moments):
        slide_count = min(max(len(moments), 1), 8)
    title = _complete_line((body.title or "").strip() or _default_carousel_title(moments), max_len=160)

    # Normalize every moment to a working mid-span frame URL.
    for m in moments:
        m.preview_url = _frame_preview_url(m.drive_file_id, float(m.timestamp_sec), m.end_timestamp_sec)

    # Instagram-style: one slide per selected timed pick with exact text.
    if _moments_are_timed_picks(moments):
        slides = _slides_from_timed_picks(moments, slide_count)
        slides = await _polish_outline_frames(slides, session)
        hooks = selected_hooks or [
            (s.get("hook_line") or "") for s in slides if (s.get("match_type") or "") == "hook"
        ]
        topics = selected_topics or [
            (s.get("hook_line") or "") for s in slides if (s.get("match_type") or "") == "topic"
        ]
        return {
            "source": "selected_picks",
            "title": title,
            "slide_count": len(slides),
            "hooks": [h for h in hooks if h],
            "topics": [t for t in topics if t],
            "slides": slides,
            "cues": _cues_from_slides(hooks, topics, slides),
        }

    # Legacy path: moments are generic preview dumps — keep fallback (no curated pad).
    slides = _fallback_carousel_outline(body.script, moments, slide_count, selected_hooks)
    slides = await _polish_outline_frames(slides, session)
    return {
        "source": "fallback",
        "title": title,
        "slide_count": len(slides),
        "hooks": selected_hooks,
        "topics": selected_topics,
        "slides": slides,
        "cues": _cues_from_slides(selected_hooks, selected_topics, slides),
    }


async def _polish_outline_frames(
    slides: list[dict[str, Any]],
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Gemini rank + Instagram-ready check for each slide's display frame (span text unchanged)."""
    from app.search.carousel_frame_select import polish_slides_instagram_frames

    settings = get_settings()
    if not settings.gemini_api_key or not slides:
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
            if s.get("frame_ts") is None:
                s["frame_ts"] = _frame_ts(
                    float(s.get("timestamp_sec") or 0),
                    s.get("end_timestamp_sec"),
                )
        return slides

    async def ensure_frame(drive_file_id: str, ts: float) -> bytes | None:
        return await _ensure_outline_frame_bytes(drive_file_id, ts, session, settings)

    try:
        return await polish_slides_instagram_frames(
            slides,
            thumbnail_dir=str(settings.thumbnail_dir),
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            ensure_frame=ensure_frame,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instagram frame polish skipped: %s", exc)
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
        return slides


async def _ensure_outline_frame_bytes(
    drive_file_id: str,
    ts: float,
    session: AsyncSession,
    settings,
) -> bytes | None:
    """Load or extract a JPEG for Gemini ranking (best-effort; never raises)."""
    from app.routers.media import _extract_frame_on_demand
    from app.search.carousel_frame_select import cached_frame_path, load_cached_frame_bytes

    cached = load_cached_frame_bytes(str(settings.thumbnail_dir), drive_file_id, ts)
    if cached:
        return cached
    out_path = cached_frame_path(str(settings.thumbnail_dir), drive_file_id, ts)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ok = await _extract_frame_on_demand(drive_file_id, ts, out_path, settings, session)
        if ok and out_path.is_file():
            return out_path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.debug("outline frame extract failed %s@%.2f: %s", drive_file_id, ts, exc)
    return None


def _moments_are_timed_picks(moments: list[SnapshotContext]) -> bool:
    if not moments:
        return False
    kinds = {(m.match_type or "").strip().lower() for m in moments}
    return bool(kinds & {"hook", "topic"}) and kinds <= {"hook", "topic", "theme"}


def _frame_ts(start: float, end: float | None) -> float:
    s = float(start or 0)
    if end is not None:
        try:
            e = float(end)
        except (TypeError, ValueError):
            return s
        if e > s:
            return round(s + (e - s) * 0.5, 2)
    return s


def _frame_preview_url(drive_file_id: str, start: float, end: float | None) -> str | None:
    fid = (drive_file_id or "").strip()
    if not fid:
        return None
    return f"/media/video/{fid}/frame?ts={_frame_ts(start, end)}"


def _slides_from_timed_picks(
    moments: list[SnapshotContext],
    slide_count: int,
) -> list[dict[str, Any]]:
    """One slide per pick: exact snippet text + span-aligned frame."""
    n = min(max(int(slide_count), 1), 8, len(moments) or 1)
    ordered = sorted(enumerate(moments), key=lambda pair: (pair[1].timestamp_sec, pair[0]))
    slides: list[dict[str, Any]] = []
    for i, (mi, moment) in enumerate(ordered[:n]):
        line = (moment.snippet or "").strip() or f"Moment @ {moment.timestamp_sec:.0f}s"
        slides.append(
            _slide_from_moment(
                order=i + 1,
                moment=moment,
                moment_index=mi,
                hook_line=line,
                caption=None,
            )
        )
    return slides


def _resolve_labels(selected: list[str], catalog: list[dict[str, str]]) -> list[str]:
    by_id = {item["id"]: item["label"] for item in catalog}
    by_label = {item["label"].lower(): item["label"] for item in catalog}
    out: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        key = (raw or "").strip()
        if not key:
            continue
        label = by_id.get(key) or by_label.get(key.lower()) or key
        if label.lower() not in seen:
            seen.add(label.lower())
            out.append(label)
    return out


def _default_carousel_title(moments: list[SnapshotContext]) -> str:
    name = (moments[0].name if moments else "") or "Carousel"
    base = name.rsplit(".", 1)[0] if "." in name else name
    return f"{base[:80]} — carousel" if base else "Video carousel"


def _moment_catalog(moments: list[SnapshotContext]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for i, m in enumerate(moments):
        catalog.append(
            {
                "index": i,
                "drive_file_id": m.drive_file_id,
                "name": m.name,
                "timestamp_sec": m.timestamp_sec,
                "end_timestamp_sec": m.end_timestamp_sec,
                "snippet": (m.snippet or "")[:400],
                "match_type": m.match_type,
                "preview_url": m.preview_url,
            }
        )
    return catalog


def _slide_from_moment(
    *,
    order: int,
    moment: SnapshotContext,
    moment_index: int,
    hook_line: str,
    caption: str | None = None,
) -> dict[str, Any]:
    hook = _complete_line((hook_line or "").strip(), max_len=280)
    preview = _frame_preview_url(
        moment.drive_file_id,
        float(moment.timestamp_sec),
        moment.end_timestamp_sec,
    )
    # Prefer client-supplied frame URL only if it already looks like /media/video/.../frame
    existing = (moment.preview_url or "").strip()
    if existing and "/media/video/" in existing and "/frame" in existing:
        preview = existing
    return {
        "index": order,
        "hook_line": hook,
        "caption": ((caption or "").strip()[:400] or None),
        "drive_file_id": moment.drive_file_id,
        "name": moment.name,
        "timestamp_sec": float(moment.timestamp_sec),
        "end_timestamp_sec": moment.end_timestamp_sec,
        "snippet": moment.snippet,
        "match_type": moment.match_type,
        "preview_url": preview or None,
        "moment_index": moment_index,
        "frame_ts": _frame_ts(float(moment.timestamp_sec), moment.end_timestamp_sec),
        "frame_source": "heuristic",
        "instagram_ready": False,
    }


def _complete_line(text: str, *, max_len: int = 280) -> str:
    """Avoid mid-clause truncation on carousel hook lines / titles."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_len and not _looks_incomplete(cleaned):
        return cleaned
    # Prefer sentence end inside budget
    import re

    chunk = cleaned[:max_len]
    ends = list(re.finditer(r"[.!?]", chunk))
    if ends:
        return chunk[: ends[-1].end()].strip()
    words = chunk.split()
    while len(words) > 4 and _looks_incomplete(" ".join(words)):
        words.pop()
    return " ".join(words).rstrip(",;:–—-")


def _looks_incomplete(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t[-1] in ".!?\"":
        return False
    import re as _re

    trailing = (
        r"(?:to|be|in|on|at|of|for|and|or|the|a|an|with|from|as|is|are|was|were|their|our|my)$"
    )
    return bool(_re.search(trailing, t, _re.I))


def _split_script_beats(script: str, n: int) -> list[str]:
    text = (script or "").strip()
    if not text:
        return [f"Slide {i + 1}" for i in range(n)]
    # Prefer paragraph breaks, then sentences.
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if len(parts) < n:
        import re

        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if len(sentences) >= n:
            parts = sentences
    if len(parts) >= n:
        # Evenly sample n beats from parts
        if len(parts) == n:
            return parts
        step = len(parts) / n
        return [parts[min(len(parts) - 1, int(i * step))] for i in range(n)]
    # Pad by repeating last / generic CTA
    while len(parts) < n:
        if len(parts) == n - 1:
            parts.append("Takeaway — one clear next step for the viewer.")
        else:
            parts.append(parts[-1] if parts else f"Beat {len(parts) + 1}")
    return parts[:n]


def _fallback_carousel_outline(
    script: str,
    moments: list[SnapshotContext],
    slide_count: int,
    hooks: list[str],
) -> list[dict[str, Any]]:
    n = min(max(int(slide_count), 1), 8)
    ordered = sorted(enumerate(moments), key=lambda pair: (pair[1].timestamp_sec, pair[0]))
    if not ordered:
        return []
    # Prefer one slide per moment when moments already encode the picks.
    if len(ordered) <= n:
        picked = ordered
    else:
        picked = [ordered[min(len(ordered) - 1, int(i * (len(ordered) / n)))] for i in range(n)]

    slides: list[dict[str, Any]] = []
    for i, (mi, moment) in enumerate(picked):
        # Prefer exact moment snippet (selected pick text); fall back to hook label.
        line = (moment.snippet or "").strip()
        if not line and hooks and i < len(hooks):
            line = hooks[i]
        if not line:
            line = f"Moment @ {moment.timestamp_sec:.0f}s"
        slides.append(
            _slide_from_moment(
                order=i + 1,
                moment=moment,
                moment_index=mi,
                hook_line=line,
                caption=None,
            )
        )
    return slides


async def _llm_carousel_outline(
    *,
    script: str,
    moments: list[SnapshotContext],
    hooks: list[str],
    topics: list[str],
    slide_count: int,
    title: str,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    """Legacy LLM outline — prefer exact moment snippets over invented hook_lines."""
    import json
    import re

    from google import genai
    from google.genai import types

    catalog = _moment_catalog(moments)
    n = min(max(int(slide_count), 1), 8)
    prompt = (
        "You order Instagram-style carousel slides from selected timed moments.\n"
        f"Title hint: {title}\n"
        f"User-selected hooks: {', '.join(hooks) or '(none)'}\n"
        f"User-selected topics: {', '.join(topics) or '(none)'}\n"
        f"Target: exactly {n} slides.\n\n"
        f"Context:\n{script}\n\n"
        f"Moments (JSON):\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON object with keys:\n"
        '- "slides": array of objects with moment_index and optional caption\n'
        "CRITICAL: Do NOT invent or paraphrase hook_line — each slide uses the moment snippet verbatim.\n"
        "Order slides chronologically by timestamp when possible."
    )

    client = genai.Client(api_key=api_key)
    resp = await __import__("asyncio").to_thread(
        client.models.generate_content,
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    text = (resp.text or "").strip()
    if not text:
        return {}

    parsed: Any = None
    obj_m = re.search(r"\{[\s\S]*\}", text)
    if obj_m:
        try:
            parsed = json.loads(obj_m.group())
        except json.JSONDecodeError:
            parsed = None
    if parsed is None:
        return {}
    if isinstance(parsed, list):
        parsed = {"slides": parsed}
    if not isinstance(parsed, dict):
        return {}

    raw_slides = parsed.get("slides") or []
    if not isinstance(raw_slides, list) or not moments:
        return {}

    slides: list[dict[str, Any]] = []
    used: set[int] = set()
    for i, row in enumerate(raw_slides):
        if not isinstance(row, dict):
            continue
        try:
            mi = int(row.get("moment_index", i % len(moments)))
        except (TypeError, ValueError):
            mi = i % len(moments)
        mi = max(0, min(mi, len(moments) - 1))
        if mi in used:
            continue
        used.add(mi)
        moment = moments[mi]
        # Exact span text — never LLM paraphrase.
        hook_line = (moment.snippet or "").strip() or f"Moment @ {moment.timestamp_sec:.0f}s"
        caption = str(row.get("caption") or "").strip() or None
        slides.append(
            _slide_from_moment(
                order=len(slides) + 1,
                moment=moment,
                moment_index=mi,
                hook_line=hook_line,
                caption=caption,
            )
        )
        if len(slides) >= n:
            break

    if not slides:
        return {}
    return {"hooks": hooks[:n], "topics": topics[:n], "slides": slides}


def _labels_from_raw(raw: Any, n: int) -> list[str]:
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            label = str(item).strip() if not isinstance(item, dict) else str(item.get("label") or "").strip()
            if label and label.lower() not in {x.lower() for x in out}:
                out.append(label[:80])
            if len(out) >= n:
                break
    return out


def _ensure_band(selected: list[str], catalog: list[dict[str, str]], n: int) -> list[str]:
    """Keep selected labels; only pad when explicitly needed and selection is empty."""
    target = min(max(int(n), 1), 8)
    out: list[str] = []
    seen: set[str] = set()
    for label in selected:
        key = (label or "").strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            out.append(key[:280])
    if out:
        return out[:target]
    for item in catalog:
        if len(out) >= target:
            break
        label = item["label"]
        if label.lower() not in seen:
            seen.add(label.lower())
            out.append(label)
    return out[:target]


def _score_label_against_snippet(label: str, snippet: str) -> float:
    import re

    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "that", "this", "it", "as", "at", "by", "from", "be", "you", "your",
    }
    label_toks = {t for t in re.findall(r"[a-z0-9]+", label.lower()) if t not in stop and len(t) > 2}
    snip_toks = {t for t in re.findall(r"[a-z0-9]+", (snippet or "").lower()) if t not in stop and len(t) > 2}
    if not label_toks:
        return 0.0
    if not snip_toks:
        return 0.05
    hit = len(label_toks & snip_toks)
    return hit / len(label_toks) + (0.15 if hit else 0.0)


def _best_cue_for_label(kind: str, label: str, moments: list[SnapshotContext]) -> dict[str, Any]:
    best: SnapshotContext | None = None
    best_score = -1.0
    for m in moments:
        hay = " ".join(filter(None, [m.snippet or "", m.name or "", m.match_type or ""]))
        score = _score_label_against_snippet(label, hay)
        mt = m.match_type or ""
        if mt.startswith("transcript") or mt.startswith("svs_transcript"):
            score += 0.1
        if score > best_score:
            best_score = score
            best = m
    if best is None and moments:
        best = moments[0]
        best_score = 0.0
    snap = None
    cue_text = None
    if best is not None:
        snap = best.model_dump()
        if not snap.get("preview_url") and best.drive_file_id:
            snap["preview_url"] = f"/media/video/{best.drive_file_id}/frame?ts={best.timestamp_sec}"
        cue_text = (best.snippet or "").strip() or None
    return {
        "kind": kind,
        "id": label.lower().replace(" ", "_")[:64],
        "label": label,
        "snapshot": snap,
        "score": round(float(max(best_score, 0.0)), 3),
        "cue_text": cue_text,
    }


def _cues_from_slides(
    hooks: list[str],
    topics: list[str],
    slides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    moments = [
        SnapshotContext(
            drive_file_id=str(s.get("drive_file_id") or ""),
            name=str(s.get("name") or ""),
            timestamp_sec=float(s.get("timestamp_sec") or 0),
            end_timestamp_sec=s.get("end_timestamp_sec"),
            snippet=s.get("snippet"),
            match_type=s.get("match_type"),
            preview_url=s.get("preview_url"),
        )
        for s in slides
    ]
    cues: list[dict[str, Any]] = []
    for i, label in enumerate(hooks):
        if i < len(moments):
            m = moments[i]
            cues.append(
                {
                    "kind": "hook",
                    "id": label.lower().replace(" ", "_")[:64],
                    "label": label,
                    "snapshot": m.model_dump(),
                    "score": 1.0,
                    "cue_text": (m.snippet or "").strip() or None,
                }
            )
        else:
            cues.append(_best_cue_for_label("hook", label, moments))
    for label in topics:
        cues.append(_best_cue_for_label("topic", label, moments))
    return cues


def _merge_moments(
    primary: list[SnapshotContext],
    extra: list[SnapshotContext],
) -> list[SnapshotContext]:
    seen: set[str] = set()
    out: list[SnapshotContext] = []
    for m in primary + extra:
        key = f"{m.drive_file_id}:{round(m.timestamp_sec, 2)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


async def _transcript_moments_for_file(drive_file_id: str) -> list[SnapshotContext]:
    """Pull indexed transcript cues for a drive file (reuses sibling transcript data)."""
    from app.db.models import MediaType
    from app.db.session import get_session_factory

    try:
        async with get_session_factory()() as session:
            stmt = (
                select(VideoSegment, DriveFile)
                .join(Media, VideoSegment.media_id == Media.id)
                .join(DriveFile, Media.drive_file_id == DriveFile.id)
                .where(
                    Media.drive_file_id == drive_file_id,
                    Media.type == MediaType.VIDEO,
                    VideoSegment.text != "",
                )
                .order_by(VideoSegment.start_sec)
                .limit(40)
            )
            rows = (await session.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript cue stub failed for %s: %s", drive_file_id, exc)
        return []

    out: list[SnapshotContext] = []
    for seg, drive_file in rows:
        ts = float(seg.start_sec or 0)
        out.append(
            SnapshotContext(
                drive_file_id=drive_file_id,
                name=drive_file.name or drive_file_id,
                timestamp_sec=ts,
                end_timestamp_sec=float(seg.end_sec) if seg.end_sec is not None else None,
                snippet=(seg.text or "")[:400] or None,
                match_type="transcript",
                preview_url=f"/media/video/{drive_file_id}/frame?ts={ts}",
            )
        )
    return out


def _fallback_script(
    prompt: str,
    hooks: list[str],
    topics: list[str],
    snapshot: SnapshotContext | None,
) -> str:
    lines = [
        "[Draft — Gemini unavailable; local template]",
        "",
        f"Hook angle: {', '.join(hooks) or 'open with energy'}.",
        f"Topic focus: {', '.join(topics) or 'the moment you selected'}.",
    ]
    if snapshot and snapshot.name:
        lines.append(
            f"Anchor on the clip “{snapshot.name}” around {snapshot.timestamp_sec:.0f}s"
            + (f": {snapshot.snippet}" if snapshot.snippet else ".")
        )
    lines.extend(
        [
            "",
            "Spoken draft:",
            prompt.strip(),
            "",
            "Close with one clear takeaway and a soft call-to-action.",
        ]
    )
    return "\n".join(lines)
