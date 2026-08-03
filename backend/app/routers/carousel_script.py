"""Carousel Search script studio: curated hooks/topics + Gemini script drafts."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    CarouselGenerationSave,
    DriveFile,
    DriveFileStatus,
    Face,
    Media,
    Person,
    VideoSegment,
)
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
    heuristic_topic_dedupe,
    _llm_translate_lines,
)
from app.search.english_text import (
    cues_need_english,
    is_english_text,
    needs_english,
    prefer_english_cues,
)
from app.pipelines.common import is_video_mime

logger = logging.getLogger(__name__)

# Single-worker uvicorn cannot usefully run multiple Gemini extract storms at
# once — studio remounts / e2e retries used to pile up overlapping extracts and
# starve health + other carousel routes. Serialize extracts process-wide.
_EXTRACT_LOCK = asyncio.Lock()
_EXTRACT_TIMEOUT_SEC = 900.0
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
    # When False (default), return a matching saved themes row if cache key matches.
    force: bool = False


SAVE_KIND_TOPICS = "topics_hooks"
SAVE_KIND_THEMES = "themes"
SAVE_KIND_CAROUSEL = "carousel"
CAROUSEL_ALGORITHM_VERSION = "p0-fast-grouped-v1"
CAROUSEL_STATUS_PROCESSING = "processing"
CAROUSEL_STATUS_IDLE = "idle"


async def _assert_carousel_unlocked(session: AsyncSession, drive_file_id: str) -> DriveFile:
    """Reject mutations while another carousel generation owns this video."""
    drive_file = await session.get(DriveFile, drive_file_id.strip())
    if drive_file is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if getattr(drive_file, "carousel_status", CAROUSEL_STATUS_IDLE) == CAROUSEL_STATUS_PROCESSING:
        raise HTTPException(
            status_code=409,
            detail="Carousel generation is locked for this video; wait for it to finish.",
            headers={"Retry-After": "1"},
        )
    return drive_file


async def _claim_carousel(
    session: AsyncSession, drive_file_id: str, input_hash: str | None = None
) -> str:
    """Atomically claim one video's carousel pipeline; prevents duplicate jobs."""
    drive_file_id = drive_file_id.strip()
    token = uuid.uuid4().hex
    result = await session.execute(
        update(DriveFile)
        .where(
            DriveFile.id == drive_file_id,
            DriveFile.carousel_status != CAROUSEL_STATUS_PROCESSING,
        )
        .values(
            carousel_status=CAROUSEL_STATUS_PROCESSING,
            carousel_lock_token=token,
            carousel_lock_input_hash=input_hash,
            carousel_locked_at=datetime.now(timezone.utc),
        )
    )
    if not result.rowcount:
        if await session.get(DriveFile, drive_file_id) is None:
            raise HTTPException(status_code=404, detail="Video not found")
        raise HTTPException(
            status_code=409,
            detail="Carousel generation is locked for this video; wait for it to finish.",
            headers={"Retry-After": "1"},
        )
    await session.commit()
    return token


async def _release_carousel(session: AsyncSession, drive_file_id: str, token: str) -> None:
    await session.execute(
        update(DriveFile)
        .where(DriveFile.id == drive_file_id, DriveFile.carousel_lock_token == token)
        .values(
            carousel_status=CAROUSEL_STATUS_IDLE,
            carousel_lock_token=None,
            carousel_lock_input_hash=None,
            carousel_locked_at=None,
        )
    )
    await session.commit()


def carousel_input_hash(drive_file_id: str, payload: dict[str, Any]) -> str:
    """Stable cache key for a complete artifact, independent of dict ordering."""
    import json

    raw = json.dumps(
        {"drive_file_id": drive_file_id.strip(), "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


async def _persist_carousel_artifact(
    session: AsyncSession,
    *,
    drive_file_id: str,
    payload: dict[str, Any],
    source: str,
    layout_mode: str = "single_1",
) -> CarouselGenerationSave | None:
    """Persist a ready, deterministic artifact for the cache-first endpoint."""
    safe_payload = _jsonb_safe(payload)
    input_hash = carousel_input_hash(drive_file_id, safe_payload)
    existing = await session.scalar(
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == drive_file_id,
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            CarouselGenerationSave.input_hash == input_hash,
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    if existing is not None:
        return existing
    save = CarouselGenerationSave(
        drive_file_id=drive_file_id,
        kind=SAVE_KIND_CAROUSEL,
        label=str(payload.get("title") or "Carousel")[:240],
        status="ready",
        input_hash=input_hash,
        layout_mode=layout_mode if layout_mode in {"single_1", "split_2"} else "single_1",
        copy_version=1,
        algorithm_version=CAROUSEL_ALGORITHM_VERSION,
        source=source,
        payload=safe_payload,
    )
    session.add(save)
    await session.commit()
    await session.refresh(save)
    return save


def _themes_transcript_hash(cues: list[Any]) -> str:
    """Stable cache key for theme generation input (transcript content)."""
    text = compact_transcript(cues) or ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


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


class TimedRange(BaseModel):
    start_sec: float = 0
    end_sec: float | None = None


class TimedPick(BaseModel):
    """A selected hook or topic with its spoken span (for multi-carousel generate)."""

    id: str = Field(default="", max_length=64)
    text: str = Field(..., min_length=1, max_length=400)
    start_sec: float = 0
    end_sec: float | None = None
    theme_id: str | None = Field(default=None, max_length=64)
    # Parent topic association (hooks) — used to expand one-carousel-per-topic seeds.
    topic_id: str | None = Field(default=None, max_length=64)
    topic_text: str | None = Field(default=None, max_length=400)
    # Spoken seed window that produced the crafted hook (for slide relevance).
    original_text: str | None = Field(default=None, max_length=800)
    # Non-contiguous topic threads (from topic_tree time_ranges).
    time_ranges: list[TimedRange] = Field(default_factory=list, max_length=12)


class CarouselGenerateRequest(BaseModel):
    """Generate one carousel per selected hook (≥6 one-line exact-transcript slides)."""

    drive_file_id: str = Field(..., min_length=1, max_length=128)
    video_name: str = Field(default="", max_length=400)
    intent: str = Field(default="", max_length=800)
    themes: list[PipelineThemeSlice] = Field(default_factory=list, max_length=12)
    hooks: list[TimedPick] = Field(default_factory=list, max_length=24)
    topics: list[TimedPick] = Field(default_factory=list, max_length=24)
    # Per-hook Instagram one-liners: at least 6 slides when cues allow.
    min_slides: int = Field(default=6, ge=2, le=12)
    max_slides: int = Field(default=10, ge=2, le=12)
    # Transcript-first: defer Gemini/frame selection until the user explicitly asks.
    select_images: bool = False


class CarouselSelectImagesBody(BaseModel):
    """Run quality + Gemini frame selection on already-edited carousel slides."""

    drive_file_id: str = Field(..., min_length=1, max_length=128)
    carousels: list[dict[str, Any]] = Field(default_factory=list, max_length=24)


class CarouselSaveBody(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    theme_key: str = Field(default="", max_length=256)
    label: str = Field(default="", max_length=240)
    topic_tree: list[dict[str, Any]] = Field(default_factory=list)
    hooks: list[dict[str, Any]] = Field(default_factory=list)
    topics: list[dict[str, Any]] = Field(default_factory=list)
    selected_hooks: list[str] = Field(default_factory=list, max_length=24)
    selected_topics: list[str] = Field(default_factory=list, max_length=24)
    intent: str | None = None
    intent_score: float | None = None
    themes: list[dict[str, Any]] = Field(default_factory=list)


class CarouselArtifactCopyBody(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    save_id: int | None = None
    layout_mode: str = Field(default="single_1", pattern="^(single_1|split_2)$")
    slides: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    theme: dict[str, Any] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class CarouselSlideRegenerateBody(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    save_id: int | None = None
    carousel_id: str = Field(default="", max_length=128)
    slide_index: int = Field(..., ge=0, le=24)
    slide: dict[str, Any] = Field(default_factory=dict)


class CarouselShuffleBody(BaseModel):
    """Reshuffle selection from a saved/current topic tree + hook pool."""

    topic_tree: list[dict[str, Any]] = Field(default_factory=list)
    hooks: list[dict[str, Any]] = Field(default_factory=list)
    topics: list[dict[str, Any]] = Field(default_factory=list)
    count_hooks: int = Field(default=3, ge=1, le=12)
    count_topics: int = Field(default=3, ge=1, le=12)


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

    claude_key = settings.anthropic_api_key or settings.claude_api_key
    if not settings.gemini_api_key and not claude_key:
        draft = _fallback_script(body.prompt, hook_labels, topic_labels, body.snapshot)
        return {"source": "fallback", "script": draft, "hooks": hook_labels, "topics": topic_labels}

    try:
        if claude_key:
            from anthropic import Anthropic

            def generate_claude() -> str:
                client = Anthropic(api_key=claude_key)
                resp = client.messages.create(
                    model=settings.claude_model,
                    max_tokens=1200,
                    temperature=0.75,
                    system=system,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                return "".join(
                    block.text for block in resp.content if getattr(block, "type", "") == "text"
                ).strip()

            text = await __import__("asyncio").to_thread(generate_claude)
            if text:
                return {"source": "claude", "script": text, "hooks": hook_labels, "topics": topic_labels}

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

    Themes are generated at most once per (video, transcript_hash, model) unless
    ``force=true``. Matching saves are returned immediately without calling Gemini.

    When person_name is set, only verify that person appears in the video (face match).
    If absent, return person_not_found — never reframe/harmonize themes around the person.
    """
    settings = get_settings()
    # Prefer explicit person_name; search_entity alone is treated as person only when it
    # matches a known Person row used for presence check below.
    explicit_person = (body.person_name or "").strip()
    drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())
    # A forced theme regeneration is a mutation and must not race generation.
    # Cache reads remain available through GET /pipeline/carousel.
    if body.force:
        await _assert_carousel_unlocked(session, drive_file.id)

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
                "cache_hit": False,
                "generated": False,
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
            "cache_hit": False,
            "generated": False,
            "warning": "No transcript cues for this video",
        }

    transcript_hash = _themes_transcript_hash(cues)
    model_name = (settings.gemini_model or "").strip() or None

    if not body.force:
        cached_q = (
            select(CarouselGenerationSave)
            .where(
                CarouselGenerationSave.drive_file_id == drive_file.id,
                CarouselGenerationSave.kind == SAVE_KIND_THEMES,
                CarouselGenerationSave.transcript_hash == transcript_hash,
            )
            .order_by(CarouselGenerationSave.created_at.desc())
            .limit(8)
        )
        cached_rows = list((await session.execute(cached_q)).scalars().all())
        for row in cached_rows:
            # Prefer exact model match; accept older rows with null model as last resort.
            if row.model and model_name and row.model != model_name:
                continue
            payload = row.payload or {}
            themes = list(payload.get("themes") or [])
            if not themes:
                continue
            logger.info(
                "carousel themes cache hit drive=%s save_id=%s hash=%s",
                drive_file.id,
                row.id,
                transcript_hash[:12],
            )
            return {
                "source": row.source or payload.get("source") or "saved",
                "drive_file_id": drive_file.id,
                "name": drive_file.name,
                "search_entity": check_name or None,
                "person_name": check_name or None,
                "person_found": True if check_name else None,
                "harmonized": False,
                "cue_count": payload.get("cue_count") or len(cues),
                "themes": themes,
                "cache_hit": True,
                "generated": False,
                "save_id": row.id,
                "transcript_hash": transcript_hash,
                "model": row.model or model_name,
            }

    themes, source, warning = await build_harmonized_themes(
        cues=cues,
        video_name=drive_file.name,
        search_entity=None,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        claude_api_key=settings.anthropic_api_key or settings.claude_api_key,
        claude_model=settings.claude_model,
    )
    result: dict[str, Any] = {
        "source": source,
        "drive_file_id": drive_file.id,
        "name": drive_file.name,
        "search_entity": check_name or None,
        "person_name": check_name or None,
        "person_found": True if check_name else None,
        "harmonized": False,
        "cue_count": len(cues),
        "themes": themes,
        "cache_hit": False,
        "generated": True,
        "transcript_hash": transcript_hash,
        "model": model_name,
        **({"warning": warning} if warning else {}),
    }

    if themes:
        try:
            label = f"{len(themes)} themes · {source}"
            save = CarouselGenerationSave(
                drive_file_id=drive_file.id,
                kind=SAVE_KIND_THEMES,
                theme_key="all",
                label=_jsonb_safe(label)[:240],
                model=model_name,
                transcript_hash=transcript_hash,
                source=(source or "")[:32] or None,
                payload=_jsonb_safe(
                    {
                        "drive_file_id": drive_file.id,
                        "themes": themes,
                        "source": source,
                        "cue_count": len(cues),
                        "transcript_hash": transcript_hash,
                        "model": model_name,
                        "person_name": check_name or None,
                    }
                ),
            )
            session.add(save)
            await session.commit()
            await session.refresh(save)
            result["save_id"] = save.id
            logger.info(
                "carousel themes saved drive=%s save_id=%s generated=1 hash=%s",
                drive_file.id,
                save.id,
                transcript_hash[:12],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("carousel themes autosave failed: %s", exc)
            await session.rollback()

    return result


@router.post("/pipeline/extract")
async def carousel_pipeline_extract(
    body: PipelineExtractRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 3–4: contextual hooks + theme-generated topics + preview markers + intent.

    Hooks prefer English: parallel English caption track when available, else Gemini translate.
    Accepts one theme (legacy fields) or multiple `themes` merged in time order.
    """
    # Serialize extracts so remount/retry storms cannot pin every default
    # thread-pool slot with concurrent Gemini jobs on workers=1.
    if _EXTRACT_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="Hook/topic extract already running; wait for it to finish.",
            headers={"Retry-After": "5"},
        )

    async with _EXTRACT_LOCK:
        if await request.is_disconnected():
            raise HTTPException(status_code=400, detail="Client disconnected before extract started")
        return await _carousel_pipeline_extract_impl(body, session, request)


async def _carousel_pipeline_extract_impl(
    body: PipelineExtractRequest,
    session: AsyncSession,
    request: Request,
) -> dict[str, Any]:
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

    # One global extract across the span of selected themes so topics cohere
    # across the (selected) talk — not fragmentary per-theme silos.
    slices_sorted = sorted(slices, key=lambda t: float(t.start_sec or 0))
    span_start = min(float(s.start_sec or 0) for s in slices_sorted)
    known_ends = [float(s.end_sec) for s in slices_sorted if s.end_sec is not None]
    span_end: float | None = max(known_ends) if known_ends else None
    if any(s.end_sec is None for s in slices_sorted):
        # Open-ended theme → read through the rest of the transcript.
        span_end = None

    theme_titles = [s.title for s in slices_sorted if (s.title or "").strip()]
    combined_title = " → ".join(theme_titles[:4]) if theme_titles else (body.title or "Theme")
    combined_summary = " ".join(
        (s.summary or "").strip() for s in slices_sorted if (s.summary or "").strip()
    )[:800]

    if await request.is_disconnected():
        raise HTTPException(status_code=400, detail="Client disconnected during extract")

    try:
        extracted = await asyncio.wait_for(
            extract_hooks_and_topics_async(
                cues,
                start_sec=span_start,
                end_sec=span_end,
                theme_title=combined_title,
                theme_summary=combined_summary,
                search_entity=(body.search_entity or "").strip() or None,
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
                english_cues=english_cues,
            ),
            timeout=_EXTRACT_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Hook/topic extract exceeded {_EXTRACT_TIMEOUT_SEC:.0f}s",
        ) from exc
    any_translated = bool(extracted.get("any_translated"))
    english_source = extracted.get("english_source")
    hooks_english = bool(extracted.get("hooks_english", True))
    topics_english = bool(extracted.get("topics_english", True))
    meta = extracted.get("transcript_meta") or {}
    per_theme_meta = [
        {
            "theme_id": s.theme_id,
            "title": s.title,
            "span_note": "global_extract",
            **(meta if isinstance(meta, dict) else {}),
        }
        for s in slices_sorted
    ]
    chars_sent = int(meta.get("transcript_chars") or 0) if isinstance(meta, dict) else 0
    chunks_used_total = int(meta.get("chunks_used") or 0) if isinstance(meta, dict) else 0

    hooks = list(extracted.get("hooks") or [])[:24]
    topics = list(extracted.get("topics") or [])[:24]
    if len(topics) >= 2:
        topics = heuristic_topic_dedupe(topics, threshold=0.62)
    topics = topics[:24]
    for i, h in enumerate(hooks):
        h["id"] = f"hook_{i + 1}"
    for i, t in enumerate(topics):
        t["id"] = f"topic_{i + 1}"
    topic_tree = list(extracted.get("topic_tree") or [])[:24]
    for i, node in enumerate(topic_tree):
        node["id"] = node.get("id") or f"topic_{i + 1}"

    all_previews: list[dict[str, Any]] = []
    preview_source = english_cues if english_cues else cues
    for sl in slices_sorted:
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
    all_previews.sort(key=lambda r: float(r.get("start_sec") or 0))
    previews = all_previews[:40]

    intent = await deduce_directional_intent(
        theme_title=combined_title,
        theme_summary=combined_summary,
        hooks=[h["text"] for h in hooks],
        topics=[t["text"] for t in topics if not t.get("is_subtopic")],
        search_entity=(body.search_entity or "").strip() or None,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    theme_key = "|".join(s.theme_id or s.title for s in slices)[:256]
    # Aggregate per-theme transcript diagnostics (from extract helpers).
    # Structural proof: never ship topic_tree sections with empty hooks arrays.
    empty_hook_sections = 0
    for node in topic_tree:
        if not (node.get("hooks") or []):
            empty_hook_sections += 1
        for sub in node.get("subtopics") or []:
            if not (sub.get("hooks") or []):
                empty_hook_sections += 1
    if empty_hook_sections:
        logger.warning(
            "extract response still has %d empty-hook sections; pruning before return",
            empty_hook_sections,
        )
        from app.search.carousel_pipeline import _drop_empty_hook_sections

        topic_tree = _drop_empty_hook_sections(topic_tree)[:24]
        topics = []
        hooks_from_tree: list[dict[str, Any]] = []
        for t in topic_tree:
            topics.append({k: v for k, v in t.items() if k != "subtopics" and k != "hooks"})
            hooks_from_tree.extend(list(t.get("hooks") or []))
            for sub in t.get("subtopics") or []:
                topics.append({**{k: v for k, v in sub.items() if k != "hooks"}, "is_subtopic": True})
                hooks_from_tree.extend(list(sub.get("hooks") or []))
        if hooks_from_tree:
            hooks = hooks_from_tree[:24]
            for i, h in enumerate(hooks):
                h["id"] = f"hook_{i + 1}"
        for i, t in enumerate(topics):
            t["id"] = t.get("id") or f"topic_{i + 1}"
        empty_hook_sections = 0
        for node in topic_tree:
            if not (node.get("hooks") or []):
                empty_hook_sections += 1
            for sub in node.get("subtopics") or []:
                if not (sub.get("hooks") or []):
                    empty_hook_sections += 1

    transcript_meta: dict[str, Any] = {
        "cue_count_total": len(cues),
        "theme_count": len(slices),
        "transcript_chars_sent": chars_sent,
        "chunks_used": chunks_used_total,
        "topic_tree_count": len(topic_tree),
        "flat_topic_count": len([t for t in topics if not t.get("is_subtopic")]),
        "hook_count": len(hooks),
        "topics_with_multi_ranges": (
            int(meta.get("topics_with_multi_ranges") or 0)
            if isinstance(meta, dict)
            else sum(1 for t in topic_tree if len(t.get("time_ranges") or []) >= 2)
        ),
        "verbatim_guard": (meta.get("verbatim_guard") if isinstance(meta, dict) else None),
        "topic_source": (meta.get("topic_source") if isinstance(meta, dict) else None),
        "empty_hook_sections": (
            int(meta.get("empty_hook_sections") or 0)
            if isinstance(meta, dict) and meta.get("empty_hook_sections") is not None
            else empty_hook_sections
        ),
        "per_theme": per_theme_meta,
    }
    # Prefer live tree count after any final prune.
    transcript_meta["empty_hook_sections"] = empty_hook_sections
    result = {
        "drive_file_id": drive_file.id,
        "theme_id": slices[0].theme_id if len(slices) == 1 else None,
        "theme_ids": [s.theme_id for s in slices if s.theme_id],
        "hooks": hooks,
        "topics": topics,
        "topic_tree": topic_tree,
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
        "transcript_meta": transcript_meta,
    }

    # Autosave generation so returning users can restore without regenerating.
    try:
        # Omit previews (cue labels often contain en-dashes); not needed to restore tree.
        save_body = {
            k: v for k, v in result.items() if k != "previews"
        }
        save_payload = _jsonb_safe(
            {
                **save_body,
                "themes": [s.model_dump() for s in slices],
                # Selection is user-driven only; never pre-pick on the client's behalf.
                "selected_hooks": [],
                "selected_topics": [],
            }
        )
        save = CarouselGenerationSave(
            drive_file_id=drive_file.id,
            kind=SAVE_KIND_TOPICS,
            source="extract_autosave",
            theme_key=theme_key,
            label=_jsonb_safe(combined_title or "Topics & hooks")[:240],
            payload=save_payload,
        )
        session.add(save)
        await session.commit()
        await session.refresh(save)
        result["save_id"] = save.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel autosave failed: %s", exc)
        await session.rollback()

    return result


def _jsonb_safe(value: Any) -> Any:
    """Make a value safe for Postgres JSONB on SQL_ASCII databases.

    Local Postgres here is SQL_ASCII; asyncpg emits \\uXXXX for non-ASCII which
    the server rejects. Normalize punctuation and drop remaining non-ASCII.
    """
    import json
    import re

    _PUNCT = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
        "\u00a0": " ",
        "\u2022": "*",
        "\u00b7": "*",
    }

    def _clean(v: Any) -> Any:
        if isinstance(v, str):
            cleaned = v.replace("\x00", "")
            for src, dst in _PUNCT.items():
                cleaned = cleaned.replace(src, dst)
            cleaned = re.sub(r"[\ud800-\udfff]", "", cleaned)
            # SQL_ASCII cannot store remaining multibyte chars via JSON escapes.
            cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
            return cleaned
        if isinstance(v, list):
            return [_clean(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _clean(x) for k, x in v.items()}
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                return None
            return v
        if isinstance(v, (int, bool)) or v is None:
            return v
        return _clean(str(v))

    raw = json.dumps(_clean(value), ensure_ascii=True, default=str)
    raw = raw.replace("\\u0000", "")
    return json.loads(raw)


@router.get("/pipeline/saves")
async def list_carousel_saves(
    drive_file_id: str,
    limit: int = 20,
    kind: str = Query(default=SAVE_KIND_TOPICS, pattern="^(topics_hooks|themes|carousel)$"),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Previous autosaved generations for a video (filter by kind)."""
    q = (
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == drive_file_id.strip(),
            CarouselGenerationSave.kind == kind,
        )
        .order_by(CarouselGenerationSave.created_at.desc())
        .limit(max(1, min(limit, 50)))
    )
    rows = list((await session.execute(q)).scalars().all())
    return {
        "items": [
            {
                "id": r.id,
                "drive_file_id": r.drive_file_id,
                "kind": getattr(r, "kind", None) or SAVE_KIND_TOPICS,
                "theme_key": r.theme_key,
                "label": r.label,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "source": r.source,
                "model": r.model,
                "transcript_hash": r.transcript_hash,
                "status": getattr(r, "status", "ready"),
                "input_hash": getattr(r, "input_hash", None),
                "layout_mode": getattr(r, "layout_mode", "single_1"),
                "copy_version": getattr(r, "copy_version", 1),
                "hook_count": len((r.payload or {}).get("hooks") or []),
                "topic_count": len(
                    (r.payload or {}).get("topic_tree")
                    or (r.payload or {}).get("topics")
                    or []
                ),
                "theme_count": len((r.payload or {}).get("themes") or []),
            }
            for r in rows
        ]
    }


@router.get("/pipeline/saves/{save_id}")
async def get_carousel_save(
    save_id: int,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await session.get(CarouselGenerationSave, save_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Save not found")
    return {
        "id": row.id,
        "drive_file_id": row.drive_file_id,
        "kind": getattr(row, "kind", None) or SAVE_KIND_TOPICS,
        "theme_key": row.theme_key,
        "label": row.label,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "source": row.source,
        "model": row.model,
        "transcript_hash": row.transcript_hash,
        "status": getattr(row, "status", "ready"),
        "input_hash": getattr(row, "input_hash", None),
        "layout_mode": getattr(row, "layout_mode", "single_1"),
        "copy_version": getattr(row, "copy_version", 1),
        "algorithm_version": getattr(row, "algorithm_version", "p0"),
        "payload": row.payload or {},
    }


@router.get("/pipeline/carousel")
async def get_cached_carousel(
    drive_file_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cache-first artifact endpoint; never runs frame selection."""
    row = await session.scalar(
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == drive_file_id.strip(),
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            CarouselGenerationSave.status == "ready",
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Carousel artifact not ready")
    return {
        "id": row.id,
        "status": row.status,
        "layout_mode": row.layout_mode,
        "copy_version": row.copy_version,
        "algorithm_version": row.algorithm_version,
        "input_hash": row.input_hash,
        **(row.payload or {}),
    }


@router.get("/pipeline/status")
async def get_carousel_pipeline_status(
    drive_file_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cheap lock/cache status read used while the UI polls generation."""
    row = await session.get(DriveFile, drive_file_id.strip())
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    artifact = await session.scalar(
        select(CarouselGenerationSave.id)
        .where(
            CarouselGenerationSave.drive_file_id == row.id,
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            CarouselGenerationSave.status == "ready",
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    return {
        "drive_file_id": row.id,
        "status": getattr(row, "carousel_status", CAROUSEL_STATUS_IDLE),
        "locked": getattr(row, "carousel_status", CAROUSEL_STATUS_IDLE)
        == CAROUSEL_STATUS_PROCESSING,
        "ready_artifact_id": artifact,
    }


@router.post("/pipeline/carousel/copy")
async def save_carousel_copy(
    body: CarouselArtifactCopyBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    token = await _claim_carousel(session, body.drive_file_id)
    try:
        return await _save_carousel_copy_impl(body, session)
    finally:
        await _release_carousel(session, body.drive_file_id.strip(), token)


async def _save_carousel_copy_impl(
    body: CarouselArtifactCopyBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Save theme/copy edits by reusing ranked slides from the current artifact."""
    current = await session.scalar(
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == body.drive_file_id.strip(),
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            CarouselGenerationSave.status == "ready",
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Carousel artifact not ready")
    payload = dict(current.payload or {})
    payload["slides"] = _jsonb_safe(body.slides or payload.get("slides") or [])
    payload["theme"] = _jsonb_safe(body.theme)
    payload["references"] = _jsonb_safe(body.references)
    # Keep the prior ready artifact immutable. Layout/copy edits are a cheap
    # new version and reuse all existing frame URLs.
    payload["layout_mode"] = body.layout_mode
    next_version = int(current.copy_version or 1) + 1
    next_hash = carousel_input_hash(body.drive_file_id, payload)
    existing = await session.scalar(
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == body.drive_file_id.strip(),
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            CarouselGenerationSave.input_hash == next_hash,
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    if existing is not None:
        return {
            "id": existing.id,
            "copy_version": existing.copy_version,
            "layout_mode": existing.layout_mode,
            "cache_hit": True,
        }
    replacement = CarouselGenerationSave(
        drive_file_id=body.drive_file_id.strip(),
        kind=SAVE_KIND_CAROUSEL,
        label=current.label,
        status="ready",
        input_hash=next_hash,
        layout_mode=body.layout_mode,
        copy_version=next_version,
        algorithm_version=current.algorithm_version,
        source="copy_edit",
        payload=payload,
    )
    session.add(replacement)
    await session.commit()
    await session.refresh(replacement)
    return {
        "id": replacement.id,
        "copy_version": replacement.copy_version,
        "layout_mode": replacement.layout_mode,
        "cache_hit": False,
    }


@router.post("/pipeline/carousel/slide/regenerate")
async def regenerate_carousel_slide(
    body: CarouselSlideRegenerateBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-select one frame and publish a new immutable artifact version."""
    token = await _claim_carousel(session, body.drive_file_id)
    try:
        current = await session.scalar(
            select(CarouselGenerationSave)
            .where(
                CarouselGenerationSave.id == body.save_id
                if body.save_id
                else CarouselGenerationSave.drive_file_id == body.drive_file_id.strip(),
                CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
                CarouselGenerationSave.status == "ready",
            )
            .order_by(CarouselGenerationSave.created_at.desc())
        )
        if current is None:
            raise HTTPException(status_code=404, detail="Carousel artifact not ready")
        payload = dict(current.payload or {})
        carousels = [dict(c) for c in (payload.get("carousels") or [])]
        target_car = next(
            (c for c in carousels if not body.carousel_id or c.get("id") == body.carousel_id),
            None,
        )
        if target_car is None:
            target_car = {"id": body.carousel_id or "carousel_1", "slides": payload.get("slides") or []}
            carousels = [target_car]
        slides = list(target_car.get("slides") or [])
        if body.slide_index >= len(slides):
            raise HTTPException(status_code=400, detail="slide_index is outside the artifact")
        candidate = dict(slides[body.slide_index])
        candidate.update(_jsonb_safe(body.slide))
        polished = await _polish_outline_frames([candidate], session)
        await _prewarm_carousel_frames([{"slides": polished}], session, get_settings())
        slides[body.slide_index] = polished[0]
        target_car["slides"] = slides
        target_car["slide_count"] = len(slides)
        payload["carousels"] = carousels
        if carousels and carousels[0] is target_car:
            payload["slides"] = slides
        payload["images_ready"] = True
        replacement = CarouselGenerationSave(
            drive_file_id=body.drive_file_id.strip(),
            kind=SAVE_KIND_CAROUSEL,
            label=current.label,
            status="ready",
            input_hash=carousel_input_hash(body.drive_file_id, payload),
            layout_mode=current.layout_mode,
            copy_version=int(current.copy_version or 1) + 1,
            algorithm_version=current.algorithm_version,
            source="slide_regenerate",
            payload=_jsonb_safe(payload),
        )
        session.add(replacement)
        await session.commit()
        await session.refresh(replacement)
        return {
            "id": replacement.id,
            "copy_version": replacement.copy_version,
            "slide": slides[body.slide_index],
        }
    finally:
        await _release_carousel(session, body.drive_file_id.strip(), token)


@router.post("/pipeline/saves")
async def create_carousel_save(
    body: CarouselSaveBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manual / client-triggered save of the current topics→hooks tree."""
    save = CarouselGenerationSave(
        drive_file_id=body.drive_file_id.strip(),
        kind=SAVE_KIND_TOPICS,
        source="user_save",
        theme_key=(body.theme_key or "")[:256],
        label=_jsonb_safe(body.label or "Topics & hooks")[:240],
        payload=_jsonb_safe(
            {
                "drive_file_id": body.drive_file_id.strip(),
                "topic_tree": body.topic_tree,
                "hooks": body.hooks,
                "topics": body.topics,
                "selected_hooks": body.selected_hooks,
                "selected_topics": body.selected_topics,
                "intent": body.intent,
                "intent_score": body.intent_score,
                "themes": body.themes,
            }
        ),
    )
    session.add(save)
    await session.commit()
    await session.refresh(save)
    return {"id": save.id, "created_at": save.created_at.isoformat() if save.created_at else None}


@router.post("/pipeline/shuffle")
async def shuffle_carousel_picks(body: CarouselShuffleBody) -> dict[str, Any]:
    """Reshuffle selected hooks/topics from the current generation pool."""
    import random

    hooks = [h for h in (body.hooks or []) if isinstance(h, dict) and (h.get("text") or "").strip()]
    topics = [
        t
        for t in (body.topics or [])
        if isinstance(t, dict) and (t.get("text") or "").strip() and not t.get("is_subtopic")
    ]
    # Prefer top-level topics from tree when available.
    if body.topic_tree:
        topics = [
            {
                "id": n.get("id"),
                "text": n.get("text"),
                "start_sec": n.get("start_sec"),
                "end_sec": n.get("end_sec"),
            }
            for n in body.topic_tree
            if isinstance(n, dict) and (n.get("text") or "").strip()
        ] or topics

    hook_pool = list(hooks)
    topic_pool = list(topics)
    random.shuffle(hook_pool)
    random.shuffle(topic_pool)
    picked_hooks = hook_pool[: min(body.count_hooks, len(hook_pool))]
    picked_topics = topic_pool[: min(body.count_topics, len(topic_pool))]
    return {
        "selected_hooks": [str(h.get("text")) for h in picked_hooks],
        "selected_topics": [str(t.get("text")) for t in picked_topics],
        "hooks": picked_hooks,
        "topics": picked_topics,
    }


@router.get("/pipeline/transcript-frames")
async def transcript_frame_candidates(
    drive_file_id: str,
    start_sec: float = 0,
    end_sec: float | None = None,
    limit: int = 48,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Dense transcript+frame candidates in a span, quality-filtered for image picking."""
    from app.search.carousel_frame_select import (
        filter_frame_candidates_by_quality,
        load_cached_frame_bytes,
        sample_candidate_timestamps,
    )

    settings = get_settings()
    drive_file, cues = await _load_video_cues(session, drive_file_id.strip())
    span_start = float(start_sec or 0)
    span_end = float(end_sec) if end_sec is not None else span_start + 40.0
    if span_end < span_start:
        span_end = span_start + 40.0

    # Cue-aligned candidates across the (possibly padded) window.
    window: list[tuple[float, float | None, str]] = []
    for s, e, t in cues:
        if not (t or "").strip():
            continue
        if float(s) < span_start - 0.05:
            continue
        if float(s) > span_end + 0.35:
            continue
        window.append((float(s), e, t.strip()))
    if not window:
        nearest = sorted(cues, key=lambda c: abs(float(c[0]) - span_start))[
            : max(1, min(limit, 24))
        ]
        window = [(float(s), e, (t or "").strip()) for s, e, t in nearest if (t or "").strip()]

    # Dense temporal samples between cues so the picker has more than cue starts.
    dense_ts = sample_candidate_timestamps(
        span_start,
        span_end,
        max_candidates=min(36, max(12, limit)),
        step_sec=0.45,
    )
    by_ts: dict[float, dict[str, Any]] = {}
    for s, e, text in window:
        by_ts[round(s, 2)] = {
            "start_sec": s,
            "end_sec": float(e) if e is not None else None,
            "text": text[:400],
            "frame_ts": round(s, 2),
            "cue": True,
        }
    for ts in dense_ts:
        key = round(ts, 2)
        if key in by_ts:
            continue
        # Attach nearest cue text for context.
        nearest_cue = min(window, key=lambda c: abs(c[0] - ts)) if window else None
        by_ts[key] = {
            "start_sec": key,
            "end_sec": nearest_cue[1] if nearest_cue else None,
            "text": (nearest_cue[2][:400] if nearest_cue else ""),
            "frame_ts": key,
            "cue": False,
        }

    raw_items = sorted(by_ts.values(), key=lambda x: float(x["frame_ts"]))
    # Load bytes for quality filter when cached; skip extract to keep picker snappy.
    images: list[bytes | None] = []
    for item in raw_items:
        images.append(
            load_cached_frame_bytes(
                str(settings.thumbnail_dir),
                drive_file.id,
                float(item["frame_ts"]),
            )
        )
    kept_idx, reject_stats = filter_frame_candidates_by_quality(
        images,
        timestamps=[float(x["frame_ts"]) for x in raw_items],
        max_keep=max(1, min(limit, 64)),
    )

    def _row(index: int, *, cached: bool) -> dict[str, Any]:
        row = dict(raw_items[index])
        row.pop("cue", None)
        ts = float(row["frame_ts"])
        # Cached frames render instantly; an uncached pick must be allowed to
        # extract, otherwise the picker shows an empty grid on fresh videos.
        suffix = "&cache_only=1" if cached else ""
        row["preview_url"] = f"/media/video/{drive_file.id}/frame?ts={ts:.3f}{suffix}"
        row["cached"] = cached
        return row

    target = max(1, min(limit, 24))
    items = [_row(i, cached=True) for i in kept_idx]
    # Frames are extracted lazily, so a video nobody has previewed yet has
    # nothing on disk. Offer cue-aligned timestamps the browser can pull.
    if len(items) < target:
        claimed = {round(float(x["frame_ts"]), 2) for x in items}
        fallback_order = sorted(
            range(len(raw_items)),
            key=lambda i: (0 if raw_items[i].get("cue") else 1, float(raw_items[i]["frame_ts"])),
        )
        for i in fallback_order:
            if len(items) >= target:
                break
            if images[i] is not None:
                continue
            ts = round(float(raw_items[i]["frame_ts"]), 2)
            if ts in claimed:
                continue
            claimed.add(ts)
            items.append(_row(i, cached=False))
    # Transcript order beats quality order in a picker tied to spoken cues.
    items.sort(key=lambda x: float(x["frame_ts"]))
    return {
        "drive_file_id": drive_file.id,
        "items": items,
        "quality": {
            "candidates": len(raw_items),
            "kept": len(items),
            "cached": sum(1 for x in items if x.get("cached")),
            **reject_stats,
        },
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


def _english_caption_track_usable(
    english: list[tuple[float, float | None, str]],
    indexed: list[tuple[float, float | None, str]],
    *,
    min_cues: int = 12,
) -> bool:
    """Reject sparse/junk EN tracks (e.g. a single brand watermark cue)."""
    if len(english) < min_cues:
        return False
    # Must cover a meaningful share of the indexed talk — otherwise keep source
    # cues and rely on Gemini translate for display English.
    if indexed and len(english) < max(min_cues, len(indexed) // 5):
        return False
    # Require some real words, not just URLs / logos.
    wordy = 0
    for _, _, t in english:
        if len((t or "").split()) >= 3:
            wordy += 1
        if wordy >= min_cues // 2:
            return True
    return wordy >= max(4, min_cues // 3)


def _select_carousel_cue_corpus(
    indexed: list[tuple[float, float | None, str]],
    english: list[tuple[float, float | None, str]] | None,
) -> tuple[list[tuple[float, float | None, str]], bool]:
    """Pick cue corpus for cut-planning. Never let a thin EN track wipe the VTT."""
    if english and _english_caption_track_usable(english, indexed):
        preferred = prefer_english_cues(english)
        if len(preferred) >= 6:
            return preferred, True
    return prefer_english_cues(indexed), False


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
    if not _english_caption_track_usable(english, cues):
        logger.info(
            "Ignoring sparse English caption track (%d cues vs %d indexed) for %s",
            len(english),
            len(cues),
            yt_id,
        )
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
        .where(
            VideoSegment.media_id == media.id,
            or_(VideoSegment.text != "", VideoSegment.vlm_description != ""),
        )
        .order_by(VideoSegment.start_sec)
    )
    segments = list(seg_result.scalars().all())
    cues = [
        (
            float(s.start_sec),
            float(s.end_sec) if s.end_sec is not None else None,
            (s.text or "").strip() or (s.vlm_description or "").strip(),
        )
        for s in segments
        if (s.text or "").strip() or (s.vlm_description or "").strip()
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


@router.post("/pipeline/generate")
async def carousel_pipeline_generate(
    body: CarouselGenerateRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    drive_file_id = body.drive_file_id.strip()
    token = await _claim_carousel(session, drive_file_id)
    try:
        return await _carousel_pipeline_generate_impl(body, session)
    finally:
        await _release_carousel(session, drive_file_id, token)


async def _build_hook_carousels(
    *,
    unique_hooks: list["TimedPick"],
    cue_corpus: list[tuple[float, float | None, str]],
    drive_file_id: str,
    video_name: str,
    min_slides: int,
    max_slides: int,
    select_images: bool,
    api_key: str | None,
    model: str | None,
) -> list[dict[str, Any]]:
    """One carousel per hook, built sequentially against a shared reserved pool.

    Hooks are built in order with a shared pool of claimed lines/timestamps so
    two hooks never steal the same transcript line (root cause of cross-hook
    duplicate slides in the UI).
    """
    reserved_texts: set[str] = set()
    reserved_starts: set[float] = set()
    carousels: list[dict[str, Any]] = []

    for idx, hook in enumerate(unique_hooks):
        hs, he = _anchor_hook_span(hook, cue_corpus)
        anchored = hook.model_copy(update={"start_sec": hs, "end_sec": he})
        plan = await _plan_hook_oneline_spans(
            cues=cue_corpus,
            hook=anchored,
            min_slides=min_slides,
            max_slides=max_slides,
            api_key=api_key,
            model=model,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        spans = list(plan.get("spans") or [])
        if len(spans) < 2:
            logger.warning("hook carousel skipped (thin plan) id=%s", hook.id or idx)
            continue
        slides = _slides_from_exact_spans(
            spans,
            cues=cue_corpus,
            drive_file_id=drive_file_id,
            video_name=video_name,
            crafted_hook=hook.text,
            defer_images=not select_images,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        slides = _top_up_oneline_slides(
            slides,
            cues=cue_corpus,
            hook=anchored,
            min_slides=min_slides,
            max_slides=max_slides,
            drive_file_id=drive_file_id,
            video_name=video_name,
            defer_images=not select_images,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        if len(slides) < min_slides:
            logger.warning(
                "hook carousel underfilled id=%s slides=%d<%d",
                hook.id or idx,
                len(slides),
                min_slides,
            )
        if len(slides) < 2:
            continue
        # Claim lines/timestamps so later hooks cannot reuse them.
        for s in slides:
            key = (s.get("transcript_text") or s.get("hook_line") or "").strip().lower()
            if key:
                reserved_texts.add(key)
            try:
                reserved_starts.add(round(float(s.get("timestamp_sec") or 0), 1))
            except (TypeError, ValueError):
                pass
        base = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
        hook_label = _complete_line((hook.text or "Hook").strip(), max_len=72)
        title = _complete_line(f"{base} — {hook_label}", max_len=160)
        carousels.append(
            {
                "id": f"hook_{idx + 1}",
                "kind": "hook",
                "title": title,
                "topic_labels": [hook.topic_text] if (hook.topic_text or "").strip() else [],
                "slide_count": len(slides),
                "slides": slides,
                "hooks": [hook.text],
                "hook_goal": hook.text,
                "topics": [hook.topic_text] if (hook.topic_text or "").strip() else [],
                "plan_source": plan.get("source"),
                "images_ready": select_images,
                "hook_start_sec": hs,
                "hook_end_sec": he,
            }
        )
    return carousels


async def _carousel_pipeline_generate_impl(
    body: CarouselGenerateRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Generate Instagram-style carousels: one group per selected hook.

    Each hook gets ≥6 one-line slides of *exact* VTT text. Gemini (when available)
    only proposes cut timestamps; the server never displays rewritten copy.
    """
    settings = get_settings()
    drive_file_id = body.drive_file_id.strip()
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")

    video_name = (body.video_name or "").strip()
    if not video_name:
        drive_file = await session.get(DriveFile, drive_file_id)
        video_name = (drive_file.name if drive_file else "") or drive_file_id

    topics = [t for t in (body.topics or []) if (t.text or "").strip()]
    hooks = [h for h in (body.hooks or []) if (h.text or "").strip()]
    if not topics and not hooks:
        raise HTTPException(status_code=400, detail="Select at least one topic or hook")

    # Product rule: ≥6 one-liners per hook group when cues allow.
    min_slides = min(max(int(body.min_slides), 6), 12)
    max_slides = min(max(int(body.max_slides), min_slides), 12)
    select_images = bool(body.select_images)

    drive_file, indexed_cues = await _load_video_cues(session, drive_file_id)
    if not video_name or video_name == drive_file_id:
        video_name = (drive_file.name if drive_file else "") or drive_file_id
    english_cues = await _maybe_load_english_cues(drive_file, indexed_cues)
    cue_corpus, used_english_track = _select_carousel_cue_corpus(indexed_cues, english_cues)
    if len(cue_corpus) < 2:
        raise HTTPException(
            status_code=400,
            detail="Not enough transcript cues to build carousels for this video",
        )

    # One carousel per selected hook. If only topics were picked, treat each as a goal.
    hook_goals: list[TimedPick] = list(hooks) if hooks else list(topics)
    # De-dupe by normalized text while preserving order.
    seen_goals: set[str] = set()
    unique_hooks: list[TimedPick] = []
    for h in hook_goals:
        key = " ".join((h.text or "").lower().split())
        if not key or key in seen_goals:
            continue
        seen_goals.add(key)
        unique_hooks.append(h)

    logger.info(
        "carousel generate drive=%s hooks=%d topics=%d min_slides=%d max_slides=%d select_images=%s cues=%d",
        drive_file_id,
        len(unique_hooks),
        len(topics),
        min_slides,
        max_slides,
        select_images,
        len(cue_corpus),
    )

    _RELAXED_CUE_LINES.set(_cue_corpus_needs_relaxed_lines(cue_corpus))
    carousels = await _build_hook_carousels(
        unique_hooks=unique_hooks,
        cue_corpus=cue_corpus,
        drive_file_id=drive_file_id,
        video_name=video_name,
        min_slides=min_slides,
        max_slides=max_slides,
        select_images=select_images,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    if not carousels and not _RELAXED_CUE_LINES.get():
        # Punctuation/casing gates can starve every hook on transcripts that
        # only look punctuated; retry once relaxed before failing the request.
        logger.warning(
            "carousel generate produced nothing; retrying relaxed drive=%s", drive_file_id
        )
        _RELAXED_CUE_LINES.set(True)
        carousels = await _build_hook_carousels(
            unique_hooks=unique_hooks,
            cue_corpus=cue_corpus,
            drive_file_id=drive_file_id,
            video_name=video_name,
            min_slides=min_slides,
            max_slides=max_slides,
            select_images=select_images,
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )

    if not carousels:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not build any carousels from selection — transcript lines around "
                "these hooks were too short or fragmented. Try other hooks or topics."
            ),
        )

    # Hindi/non-English one-liners → faithful English for display/edit (cuts stay timed).
    carousels, translate_meta = await _ensure_english_carousel_slides(
        carousels,
        drive_file_id=drive_file_id,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        used_english_track=used_english_track,
    )
    if select_images:
        # One grouped frame pass for all carousel slides; the selector itself
        # enforces the hard three-request Gemini cap.
        flat_slides = [
            slide
            for carousel in carousels
            for slide in (carousel.get("slides") or [])
        ]
        polished = await _polish_outline_frames(flat_slides, session)
        cursor = 0
        for carousel in carousels:
            count = len(carousel.get("slides") or [])
            carousel["slides"] = polished[cursor : cursor + count]
            carousel["slide_count"] = count
            cursor += count
    _attach_layout_panels(carousels)
    frames_prewarmed = await _prewarm_carousel_frames(carousels, session, settings)

    primary = carousels[0]
    result = {
        "source": "hook_oneline_carousels",
        "title": primary["title"],
        "slide_count": primary["slide_count"],
        "hooks": [h.text for h in unique_hooks],
        "topics": [t.text for t in topics],
        "slides": primary["slides"],
        "carousels": carousels,
        "carousel_count": len(carousels),
        "images_ready": select_images,
        "frames_prewarmed": frames_prewarmed,
        "intent": (body.intent or "").strip() or None,
        "transcript_language": translate_meta,
        "layouts": {
            "single_1": {
                "layout_mode": "single_1",
                "carousels": _layout_carousels(carousels, split=False),
            },
            "split_2": {
                "layout_mode": "split_2",
                "carousels": _layout_carousels(carousels, split=True),
            },
        },
    }
    try:
        save = await _persist_carousel_artifact(
            session,
            drive_file_id=drive_file_id,
            payload=result,
            source="generate",
        )
        result["save_id"] = save.id if save else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel artifact save failed: %s", exc)
        await session.rollback()
    return result


@router.post("/pipeline/select-images")
async def carousel_pipeline_select_images(
    body: CarouselSelectImagesBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    drive_file_id = body.drive_file_id.strip()
    token = await _claim_carousel(session, drive_file_id)
    try:
        return await _carousel_pipeline_select_images_impl(body, session)
    finally:
        await _release_carousel(session, drive_file_id, token)


async def _carousel_pipeline_select_images_impl(
    body: CarouselSelectImagesBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Final image selection pass after the user reviewed/edited slide transcripts."""
    drive_file_id = body.drive_file_id.strip()
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    raw = list(body.carousels or [])
    if not raw:
        raise HTTPException(status_code=400, detail="No carousels to polish")

    polished: list[dict[str, Any]] = [dict(car) for car in raw]
    quality_rollup: list[dict[str, Any]] = []
    all_slides: list[dict[str, Any]] = []
    slide_locations: list[tuple[int, int]] = []
    for car_index, item in enumerate(polished):
        slides = list(item.get("slides") or [])
        # Align frame search windows to (possibly edited) transcript timing/text.
        for s in slides:
            s["drive_file_id"] = s.get("drive_file_id") or drive_file_id
            edited = (s.get("transcript_text") or s.get("hook_line") or "").strip()
            if edited:
                s["transcript_text"] = edited
                s["hook_line"] = edited
                s["snippet"] = edited
            slide_locations.append((car_index, len(all_slides)))
            all_slides.append(s)

    # Harvest all slides locally and rank in grouped requests. This keeps the
    # default image pass at the hard three-call Gemini cap even with several
    # carousel tabs.
    selected_slides = await _polish_outline_frames(all_slides, session)
    for s in selected_slides:
        if isinstance(s.get("frame_quality"), dict):
            quality_rollup.append(s["frame_quality"])
    for (car_index, flat_index), slide in zip(slide_locations, selected_slides):
        car_slides = polished[car_index].setdefault("slides", [])
        local_index = sum(1 for c, _ in slide_locations[:flat_index] if c == car_index)
        if local_index < len(car_slides):
            car_slides[local_index] = slide
    for item in polished:
        slides = list(item.get("slides") or [])
        _attach_layout_panels([{"slides": slides}])
        await _prewarm_carousel_frames([{"slides": slides}], session, get_settings())
        item["slides"] = slides
        item["slide_count"] = len(slides)
        item["images_ready"] = True

    rejected: dict[str, int] = {}
    candidates = kept = 0
    for q in quality_rollup:
        candidates += int(q.get("candidates") or 0)
        kept += int(q.get("kept") or 0)
        for k, v in (q.get("rejected") or {}).items():
            rejected[str(k)] = rejected.get(str(k), 0) + int(v or 0)

    primary = polished[0]
    result = {
        "source": "select_images",
        "drive_file_id": drive_file_id,
        "carousels": polished,
        "carousel_count": len(polished),
        "images_ready": True,
        "frames_prewarmed": True,
        "title": primary.get("title"),
        "slides": primary.get("slides") or [],
        "slide_count": primary.get("slide_count") or 0,
        "layouts": {
            "single_1": {
                "layout_mode": "single_1",
                "carousels": _layout_carousels(polished, split=False),
            },
            "split_2": {
                "layout_mode": "split_2",
                "carousels": _layout_carousels(polished, split=True),
            },
        },
        "quality": {
            "candidates": candidates,
            "kept": kept,
            "rejected": rejected,
            "slides_polished": sum(len(c.get("slides") or []) for c in polished),
        },
    }
    try:
        save = await _persist_carousel_artifact(
            session,
            drive_file_id=drive_file_id,
            payload=result,
            source="select_images",
        )
        result["save_id"] = save.id if save else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("selected carousel artifact save failed: %s", exc)
        await session.rollback()
    return result


def _expand_carousel_seeds(
    *,
    topics: list[TimedPick],
    hooks: list[TimedPick],
) -> list[TimedPick]:
    """Unique topic seeds: explicit topics first, then parents implied by hooks."""
    seeds: list[TimedPick] = []
    seen: set[str] = set()

    def _key(text: str) -> str:
        return " ".join((text or "").lower().split())

    def _add(pick: TimedPick) -> None:
        k = _key(pick.text)
        if not k or k in seen:
            return
        seen.add(k)
        seeds.append(pick)

    for t in topics:
        _add(t)

    # Hooks under distinct parents → one carousel per parent topic.
    for h in hooks:
        parent = (h.topic_text or "").strip()
        if not parent:
            continue
        k = _key(parent)
        if k in seen:
            continue
        _add(
            TimedPick(
                id=(h.topic_id or f"topic_from_{h.id or 'hook'}")[:64],
                text=parent[:400],
                start_sec=float(h.start_sec or 0),
                end_sec=h.end_sec,
                theme_id=h.theme_id,
                time_ranges=(
                    [TimedRange(start_sec=float(h.start_sec or 0), end_sec=h.end_sec)]
                    if h.end_sec is not None or h.start_sec
                    else []
                ),
            )
        )

    if seeds:
        return seeds

    # No topics at all — each selected hook becomes its own carousel seed.
    for i, h in enumerate(hooks):
        _add(
            TimedPick(
                id=h.id or f"hook_{i}",
                text=h.text,
                start_sec=h.start_sec,
                end_sec=h.end_sec,
                theme_id=h.theme_id,
            )
        )
    return seeds


def _pick_end(start: float, end: float | None, *, default_span: float = 4.0) -> float:
    s = float(start or 0)
    if end is not None:
        try:
            e = float(end)
            if e > s:
                return e
        except (TypeError, ValueError):
            pass
    return s + default_span


def _topic_windows(topic: TimedPick) -> list[tuple[float, float]]:
    """Primary span plus any non-contiguous time_ranges for a topic."""
    windows: list[tuple[float, float]] = []
    for tr in topic.time_ranges or []:
        s = float(tr.start_sec or 0)
        e = _pick_end(s, tr.end_sec, default_span=10.0)
        if e > s:
            windows.append((s, e))
    t_start = float(topic.start_sec or 0)
    t_end = _pick_end(t_start, topic.end_sec, default_span=12.0)
    if not any(abs(s - t_start) < 0.05 for s, _ in windows):
        windows.insert(0, (t_start, t_end))
    if not windows:
        windows.append((t_start, t_end))
    windows.sort(key=lambda w: w[0])
    return windows


def _beats_for_topic(
    *,
    topic: TimedPick,
    hooks: list[TimedPick],
    themes: list[PipelineThemeSlice],
    video_id: str,
    video_name: str,
    min_slides: int,
    max_slides: int,
) -> list[SnapshotContext]:
    """Multi-slide beats for one topic carousel — hook/transcript text only (never topic titles)."""
    windows = _topic_windows(topic)
    t_start, _t_end = windows[0]
    win_start = min(s for s, _ in windows)
    win_end = max(e for _, e in windows)

    # Prefer hooks that name this topic as parent; else time-window match.
    topic_key = " ".join((topic.text or "").lower().split())
    parent_hooks = [
        h
        for h in hooks
        if " ".join((h.topic_text or "").lower().split()) == topic_key
        or ((h.topic_id or "") and (h.topic_id or "") == (topic.id or ""))
    ]

    # Light pad — do not swallow the whole theme (that collapsed multi-topic variety).
    pad = max(1.5, min(8.0, (win_end - win_start) * 0.15))
    win_start -= pad
    win_end += pad

    moments: list[SnapshotContext] = []
    used_ts: list[float] = []
    hook_pool: list[TimedPick] = []

    def add_moment(text: str, start: float, end: float | None, kind: str) -> bool:
        if len(moments) >= max_slides:
            return False
        line = (text or "").strip()
        if not line:
            return False
        s = float(start)
        # Avoid near-duplicate frames; keep a tighter stride than before so we can fit more slides.
        if any(abs(s - prev) < 1.0 for prev in used_ts):
            return False
        moments.append(
            SnapshotContext(
                drive_file_id=video_id,
                name=video_name,
                timestamp_sec=s,
                end_timestamp_sec=end,
                snippet=line[:400],
                match_type=kind,
                preview_url=_frame_preview_url(video_id, s, end),
            )
        )
        used_ts.append(s)
        return True

    def nearest_hook_text(at: float) -> tuple[str, float | None]:
        if not hook_pool:
            return "", None
        best = min(hook_pool, key=lambda h: abs(float(h.start_sec or 0) - at))
        return (best.text or "").strip(), best.end_sec

    in_window = sorted(
        (
            h
            for h in hooks
            if win_start - 0.5 <= float(h.start_sec or 0) <= win_end + 0.5
            or any(ws - 0.5 <= float(h.start_sec or 0) <= we + 0.5 for ws, we in windows)
        ),
        key=lambda h: float(h.start_sec or 0),
    )
    # Prefer parent-linked hooks first for this topic's carousel.
    ordered_hooks = list(parent_hooks) + [h for h in in_window if h not in parent_hooks]
    for h in ordered_hooks:
        if len(moments) >= max_slides:
            break
        if add_moment(h.text, float(h.start_sec or 0), h.end_sec, "hook"):
            hook_pool.append(h)

    # Still thin: pull nearest spoken hooks from anywhere in the selection.
    if len(moments) < min_slides:
        nearest = sorted(
            (h for h in hooks if h not in ordered_hooks),
            key=lambda h: abs(float(h.start_sec or 0) - t_start),
        )
        for h in nearest:
            if len(moments) >= min_slides:
                break
            if add_moment(h.text, float(h.start_sec or 0), h.end_sec, "hook"):
                hook_pool.append(h)

    # Fill remaining slots by subdividing windows — reuse nearest hook/transcript text
    # (never topic titles like "Why X matters").
    if len(moments) < max_slides and hook_pool:
        need = max_slides - len(moments)
        per_window = max(1, (need + len(windows) - 1) // len(windows))
        filled = 0
        for ws, we in windows:
            if filled >= need or len(moments) >= max_slides:
                break
            span = max(3.0, we - ws)
            stride = max(3.0, span / (per_window + 1))
            for i in range(per_window):
                if filled >= need or len(moments) >= max_slides:
                    break
                s = round(ws + stride * (i + 1), 2)
                if s >= we:
                    continue
                e = round(min(we, s + max(2.5, stride * 0.55)), 2)
                line, end = nearest_hook_text(s)
                if add_moment(line, s, end if end is not None else e, "transcript"):
                    filled += 1

    # A topic can be selected without any crafted hooks. Keep the carousel
    # useful by creating distinct, time-spread transcript-context beats.
    if len(moments) < min_slides and not hook_pool:
        needed = min(max_slides, min_slides) - len(moments)
        span = max(win_end - win_start, max(6.0, (needed + 1) * 1.75))
        stride = span / max(needed + 1, 2)
        for i in range(needed):
            s = round(win_start + stride * (i + 1), 2)
            line = f"Transcript context at {s:.1f}s"
            add_moment(line, s, round(s + min(3.0, stride), 2), "transcript")

    # Last resort: cycle selected hooks if still short (same text, distinct times).
    if len(moments) < min_slides and hook_pool:
        i = 0
        while len(moments) < min_slides and i < min_slides * 3:
            h = hook_pool[i % len(hook_pool)]
            s = round(float(h.start_sec or t_start) + 1.25 * (i + 1), 2)
            e = h.end_sec if h.end_sec is not None else round(s + 3.0, 2)
            add_moment(h.text, s, e, "hook")
            i += 1

    moments.sort(key=lambda m: float(m.timestamp_sec or 0))
    return moments[:max_slides]


def _covering_theme(
    start_sec: float,
    themes: list[PipelineThemeSlice],
) -> PipelineThemeSlice | None:
    for t in themes:
        t_start = float(t.start_sec or 0)
        t_end = t.end_sec
        if start_sec >= t_start - 0.5 and (t_end is None or start_sec <= float(t_end) + 0.5):
            return t
    return themes[0] if themes else None


def _mixed_topic_groups(seeds: list[TimedPick]) -> list[list[TimedPick]]:
    """Build cohesive mixed groups: adjacent pairs + optional full-set narrative."""
    ordered = sorted(seeds, key=lambda t: float(t.start_sec or 0))
    groups: list[list[TimedPick]] = []
    # Adjacent pairs (narrative neighbors in time).
    for i in range(len(ordered) - 1):
        groups.append([ordered[i], ordered[i + 1]])
    # Full set when 3–5 topics (one cohesive multi-topic story).
    if 3 <= len(ordered) <= 5:
        groups.append(list(ordered))
    elif len(ordered) > 5:
        # First half + second half as two broader mixes.
        mid = len(ordered) // 2
        groups.append(ordered[: mid + 1])
        groups.append(ordered[mid:])
    # Cap mixed carousels so we don't explode.
    return groups[:4]


def _beats_for_mixed_topics(
    *,
    group: list[TimedPick],
    hooks: list[TimedPick],
    video_id: str,
    video_name: str,
    min_slides: int,
    max_slides: int,
) -> list[SnapshotContext]:
    """Mixed-topic carousel beats from hook/transcript lines only (no topic-title slides)."""
    moments: list[SnapshotContext] = []
    g_start = min(float(t.start_sec or 0) for t in group)
    g_end = max(_pick_end(float(t.start_sec or 0), t.end_sec) for t in group)

    def add(
        text: str,
        start: float,
        end: float | None,
        kind: str,
    ) -> None:
        if len(moments) >= max_slides:
            return
        line = (text or "").strip()
        if not line:
            return
        s = float(start)
        if any(abs(s - float(m.timestamp_sec or 0)) < 1.5 for m in moments):
            return
        moments.append(
            SnapshotContext(
                drive_file_id=video_id,
                name=video_name,
                timestamp_sec=s,
                end_timestamp_sec=end,
                snippet=line[:400],
                match_type=kind,
                preview_url=_frame_preview_url(video_id, s, end),
            )
        )

    related = [
        h
        for h in hooks
        if g_start - 1.0 <= float(h.start_sec or 0) <= g_end + 1.0
    ]
    # Preserve topic provenance for mixed carousels; hooks remain the
    # preferred editable copy when available.
    for topic in group:
        add(topic.text, float(topic.start_sec or 0), topic.end_sec, "topic")
    # Prefer hooks whose parent topic is in this mixed group.
    group_keys = {" ".join((t.text or "").lower().split()) for t in group}
    group_ids = {t.id for t in group if t.id}
    parent_linked = [
        h
        for h in hooks
        if " ".join((h.topic_text or "").lower().split()) in group_keys
        or ((h.topic_id or "") in group_ids)
    ]
    ordered = sorted(
        {id(h): h for h in (parent_linked + related)}.values(),
        key=lambda h: float(h.start_sec or 0),
    )
    for h in ordered:
        add(h.text, float(h.start_sec or 0), h.end_sec, "hook")

    # Spread remaining selected hooks if still thin.
    if len(moments) < min_slides:
        extras = sorted(
            (h for h in hooks if h not in ordered),
            key=lambda h: abs(float(h.start_sec or 0) - g_start),
        )
        for h in extras:
            if len(moments) >= min_slides:
                break
            add(h.text, float(h.start_sec or 0), h.end_sec, "hook")

    # Time-fill with nearest hook transcript — never topic titles.
    if len(moments) < max_slides and ordered:
        need = max_slides - len(moments)
        stride = max(5.0, (g_end - g_start) / (need + 1))
        for i in range(need):
            if len(moments) >= max_slides:
                break
            s = round(g_start + stride * (i + 1), 2)
            e = round(s + max(3.0, stride * 0.6), 2)
            h = min(ordered, key=lambda x: abs(float(x.start_sec or 0) - s))
            add(h.text, s, h.end_sec if h.end_sec is not None else e, "transcript")

    moments.sort(key=lambda m: float(m.timestamp_sec or 0))
    return moments[:max_slides]


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
        from app.search.carousel_frame_select import focal_point_for_slide
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
            if s.get("frame_ts") is None:
                s["frame_ts"] = _frame_ts(
                    float(s.get("timestamp_sec") or 0),
                    s.get("end_timestamp_sec"),
                )
            ts = float(s.get("frame_ts") or 0)
            fx, fy, fs = focal_point_for_slide(s, ts)
            s.setdefault("focal_x", fx)
            s.setdefault("focal_y", fy)
            s.setdefault("front_face_score", fs)
        return slides

    async def ensure_frame(drive_file_id: str, ts: float) -> bytes | None:
        return await _ensure_outline_frame_bytes(drive_file_id, ts, session, settings)

    try:
        # Feed indexed InsightFace detections into the local candidate scorer.
        # Older rows have no yaw; confidence and normalized box area still
        # provide a useful front-facing/portrait prior.
        face_rows: dict[str, list[dict[str, Any]]] = {}
        for fid in {str(s.get("drive_file_id") or "") for s in slides}:
            if not fid:
                continue
            media = await session.scalar(select(Media).where(Media.drive_file_id == fid))
            if media is None:
                continue
            faces = list(
                (
                    await session.execute(
                        select(Face).where(
                            Face.media_id == media.id,
                            Face.frame_timestamp.is_not(None),
                        )
                    )
                ).scalars().all()
            )
            face_rows[fid] = [
                {
                    "timestamp_sec": f.frame_timestamp,
                    "bbox_x": f.bbox_x,
                    "bbox_y": f.bbox_y,
                    "bbox_width": f.bbox_width,
                    "bbox_height": f.bbox_height,
                    "detection_confidence": f.detection_confidence,
                    "yaw": getattr(f, "yaw", None),
                    "pitch": getattr(f, "pitch", None),
                    "roll": getattr(f, "roll", None),
                }
                for f in faces
            ]
        for slide in slides:
            fid = str(slide.get("drive_file_id") or "")
            if face_rows.get(fid):
                slide["faces"] = face_rows[fid]
        return await polish_slides_instagram_frames(
            slides,
            thumbnail_dir=str(settings.thumbnail_dir),
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_candidates=12,
            ensure_frame=ensure_frame,
            concurrency=3,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instagram frame polish skipped: %s", exc)
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
        return slides


async def _prewarm_carousel_frames(
    carousels: list[dict[str, Any]],
    session: AsyncSession,
    settings,
) -> bool:
    """Materialize every selected carousel JPEG before publishing a ready artifact."""
    from app.search.carousel_frame_select import cached_frame_path

    missing: list[str] = []
    for carousel in carousels:
        carousel_missing = False
        frame_items = list(carousel.get("slides") or [])
        for slide in frame_items:
            frame_items.extend(slide.get("panels") or [])
            frame_items.extend(slide.get("_split_panels") or [])
        for slide in frame_items:
            fid = str(slide.get("drive_file_id") or "").strip()
            if not fid:
                missing.append("missing drive_file_id")
                carousel_missing = True
                continue
            ts = slide.get("frame_ts")
            if ts is None:
                ts = _frame_ts(float(slide.get("timestamp_sec") or 0), slide.get("end_timestamp_sec"))
                slide["frame_ts"] = ts
            data = await _ensure_outline_frame_bytes(fid, float(ts), session, settings)
            if not data:
                missing.append(f"{fid}@{float(ts):.3f}")
                carousel_missing = True
                continue
            path = cached_frame_path(str(settings.thumbnail_dir), fid, float(ts))
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file():
                path.write_bytes(data)
            slide["preview_url"] = f"/media/video/{fid}/frame?ts={float(ts):.3f}&cache_only=1"
        carousel["frames_prewarmed"] = not carousel_missing
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Could not prewarm carousel frames ({len(missing)} missing)",
        )
    return True


def _split_exact_caption_lines(text: str) -> tuple[str, str]:
    """Split existing transcript/copy only; never synthesize panel wording."""
    import re

    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return "", ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return cleaned, cleaned


def _attach_layout_panels(carousels: list[dict[str, Any]]) -> None:
    """Attach cache-backed single and two-panel layout metadata to slides."""
    from app.search.carousel_frame_select import focal_point_for_slide

    for carousel in carousels:
        for slide in carousel.get("slides") or []:
            fid = str(slide.get("drive_file_id") or "")
            start = float(slide.get("timestamp_sec") or 0)
            end = slide.get("end_timestamp_sec")
            try:
                end_f = float(end) if end is not None else start
            except (TypeError, ValueError):
                end_f = start
            selected_ts = float(slide.get("frame_ts") or _frame_ts(start, end_f))
            alternate = round(end_f if abs(end_f - selected_ts) >= 0.45 else start, 3)
            if abs(alternate - selected_ts) < 0.01:
                alternate = round(selected_ts + 0.5, 3)
            text = str(
                slide.get("transcript_text")
                or slide.get("hook_line")
                or slide.get("snippet")
                or ""
            )
            top, bottom = _split_exact_caption_lines(text)
            fx, fy, fs = focal_point_for_slide(slide, selected_ts)
            ax, ay, afs = focal_point_for_slide(slide, alternate)
            def panel(ts: float, caption: str, px: float, py: float, score: float) -> dict[str, Any]:
                return {
                    "drive_file_id": fid,
                    "frame_ts": ts,
                    "preview_url": (
                        f"/media/video/{fid}/frame?ts={ts:.3f}&cache_only=1"
                        if fid else None
                    ),
                    "caption": caption[:400] or None,
                    "focal_x": px,
                    "focal_y": py,
                    "front_face_score": score,
                }
            # single_1 is represented by one panel and the existing bottom
            # caption; split_2 is materialized in the artifact layouts below.
            slide["panels"] = [panel(selected_ts, bottom, fx, fy, fs)]
            slide["_split_panels"] = [
                panel(selected_ts, top, fx, fy, fs),
                panel(alternate, bottom, ax, ay, afs),
            ]


def _layout_carousels(
    carousels: list[dict[str, Any]], *, split: bool
) -> list[dict[str, Any]]:
    """Make a JSON-safe layout view without mutating the single layout."""
    out: list[dict[str, Any]] = []
    for carousel in carousels:
        item = dict(carousel)
        slides: list[dict[str, Any]] = []
        for raw in carousel.get("slides") or []:
            slide = {k: v for k, v in raw.items() if k != "_split_panels"}
            if split:
                slide["panels"] = list(raw.get("_split_panels") or raw.get("panels") or [])[:2]
            else:
                slide["panels"] = list(raw.get("panels") or [])[:1]
            slides.append(slide)
        item["slides"] = slides
        item["slide_count"] = len(slides)
        out.append(item)
    return out


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
    return f"/media/video/{fid}/frame?ts={_frame_ts(start, end)}&cache_only=1"


def _translate_cache_path() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "data"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / "carousel_line_translate_cache.json")


def _load_translate_cache() -> dict[str, str]:
    import json
    from pathlib import Path

    path = Path(_translate_cache_path())
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_translate_cache(cache: dict[str, str]) -> None:
    import json
    from pathlib import Path

    path = Path(_translate_cache_path())
    try:
        # Cap growth — keep newest ~4000 entries.
        items = list(cache.items())
        if len(items) > 4000:
            items = items[-4000:]
            cache = dict(items)
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("translate cache write failed: %s", exc)


async def _ensure_english_carousel_slides(
    carousels: list[dict[str, Any]],
    *,
    drive_file_id: str,
    api_key: str | None,
    model: str | None,
    used_english_track: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translate non-English slide one-liners to English for display (faithful, cached)."""
    meta: dict[str, Any] = {
        "source": "english" if used_english_track else "indexed",
        "translated_slides": 0,
        "cached_hits": 0,
        "any_translated": False,
    }
    if not carousels:
        return carousels, meta

    # Collect unique lines that still need English.
    pending: list[str] = []
    pending_keys: list[str] = []
    cache = _load_translate_cache()
    model_key = (model or "default").strip()

    def cache_key(text: str) -> str:
        raw = f"{model_key}|{drive_file_id}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    for car in carousels:
        for slide in car.get("slides") or []:
            text = str(slide.get("transcript_text") or slide.get("hook_line") or "").strip()
            if not text or not needs_english(text):
                continue
            ck = cache_key(text)
            hit = cache.get(ck)
            if hit and is_english_text(hit):
                slide["original_text"] = slide.get("original_text") or text
                slide["transcript_text"] = hit[:280]
                slide["hook_line"] = hit[:280]
                slide["snippet"] = hit[:280]
                slide["translated"] = True
                slide["english_source"] = "cache"
                meta["cached_hits"] += 1
                meta["translated_slides"] += 1
                meta["any_translated"] = True
                continue
            pending.append(text)
            pending_keys.append(ck)

    if pending and api_key and model:
        # Deduplicate strings for the LLM batch.
        unique: list[str] = []
        uniq_index: dict[str, int] = {}
        for line in pending:
            if line not in uniq_index:
                uniq_index[line] = len(unique)
                unique.append(line)
        try:
            translated = await _llm_translate_lines(
                unique, api_key=api_key, model=model
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("carousel slide translate failed: %s", exc)
            translated = []
        # Map back
        eng_by_src: dict[str, str] = {}
        for src, eng in zip(unique, translated, strict=False):
            eng_s = " ".join((eng or "").split()).strip()
            if eng_s and is_english_text(eng_s):
                eng_by_src[src] = eng_s
        for car in carousels:
            for slide in car.get("slides") or []:
                text = str(slide.get("transcript_text") or slide.get("hook_line") or "").strip()
                if not text or not needs_english(text):
                    continue
                if slide.get("english_source") == "cache":
                    continue
                eng = eng_by_src.get(text)
                if not eng:
                    continue
                ck = cache_key(text)
                cache[ck] = eng
                slide["original_text"] = slide.get("original_text") or text
                slide["transcript_text"] = eng[:280]
                slide["hook_line"] = eng[:280]
                slide["snippet"] = eng[:280]
                slide["translated"] = True
                slide["english_source"] = "llm_translate"
                meta["translated_slides"] += 1
                meta["any_translated"] = True
        if eng_by_src:
            _save_translate_cache(cache)
            meta["source"] = "llm_translate"
    elif used_english_track:
        meta["source"] = "caption_track"
    elif meta["cached_hits"]:
        meta["source"] = "cache"

    return carousels, meta


_ONELINE_MAX_WORDS = 14
_ONELINE_MIN_WORDS = 4
# Latin + Hindi danda / double danda (common in Indic captions).
_SENTENCE_END = r"[.!?…।॥]"
_DANGLING_ENDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "for",
    "with",
    "in",
    "on",
    "at",
    "by",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "will",
    "can",
    "could",
    "would",
    "should",
    "have",
    "has",
    "had",
    "from",
    "into",
    "than",
    "then",
    "that",
    "this",
    "your",
    "our",
    "their",
    "it's",
    "its",
    "as",
    "so",
    "but",
    "if",
    "we",
    "you",
    "i",
    "he",
    "she",
    "they",
    "more",
    "very",
    "just",
    "also",
    "fueling",
    "petrol",
}

# YouTube ASR tracks arrive lowercase and without terminators, so the strict
# sentence gates below reject every candidate line and starve the whole
# carousel. Relaxation is decided per request from the cue corpus.
_RELAXED_CUE_LINES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "carousel_relaxed_cue_lines", default=False
)

# Words that only ever continue a clause, so they cannot open a readable line.
_MIDCLAUSE_OPENERS = {
    "of",
    "to",
    "and",
    "or",
    "for",
    "with",
    "in",
    "on",
    "at",
    "by",
    "from",
    "into",
    "than",
    "then",
    "but",
    "as",
    "so",
    "that",
    "which",
    "who",
    "whom",
    "whose",
    "because",
    "while",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "has",
    "have",
    "had",
    "its",
    "it's",
}

_PRONOUN_OPENERS = {"i", "you", "we", "he", "she", "they", "it", "me", "us", "them", "him", "her"}


def _cue_corpus_needs_relaxed_lines(
    cues: list[tuple[float, float | None, str]],
) -> bool:
    """True when cues look like auto-captions (mostly unterminated lines)."""
    sample = [" ".join((t or "").split()) for _s, _e, t in cues if (t or "").strip()][:400]
    if len(sample) < 4:
        return False
    terminated = sum(
        1 for t in sample if re.search(rf"{_SENTENCE_END}[\"')\]]*$", t)
    )
    return (terminated / len(sample)) < 0.25


def _is_indic_or_non_latin_heavy(text: str) -> bool:
    """True for Devanagari / other non-Latin-heavy cue lines (often lack .!? punctuation)."""
    t = text or ""
    if re.search(r"[\u0900-\u097F]", t):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 3:
        return False
    non_latin = sum(1 for c in letters if ord(c) > 127)
    return (non_latin / len(letters)) >= 0.35


def _line_starts_clean(text: str) -> bool:
    """Accept clean sentence starts in English *and* caseless scripts (e.g. Hindi)."""
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", (text or "").strip())
    if not cleaned:
        return False
    c = cleaned[0]
    if c in "\"'“‘(":
        return True
    # Stats openers are common in edu/Hinglish clips ("8000 प्लस आवर्स…").
    if c.isdigit():
        return True
    # Uppercase Latin, or any letter that is not lowercase (covers Devanagari etc.).
    if c.isalpha() and not c.islower():
        return True
    # Hinglish cues sometimes start with a Latin brand in lowercase ("youtube's …").
    if c.islower() and _is_indic_or_non_latin_heavy(cleaned):
        return True
    # Auto-caption tracks are entirely lowercase, so casing carries no signal;
    # only reject openers that are bare clause continuations.
    if _RELAXED_CUE_LINES.get():
        words = [re.sub(r"[^\w']", "", w.lower()) for w in cleaned.split()]
        first = words[0] if words else ""
        if not first or first in _MIDCLAUSE_OPENERS:
            return False
        # "you for the true business World…" — a pronoun followed by a
        # preposition is always a fragment of the previous clause.
        if first in _PRONOUN_OPENERS and len(words) > 1 and words[1] in _MIDCLAUSE_OPENERS:
            return False
        return True
    return False


def _line_complete_enough(text: str) -> bool:
    """Require a finished spoken unit — punctuation for English; cue-sized for Indic."""
    t = (text or "").strip()
    if not t or len(t.split()) < _ONELINE_MIN_WORDS:
        return False
    ends = re.findall(_SENTENCE_END, t)
    if ends:
        # One carousel line = one sentence/question (not a mini-paragraph).
        if len(ends) != 1:
            return False
        if not re.search(rf"{_SENTENCE_END}[\"')\]]*$", t):
            return False
        last = t.split()[-1].lower().rstrip(".,;:…।॥\"')")
        return last not in _DANGLING_ENDS
    # Many Hindi/Hinglish VTTs and all ASR tracks omit terminators — accept a
    # short clean cue line instead of dropping the transcript wholesale.
    if (_is_indic_or_non_latin_heavy(t) or _RELAXED_CUE_LINES.get()) and _line_starts_clean(t):
        last = t.split()[-1].lower().rstrip(".,;:…।॥\"')")
        if last in _DANGLING_ENDS:
            return False
        return len(t.split()) <= _ONELINE_MAX_WORDS + 2
    return False


def _trim_to_oneline(text: str, *, max_words: int = _ONELINE_MAX_WORDS) -> str:
    """Keep exact words but cut at a natural short boundary for one carousel line."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    # Prefer a single complete sentence/question (never pack multiple thoughts).
    qm = re.search(r"^(.{8,}?[?])(?:\s|$)", cleaned)
    if qm and _ONELINE_MIN_WORDS <= len(qm.group(1).split()) <= max_words + 2:
        return qm.group(1).strip()
    sm = re.search(rf"^(.{{8,}}?[.!…।॥])(?:\s|$)", cleaned)
    if sm and _ONELINE_MIN_WORDS <= len(sm.group(1).split()) <= max_words + 2:
        return sm.group(1).strip()
    # If multiple sentences remain, keep only the first finished one.
    multi = re.match(rf"^(.+?{_SENTENCE_END})\s+\S", cleaned)
    if multi and _ONELINE_MIN_WORDS <= len(multi.group(1).split()) <= max_words + 4:
        return multi.group(1).strip()
    # Drop leading mid-clause residue after a sentence end inside the cue.
    m0 = re.search(rf"{_SENTENCE_END}\s+(\S.*)$", cleaned)
    if m0 and _line_starts_clean(m0.group(1)) and len(m0.group(1).split()) >= _ONELINE_MIN_WORDS:
        cleaned = m0.group(1).strip()
        qm = re.search(r"^(.{8,}?[?])(?:\s|$)", cleaned)
        if qm and len(qm.group(1).split()) <= max_words + 2:
            return qm.group(1).strip()
    words = cleaned.split()
    if len(words) <= max_words:
        out = cleaned
    else:
        cut = " ".join(words[:max_words])
        m = list(re.finditer(r"[,;:—.!?…।॥]", cut))
        if m and m[-1].end() >= 14:
            out = cut[: m[-1].end()].strip().rstrip(",;:")
        else:
            out = cut.strip()
    out_words = out.split()
    while out_words and out_words[-1].lower().rstrip(".,;:।॥") in _DANGLING_ENDS:
        out_words.pop()
    return " ".join(out_words).strip()


def _exact_text_for_span(
    cues: list[tuple[float, float | None, str]],
    start_sec: float,
    end_sec: float,
) -> str:
    """Verbatim one-liner for a cut — prefer a single cue (avoid rolling-caption collapse)."""
    lo = float(start_sec)
    hi = float(end_sec)
    if hi <= lo:
        hi = lo + 2.5

    # Prefer cues that *start* inside the cut (stable one-liners).
    starts_inside = [
        (float(s), e, t)
        for s, e, t in cues
        if (t or "").strip() and lo - 0.2 <= float(s) <= hi + 0.05
    ]
    if starts_inside:
        # Closest start to lo; if rolling, later cues often have fuller text — pick
        # the shortest clean line ≥ min words among the first few.
        starts_inside.sort(key=lambda c: float(c[0]))
        best = ""
        for s, e, t in starts_inside[:6]:
            candidate = _trim_to_oneline(t)
            if len(candidate.split()) < _ONELINE_MIN_WORDS or not _line_starts_clean(candidate):
                continue
            # Prefer complete-ish lines; otherwise keep shortest distinct phrase.
            if not best:
                best = candidate
            elif re.search(r"[.!?…][\"')\]]*$", candidate) and not re.search(
                r"[.!?…][\"')\]]*$", best
            ):
                best = candidate
            elif abs(len(candidate.split()) - 8) < abs(len(best.split()) - 8):
                best = candidate
        if best:
            if not _line_complete_enough(best):
                # Extend with the next cue(s) — still exact words only.
                base_i = next(
                    (
                        i
                        for i, c in enumerate(cues)
                        if abs(float(c[0]) - float(starts_inside[0][0])) < 0.08
                    ),
                    None,
                )
                if base_i is not None:
                    for j in range(base_i + 1, min(base_i + 4, len(cues))):
                        extended = _trim_to_oneline(
                            _join_rolling_cue_texts(cues[base_i : j + 1])
                        )
                        if (
                            extended
                            and _line_starts_clean(extended)
                            and _line_complete_enough(extended)
                            and len(extended.split()) <= _ONELINE_MAX_WORDS + 2
                        ):
                            best = extended
                            break
            best = _trim_to_oneline(best)
            return best

    # Fallback: nearest cue by start.
    nearest = min(cues, key=lambda c: abs(float(c[0]) - lo), default=None)
    if nearest is None:
        return ""
    return _trim_to_oneline(nearest[2])


def _norm_slide_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_reserved_line(
    text: str,
    start: float,
    *,
    reserved_texts: set[str] | None,
    reserved_starts: set[float] | None,
    gap: float = 1.8,
) -> bool:
    """True when this line/time was already claimed by another hook carousel."""
    key = _norm_slide_key(text)
    if reserved_texts and key:
        if key in reserved_texts:
            return True
        for r in reserved_texts:
            if len(r) < 16 or len(key) < 16:
                continue
            if key in r or r in key:
                return True
    if reserved_starts:
        try:
            rs = round(float(start), 1)
        except (TypeError, ValueError):
            return False
        if rs in reserved_starts:
            return True
        if any(abs(rs - x) < gap for x in reserved_starts):
            return True
    return False


# Content stopwords for hook↔cue relevance (Latin). Keep small; Devanagari tokens pass through.
_HOOK_RELEVANCE_STOP = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
        "that", "this", "it", "as", "at", "by", "from", "be", "you", "your", "have", "has",
        "had", "was", "were", "will", "would", "can", "could", "should", "about", "into",
        "they", "their", "them", "what", "when", "where", "which", "who", "how", "why",
        "not", "but", "our", "out", "all", "any", "more", "most", "some", "than", "then",
        "there", "these", "those", "been", "being", "just", "also", "very", "really",
    }
)


def _content_tokens(blob: str, *, min_len: int = 3) -> set[str]:
    """Tokenize for relevance; drop stopwords and very short tokens."""
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9\u0900-\u097f]+", (blob or "").lower()):
        if t in _HOOK_RELEVANCE_STOP:
            continue
        if len(t) < min_len:
            continue
        # Prefer content words (≥4 Latin chars); keep shorter only if not stopword.
        if re.fullmatch(r"[a-z0-9]+", t) and len(t) < 4:
            continue
        out.add(t)
    return out


def _hook_token_set(hook: "TimedPick") -> set[str]:
    """Union crafted hook + topic + spoken seed so rewrites still match transcript cues."""
    # Weight spoken seed by including it twice conceptually via union with crafted text;
    # original_text carries the vocabulary that exact slides share.
    blob = " ".join(
        filter(
            None,
            [
                hook.text or "",
                getattr(hook, "topic_text", None) or "",
                getattr(hook, "original_text", None) or "",
            ],
        )
    )
    return _content_tokens(blob, min_len=3)


def _cue_relevance(text: str, hook_toks: set[str]) -> float:
    """Stopword-aware overlap; soft boost when at least one content token hits."""
    if not hook_toks:
        return 0.0
    toks = _content_tokens(text or "", min_len=3)
    if not toks:
        return 0.0
    hit = len(hook_toks & toks)
    if not hit:
        return 0.0
    return hit / float(max(1, len(hook_toks))) + 0.15


_MIN_CUE_RELEVANCE = 0.12


def _anchor_hook_span(
    hook: "TimedPick",
    cues: list[tuple[float, float | None, str]],
) -> tuple[float, float | None]:
    """Resolve a real cue window for the hook when start_sec is missing/bogus."""
    hs = float(hook.start_sec or 0)
    he = hook.end_sec
    he_f = float(he) if he is not None else None
    if hs >= 1.0 or (he_f is not None and he_f > hs + 0.5):
        return hs, he_f if he_f is not None else hs + 18.0

    hook_toks = _hook_token_set(hook)
    if not hook_toks or not cues:
        return hs, he_f if he_f is not None else (hs + 18.0 if hs > 0 else None)

    best_i: int | None = None
    best_score = 0.0
    for i, (_s, _e, t) in enumerate(cues):
        score = _cue_relevance(t or "", hook_toks)
        if score > best_score:
            best_score = score
            best_i = i
    if best_i is None or best_score < 0.2:
        return hs, he_f if he_f is not None else (hs + 18.0 if hs > 0 else None)

    # Expand a local window around the best-matching cue (± nearby cues).
    lo_i = max(0, best_i - 2)
    hi_i = min(len(cues) - 1, best_i + 8)
    start = float(cues[lo_i][0])
    end_raw = cues[hi_i][1]
    end = float(end_raw) if end_raw is not None else float(cues[hi_i][0]) + 4.0
    if end <= start:
        end = start + 18.0
    return start, end


def _cues_near_hook(
    cues: list[tuple[float, float | None, str]],
    hook_start: float,
    hook_end: float | None,
    *,
    back_sec: float = 8.0,
    forward_sec: float = 36.0,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> list[dict[str, Any]]:
    """Compact cue catalog around a hook for Gemini cut planning."""
    hs = float(hook_start)
    he = float(hook_end) if hook_end is not None else hs + 8.0
    lo, hi = max(0.0, hs - back_sec), he + forward_sec
    out: list[dict[str, Any]] = []
    for i, (s, e, t) in enumerate(cues):
        piece = " ".join((t or "").split()).strip()
        if not piece:
            continue
        ce = float(e) if e is not None else float(s) + 0.4
        if ce < lo or float(s) > hi:
            continue
        if _is_reserved_line(
            piece,
            float(s),
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        ):
            continue
        out.append(
            {
                "i": i,
                "s": round(float(s), 2),
                "e": round(ce, 2),
                "t": piece[:160],
            }
        )
        if len(out) >= 80:
            break
    return out


def _plan_hook_oneline_spans_heuristic(
    cues: list[tuple[float, float | None, str]],
    *,
    hook_start: float,
    hook_end: float | None,
    min_slides: int,
    max_slides: int,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
    hook: "TimedPick | None" = None,
) -> list[dict[str, float]]:
    """Fallback: cut short exact lines from cues around the hook (no LLM rewrite)."""
    hs = float(hook_start)
    he = float(hook_end) if hook_end is not None else hs + 8.0
    catalog = _cues_near_hook(
        cues,
        hs,
        he,
        back_sec=12.0,
        forward_sec=48.0,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )
    if not catalog:
        catalog = _cues_near_hook(
            cues,
            hs,
            he,
            back_sec=22.0,
            forward_sec=70.0,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
    if not catalog:
        return []

    hook_toks = _hook_token_set(hook) if hook is not None else set()
    candidates: list[dict[str, float | str]] = []

    def add_candidate(text: str, start: float, end: float) -> None:
        line = _trim_to_oneline(text)
        if len(line.split()) < _ONELINE_MIN_WORDS or not _line_starts_clean(line):
            return
        if _is_reserved_line(
            line,
            start,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        ):
            return
        candidates.append(
            {"start_sec": float(start), "end_sec": float(end), "text": line}
        )

    # 1) Per-cue lines + sentence splits inside cues.
    for row in catalog:
        piece = str(row["t"])
        add_candidate(piece, float(row["s"]), float(row["e"]))
        parts = re.split(r"(?<=[.!?…])\s+", piece)
        if len(parts) > 1:
            for part in parts:
                add_candidate(part, float(row["s"]), float(row["e"]))

    # 2) Merge adjacent short cues into one-liners.
    for i in range(len(catalog) - 1):
        a, b = catalog[i], catalog[i + 1]
        merged = _trim_to_oneline(f"{a['t']} {b['t']}")
        if _ONELINE_MIN_WORDS <= len(merged.split()) <= _ONELINE_MAX_WORDS:
            add_candidate(merged, float(a["s"]), float(b["e"]))

    # Seed first from cues overlapping the spoken hook span (±1 cue window).
    span_lo, span_hi = hs, he
    if catalog:
        # Expand to neighboring cue boundaries when present.
        near = [c for c in catalog if float(c["e"]) >= hs - 2.0 and float(c["s"]) <= he + 2.0]
        if near:
            span_lo = min(float(c["s"]) for c in near)
            span_hi = max(float(c["e"]) for c in near)

    def _cand_key(c: dict[str, float | str]) -> tuple:
        start = float(c["start_sec"])
        in_span = 0 if span_lo <= start <= span_hi else 1
        return (
            in_span,
            -_cue_relevance(str(c["text"]), hook_toks),
            abs(start - hs),
            start,
        )

    # Prefer: in hook span, then relevant to hook, then near hook time.
    candidates.sort(key=_cand_key)
    picked: list[dict[str, float]] = []
    seen_txt: set[str] = set()
    min_gap = 2.0  # Instagram-style: one idea per slide, no overlapping clips
    for c in candidates:
        key = str(c["text"]).lower()
        if key in seen_txt or any(key in s0 or s0 in key for s0 in seen_txt if len(s0) >= 12):
            continue
        if any(abs(float(c["start_sec"]) - p["start_sec"]) < min_gap for p in picked):
            continue
        # Prefer mild relevance once we have a couple of slides — never starve.
        score = _cue_relevance(str(c["text"]), hook_toks)
        if (
            hook_toks
            and score < _MIN_CUE_RELEVANCE
            and len(picked) >= max(2, min_slides // 2)
        ):
            continue
        seen_txt.add(key)
        picked.append({"start_sec": float(c["start_sec"]), "end_sec": float(c["end_sec"])})
        if len(picked) >= max(max_slides, min_slides):
            break
    picked.sort(key=lambda c: c["start_sec"])
    return picked[: max(max_slides, min_slides)]


async def _plan_hook_oneline_spans_gemini(
    cues: list[tuple[float, float | None, str]],
    *,
    hook: "TimedPick",
    min_slides: int,
    max_slides: int,
    api_key: str,
    model: str,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> list[dict[str, float]] | None:
    """Gemini proposes cut timestamps only; text is filled verbatim later."""
    import json

    from google import genai
    from google.genai import types

    catalog = _cues_near_hook(
        cues,
        float(hook.start_sec or 0),
        hook.end_sec,
        back_sec=10.0,
        forward_sec=42.0,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )
    if len(catalog) < min_slides:
        catalog = _cues_near_hook(
            cues,
            float(hook.start_sec or 0),
            hook.end_sec,
            back_sec=18.0,
            forward_sec=70.0,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
    if len(catalog) < min_slides:
        return None

    reserved_note = ""
    if reserved_starts:
        reserved_note = (
            f"\nDo NOT reuse these already-claimed start times (other hooks): "
            f"{sorted(list(reserved_starts))[:40]}\n"
        )

    prompt = (
        "You plan Instagram carousel CUT POINTS from a video transcript.\n"
        "Carousel best practice: 6–10 slides, ONE idea per slide, short readable lines,\n"
        "hook meaning built across the sequence — never repeat another hook's lines.\n"
        "You must NOT rewrite, paraphrase, or invent spoken words.\n"
        "Return ONLY JSON: {\"spans\":[{\"start_sec\":number,\"end_sec\":number,\"cue_i\":number}]}\n"
        f"Hook / goal to convey (intent only — do not output this as slide text): {hook.text}\n"
        f"Topic context: {getattr(hook, 'topic_text', None) or ''}\n"
        f"Produce between {min_slides} and {max_slides} ordered spans.\n"
        "Rules for each span:\n"
        "- Short enough for ONE carousel line (~3–12 spoken words)\n"
        "- start_sec/end_sec must align to the cue catalog times (use cue_i when possible)\n"
        "- Sequence together must convey THIS hook's meaning in full (not a different hook)\n"
        "- Prefer sentence/clause starts (no mid-clause fragments like starting with 'than'/'and'/'your')\n"
        "- Prefer the most important lines for this hook; skip filler and unrelated tangents\n"
        "- Spans must be chronological with ≥2s gap between starts; no overlapping clips\n"
        f"{reserved_note}\n"
        f"Cue catalog JSON:\n{json.dumps(catalog, ensure_ascii=False)}"
    )
    try:
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
        raw = (resp.text or "").strip()
        if not raw:
            return None
        m = re.search(r"\{[\s\S]*\}", raw)
        parsed = json.loads(m.group() if m else raw)
        rows = parsed.get("spans") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            return None
        by_i = {int(c["i"]): c for c in catalog}
        spans: list[dict[str, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cue_i = row.get("cue_i")
            if cue_i is not None and int(cue_i) in by_i:
                c = by_i[int(cue_i)]
                s, e = float(c["s"]), float(c["e"])
            else:
                try:
                    s = float(row.get("start_sec"))
                    e = float(row.get("end_sec", s + 2.5))
                except (TypeError, ValueError):
                    continue
            if e <= s:
                e = s + 2.0
            # Snap to nearest catalog cue starts when far off
            if catalog and min(abs(s - float(c["s"])) for c in catalog) > 2.5:
                nearest = min(catalog, key=lambda c: abs(float(c["s"]) - s))
                s, e = float(nearest["s"]), float(nearest["e"])
            if reserved_starts and any(
                abs(s - x) < 1.8 for x in reserved_starts
            ):
                continue
            spans.append({"start_sec": s, "end_sec": e})
            if len(spans) >= max_slides:
                break
        # Dedupe near-identical starts (≥2s for one-idea-per-slide)
        uniq: list[dict[str, float]] = []
        for sp in sorted(spans, key=lambda x: x["start_sec"]):
            if any(abs(sp["start_sec"] - u["start_sec"]) < 2.0 for u in uniq):
                continue
            uniq.append(sp)
        return uniq if len(uniq) >= 2 else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("hook oneline span plan (gemini) failed: %s", exc)
        return None


def _merge_span_lists(
    primary: list[dict[str, float]],
    extra: list[dict[str, float]],
    *,
    limit: int,
) -> list[dict[str, float]]:
    out = list(primary)
    for sp in extra:
        if len(out) >= limit:
            break
        if any(abs(sp["start_sec"] - u["start_sec"]) < 2.0 for u in out):
            continue
        out.append(sp)
    out.sort(key=lambda x: x["start_sec"])
    return out[:limit]


async def _plan_hook_oneline_spans(
    *,
    cues: list[tuple[float, float | None, str]],
    hook: "TimedPick",
    min_slides: int,
    max_slides: int,
    api_key: str | None,
    model: str | None,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> dict[str, Any]:
    """Plan ≥6 short exact-transcript spans that convey a hook's goal."""
    source = "heuristic"
    spans: list[dict[str, float]] = []
    hs = float(hook.start_sec or 0)
    he = float(hook.end_sec) if hook.end_sec is not None else hs + 15.0

    def _localize(cands: list[dict[str, float]], forward: float) -> list[dict[str, float]]:
        local_lo = max(0.0, hs - 12.0)
        local_hi = he + forward
        local = [sp for sp in cands if local_lo <= float(sp["start_sec"]) <= local_hi]
        return local

    if api_key and model and cues:
        gem = await _plan_hook_oneline_spans_gemini(
            cues,
            hook=hook,
            min_slides=min_slides,
            max_slides=max_slides,
            api_key=api_key,
            model=model,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        if gem:
            # Prefer cuts near the hook; widen only if the tight window is thin.
            for fwd in (55.0, 90.0, 140.0):
                local = _localize(gem, fwd)
                if len(local) >= min(min_slides, 4) or fwd == 140.0:
                    spans = local if local else gem
                    break
            source = "gemini_cuts"

    # Always run heuristic — either as primary or to top up Gemini's short lists.
    heur = _plan_hook_oneline_spans_heuristic(
        cues,
        hook_start=hs,
        hook_end=he,
        min_slides=max(min_slides, 8),
        max_slides=max_slides,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
        hook=hook,
    )
    if not spans:
        spans = heur
        source = "heuristic"
    elif len(spans) < min_slides:
        spans = _merge_span_lists(spans, heur, limit=max_slides)
        source = "gemini+heuristic"

    # Wider window top-up if still thin (still respect reserved pool).
    if len(spans) < min_slides:
        wider = _plan_hook_oneline_spans_heuristic(
            cues,
            hook_start=max(0.0, hs - 12.0),
            hook_end=he + 55.0,
            min_slides=min_slides,
            max_slides=max_slides,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
            hook=hook,
        )
        spans = _merge_span_lists(spans, wider, limit=max_slides)
        if source == "gemini_cuts":
            source = "gemini+heuristic"
    return {"spans": spans[:max_slides], "source": source}


def _make_oneline_slide(
    *,
    text: str,
    start_sec: float,
    end_sec: float,
    drive_file_id: str,
    video_name: str,
    crafted_hook: str | None,
    defer_images: bool,
    order: int,
) -> dict[str, Any]:
    moment = SnapshotContext(
        drive_file_id=drive_file_id,
        name=video_name,
        timestamp_sec=float(start_sec),
        end_timestamp_sec=float(end_sec),
        snippet=text[:800],
        match_type="transcript",
        preview_url=None,
    )
    slide = _slide_from_moment(
        order=order,
        moment=moment,
        moment_index=order - 1,
        hook_line=text,
        caption=None,
    )
    slide["hook_line"] = text[:280]
    slide["transcript_text"] = text[:280]
    slide["snippet"] = text[:280]
    slide["match_type"] = "transcript"
    if crafted_hook and crafted_hook.strip().lower() != text.strip().lower():
        slide["crafted_hook"] = crafted_hook.strip()[:400]
    slide["images_ready"] = not defer_images
    if defer_images:
        slide["preview_url"] = None
        slide["frame_source"] = "deferred"
        slide["instagram_ready"] = False
    return slide


def _slides_from_exact_spans(
    spans: list[dict[str, float]],
    *,
    cues: list[tuple[float, float | None, str]],
    drive_file_id: str,
    video_name: str,
    crafted_hook: str | None,
    defer_images: bool,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> list[dict[str, Any]]:
    """Materialize one-line slides from exact cue spans; drop mid-clause junk."""
    slides: list[dict[str, Any]] = []
    seen: set[str] = set()
    used_ts: list[float] = []
    for sp in spans:
        s = float(sp.get("start_sec") or 0)
        e = float(sp.get("end_sec") or s + 2.5)
        text = _exact_text_for_span(cues, s, e)
        if not text or len(text.split()) < _ONELINE_MIN_WORDS:
            continue
        if not _line_starts_clean(text):
            repaired = _exact_text_for_span(cues, max(0.0, s - 2.5), e)
            if repaired and _line_starts_clean(repaired):
                text = repaired
                s = max(0.0, s - 2.5)
            else:
                continue
        if not _line_complete_enough(text):
            continue
        if _is_reserved_line(
            text,
            s,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        ):
            continue
        key = _norm_slide_key(text)
        if key in seen or any(key in s0 or s0 in key for s0 in seen if len(s0) >= 12):
            continue
        if any(abs(s - t) < 2.0 for t in used_ts):
            continue
        seen.add(key)
        used_ts.append(s)
        slides.append(
            _make_oneline_slide(
                text=text,
                start_sec=s,
                end_sec=e,
                drive_file_id=drive_file_id,
                video_name=video_name,
                crafted_hook=crafted_hook,
                defer_images=defer_images,
                order=len(slides) + 1,
            )
        )
    for i, slide in enumerate(slides):
        slide["index"] = i + 1
    return slides


def _sentence_cut_candidates(
    cues: list[tuple[float, float | None, str]],
    *,
    hook_start: float,
    hook_end: float | None,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> list[dict[str, float | str]]:
    """Exact sentence/question fragments near the hook, with approximate times."""
    hs = float(hook_start)
    he = float(hook_end) if hook_end is not None else hs + 8.0
    catalog = _cues_near_hook(
        cues,
        hs,
        he,
        back_sec=18.0,
        forward_sec=55.0,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )
    if not catalog:
        return []
    # Rebuild rolling-deduped stream with timestamps per piece.
    pieces: list[tuple[float, float, str]] = []
    prev = ""
    for row in catalog:
        piece = " ".join(str(row["t"]).split()).strip()
        if not piece:
            continue
        if prev and (piece == prev or prev.startswith(piece)):
            continue
        if prev and piece.startswith(prev) and len(piece) > len(prev):
            # Rolling growth — only keep the delta if it completes a sentence.
            delta = piece[len(prev) :].strip()
            prev = piece
            if delta:
                pieces.append((float(row["s"]), float(row["e"]), piece))
            continue
        prev = piece
        pieces.append((float(row["s"]), float(row["e"]), piece))

    out: list[dict[str, float | str]] = []
    for s, e, piece in pieces:
        # Split into sentence-like units while keeping exact words.
        chunks = re.findall(r"[^.!?…।॥]+[.!?…।॥]+", piece)
        if not chunks and re.search(_SENTENCE_END, piece):
            chunks = [piece]
        for chunk in chunks:
            line = _trim_to_oneline(chunk.strip())
            if not (_line_starts_clean(line) and _line_complete_enough(line)):
                continue
            if _is_reserved_line(
                line,
                float(s),
                reserved_texts=reserved_texts,
                reserved_starts=reserved_starts,
            ):
                continue
            out.append({"start_sec": s, "end_sec": e, "text": line})
    return out


def _top_up_oneline_slides(
    slides: list[dict[str, Any]],
    *,
    cues: list[tuple[float, float | None, str]],
    hook: "TimedPick",
    min_slides: int,
    max_slides: int,
    drive_file_id: str,
    video_name: str,
    defer_images: bool,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> list[dict[str, Any]]:
    """Guarantee ≥ min_slides unique exact one-liners from cues near the hook."""
    # Drop incomplete lines and anything already claimed by another hook.
    slides = [
        s
        for s in slides
        if _line_complete_enough((s.get("transcript_text") or "").strip())
        and _line_starts_clean((s.get("transcript_text") or "").strip())
        and not _is_reserved_line(
            (s.get("transcript_text") or "").strip(),
            float(s.get("timestamp_sec") or 0),
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
    ]
    if len(slides) >= min_slides:
        slides = sorted(slides, key=lambda s: float(s.get("timestamp_sec") or 0))
        for i, s in enumerate(slides[:max_slides]):
            s["index"] = i + 1
        return slides[:max_slides]

    seen = {_norm_slide_key(s.get("transcript_text") or "") for s in slides}
    used_ts = {float(s.get("timestamp_sec") or 0) for s in slides}
    hs = float(hook.start_sec or 0)
    he = float(hook.end_sec) if hook.end_sec is not None else hs + 8.0
    hook_toks = _hook_token_set(hook)
    min_gap = 2.0

    def try_add(text: str, start: float, end: float, *, allow_weak: bool = False) -> bool:
        nonlocal slides
        key = _norm_slide_key(text)
        if not key:
            return False
        if _is_reserved_line(
            text,
            start,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        ):
            return False
        if key in seen or any(key in s0 or s0 in key for s0 in seen if len(s0) >= 12):
            return False
        if any(abs(float(start) - t) < min_gap for t in used_ts):
            return False
        if (
            len(text.split()) < _ONELINE_MIN_WORDS
            or not _line_starts_clean(text)
            or not _line_complete_enough(text)
        ):
            return False
        # Keep each carousel on-topic for its hook (Instagram: one idea sequence).
        score = _cue_relevance(text, hook_toks)
        if (
            hook_toks
            and not allow_weak
            and score < _MIN_CUE_RELEVANCE
            and len(slides) >= 2
        ):
            return False
        seen.add(key)
        used_ts.add(float(start))
        slides.append(
            _make_oneline_slide(
                text=text,
                start_sec=float(start),
                end_sec=float(end),
                drive_file_id=drive_file_id,
                video_name=video_name,
                crafted_hook=hook.text,
                defer_images=defer_images,
                order=len(slides) + 1,
            )
        )
        return True

    # Prefer finished sentences/questions closest to the hook first.
    sentence_cands = _sentence_cut_candidates(
        cues,
        hook_start=hs,
        hook_end=he,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )

    def _near_hook_key(start: float, text: str) -> tuple:
        in_span = 0 if hs - 2.0 <= start <= he + 2.0 else 1
        return (
            in_span,
            -_cue_relevance(text, hook_toks),
            abs(start - hs),
            start,
        )

    sentence_cands.sort(
        key=lambda c: _near_hook_key(float(c["start_sec"]), str(c["text"]))
    )
    for cand in sentence_cands:
        if len(slides) >= min_slides:
            break
        try_add(str(cand["text"]), float(cand["start_sec"]), float(cand["end_sec"]))

    catalog = _cues_near_hook(
        cues,
        hs,
        he,
        back_sec=20.0,
        forward_sec=60.0,
        reserved_texts=reserved_texts,
        reserved_starts=reserved_starts,
    )
    # Prefer in-span + relevant cues near the hook; then chronological fill.
    catalog_sorted = sorted(
        catalog,
        key=lambda row: _near_hook_key(float(row["s"]), str(row["t"])),
    )
    for row in catalog_sorted:
        if len(slides) >= min_slides:
            break
        text = _exact_text_for_span(cues, float(row["s"]), float(row["e"]))
        # Drop shorter prefix duplicates ("…petrol" vs "…petrol pump which is dry?")
        key = _norm_slide_key(text)
        if any(key in s or s in key for s in seen if s != key):
            shorter = next(
                (
                    s
                    for s in list(seen)
                    if s != key and (key.startswith(s) or s.startswith(key))
                ),
                None,
            )
            if shorter and len(key) > len(shorter):
                slides = [
                    sl
                    for sl in slides
                    if _norm_slide_key(sl.get("transcript_text") or "") != shorter
                ]
                seen.discard(shorter)
            else:
                continue
        try_add(text, float(row["s"]), float(row["e"]))

    # Last resort: walk cues from the hook onward — still exclusive / gapped.
    # Prefer on-topic lines; only allow weak/off-topic fillers at the widest pass.
    if len(slides) < min_slides:
        for forward, allow_weak in ((90.0, False), (140.0, False), (220.0, True)):
            local_hi = he + forward
            pool: list[tuple[float, float, str]] = []
            for s, e, raw in cues:
                if float(s) < hs - 5.0 or float(s) > local_hi:
                    continue
                pool.append(
                    (
                        float(s),
                        float(e) if e is not None else float(s) + 2.0,
                        raw or "",
                    )
                )
            pool.sort(
                key=lambda row: (
                    -_cue_relevance(
                        _exact_text_for_span(cues, row[0], row[1]),
                        hook_toks,
                    ),
                    abs(row[0] - hs),
                )
            )
            for s, e, _raw in pool:
                if len(slides) >= min_slides:
                    break
                text = _exact_text_for_span(cues, s, e)
                try_add(text, s, e, allow_weak=allow_weak)
            if len(slides) >= min_slides:
                break

    # Drop trailing weak-relevance fillers if we still meet min_slides without them.
    if hook_toks and len(slides) > min_slides:
        kept = [
            s
            for s in slides
            if _cue_relevance(s.get("transcript_text") or "", hook_toks) >= _MIN_CUE_RELEVANCE
        ]
        if len(kept) >= min_slides:
            slides = kept

    slides = sorted(slides, key=lambda s: float(s.get("timestamp_sec") or 0))
    for i, s in enumerate(slides[:max_slides]):
        s["index"] = i + 1
    return slides[:max_slides]


def _join_rolling_cue_texts(cues: list[tuple[float, float | None, str]]) -> str:
    """Join VTT/cue lines, collapsing YouTube-style rolling duplicates."""
    parts: list[str] = []
    prev = ""
    for _s, _e, raw in cues:
        piece = " ".join((raw or "").split()).strip()
        if not piece:
            continue
        if prev:
            if piece == prev:
                continue
            if piece.startswith(prev) and len(piece) > len(prev):
                parts[-1] = piece
                prev = piece
                continue
            if prev.startswith(piece):
                continue
            # High overlap rolling window (common in auto-captions)
            prev_words = prev.split()
            piece_words = piece.split()
            if len(prev_words) >= 4 and len(piece_words) >= 4:
                overlap = 0
                max_o = min(8, len(prev_words), len(piece_words))
                for k in range(max_o, 2, -1):
                    if prev_words[-k:] == piece_words[:k]:
                        overlap = k
                        break
                if overlap:
                    piece = " ".join(piece_words[overlap:]).strip()
                    if not piece:
                        continue
        parts.append(piece)
        prev = " ".join(parts[-1].split()) if parts else piece
        # Keep prev as the last full joined tail for next overlap check
        prev = parts[-1]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _sentence_count(text: str) -> int:
    return len(re.findall(r"[.!?…](?:\s|$)", text or ""))


def _cue_end(cue: tuple[float, float | None, str]) -> float:
    s, e, _t = cue
    return float(e) if e is not None else float(s) + 0.5


def _verbatim_passage_for_span(
    cues: list[tuple[float, float | None, str]],
    start_sec: float,
    end_sec: float | None,
    *,
    goal_text: str | None = None,
    min_words: int = 18,
    target_sentences: int = 2,
    max_sentences: int = 4,
    max_words: int = 110,
    max_back_sec: float = 15.0,
    max_forward_sec: float = 40.0,
    max_span_sec: float = 45.0,
) -> tuple[str, float, float | None]:
    """Exact spoken transcript expanded until the hook's idea is fully conveyed.

    Anchored to the beat time range. Prefers expanding *forward* to finish the
    thought; expands backward only to escape mid-clause starts. Hard-capped so
    slides stay distinct and never swallow the whole video.
    """
    if not cues:
        return "", float(start_sec), end_sec

    beat_start = float(start_sec)
    beat_end = float(end_sec) if end_sec is not None else beat_start + 6.0
    if beat_end <= beat_start:
        beat_end = beat_start + 6.0
    beat_mid = beat_start + (beat_end - beat_start) * 0.35

    # Absolute rails around the beat (do not walk back to t=0 for every slide).
    hard_lo = max(0.0, beat_start - max_back_sec)
    hard_hi = beat_start + max_forward_sec

    def overlaps(i: int, lo: float, hi: float) -> bool:
        s, e, t = cues[i]
        if not (t or "").strip():
            return False
        return not (_cue_end(cues[i]) < lo - 0.05 or float(s) > hi + 0.05)

    seed_idxs = [i for i in range(len(cues)) if overlaps(i, beat_start - 0.4, beat_end + 0.4)]
    if not seed_idxs:
        nearest = min(range(len(cues)), key=lambda i: abs(float(cues[i][0]) - beat_mid))
        seed_idxs = [nearest]

    left = min(seed_idxs)
    right = max(seed_idxs)

    def window_ok(l: int, r: int) -> bool:
        win_s = float(cues[l][0])
        win_e = _cue_end(cues[r])
        if win_s < hard_lo - 0.05 or win_e > hard_hi + 0.05:
            return False
        if (win_e - win_s) > max_span_sec + 0.05:
            return False
        return True

    def passage(l: int, r: int) -> str:
        return _join_rolling_cue_texts([cues[i] for i in range(l, r + 1)])

    goal = " ".join((goal_text or "").split()).strip()
    goal_terms = [
        w.lower()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", goal)
        if w.lower()
        not in {
            "that",
            "this",
            "with",
            "from",
            "your",
            "have",
            "what",
            "when",
            "where",
            "which",
            "their",
            "about",
            "into",
            "just",
            "like",
            "will",
            "they",
            "them",
            "then",
            "than",
            "been",
            "were",
            "said",
            "also",
            "only",
            "over",
            "such",
            "make",
            "made",
            "more",
            "most",
            "some",
            "very",
            "really",
            "first",
            "stop",
            "forget",
            "don't",
            "never",
        }
    ][:8]

    def starts_ok(t: str) -> bool:
        if not t:
            return False
        # Strip leading music/noise tags for the capitalisation check.
        cleaned = re.sub(r"^\[[^\]]+\]\s*", "", t).strip()
        if not cleaned:
            return False
        return cleaned[0].isupper() or cleaned[0] in "\"'“‘("

    text = passage(left, right)
    guard = 0
    while guard < 60:
        guard += 1
        words = len(text.split()) if text else 0
        sents = _sentence_count(text)
        ends_ok = bool(re.search(r"[.!?…][\"')\]]*$", text)) if text else False
        start_ok = starts_ok(text)
        goal_hit = (
            not goal_terms
            or sum(1 for t in goal_terms if t in text.lower()) >= min(2, len(goal_terms))
        )
        idea_complete = (
            words >= min_words
            and ends_ok
            and start_ok
            and sents >= target_sentences
            and goal_hit
        )
        # Never stop on word/sentence caps while still mid-clause at the start.
        if idea_complete:
            break
        if start_ok and (sents >= max_sentences or words >= max_words):
            break

        expanded = False
        # Prefer fixing a mid-clause start before / while expanding forward.
        if (not start_ok) and left > 0 and window_ok(left - 1, right):
            left -= 1
            expanded = True
        else:
            need_forward = (
                (not ends_ok) or sents < target_sentences or words < min_words or not goal_hit
            )
            if need_forward and right + 1 < len(cues) and window_ok(left, right + 1):
                right += 1
                expanded = True
        if not expanded:
            break
        text = passage(left, right)

    # Final left polish: walk back to a sentence start if still mid-clause.
    polish = 0
    while polish < 20 and text and not starts_ok(text) and left > 0 and window_ok(left - 1, right):
        left -= 1
        text = passage(left, right)
        polish += 1

    # Drop a leading fragment before the first sentence boundary if still awkward.
    if text and not starts_ok(text):
        m = re.search(r"[.!?…]\s+(\S)", text)
        if m and m.start(1) < len(text) // 2:
            text = text[m.start(1) :].strip()

    # Trim to last sentence end inside the budget (keep meaning intact).
    if text and len(text.split()) > max_words:
        cut = " ".join(text.split()[:max_words])
        m = list(re.finditer(r"[.!?…]", cut))
        text = cut[: m[-1].end()].strip() if m else cut.strip()
    elif text and not re.search(r"[.!?…][\"')\]]*$", text):
        m = list(re.finditer(r"[.!?…][\"')\]]*(?:\s|$)", text))
        if m and m[-1].end() >= max(40, len(text) // 3):
            text = text[: m[-1].end()].strip()

    win_s = float(cues[left][0])
    win_e_raw = cues[right][1]
    win_e = float(win_e_raw) if win_e_raw is not None else _cue_end(cues[right])
    return text, win_s, win_e


def _slides_from_timed_picks(
    moments: list[SnapshotContext],
    slide_count: int,
    *,
    defer_images: bool = False,
    cues: list[tuple[float, float | None, str]] | None = None,
) -> list[dict[str, Any]]:
    """One slide per pick: verbatim transcript for the beat span (+ optional frames)."""
    n = min(max(int(slide_count), 1), 10, len(moments) or 1)
    ordered = sorted(enumerate(moments), key=lambda pair: (pair[1].timestamp_sec, pair[0]))
    slides: list[dict[str, Any]] = []
    cue_list = list(cues or [])
    used_spans: list[tuple[float, float]] = []
    for i, (mi, moment) in enumerate(ordered[:n]):
        crafted = (moment.snippet or "").strip()
        start = float(moment.timestamp_sec or 0)
        end = moment.end_timestamp_sec
        # Nudge degenerate / duplicate beat starts so passages stay distinct.
        for prev_s, prev_e in used_spans:
            if abs(start - prev_s) < 1.25:
                start = max(start, prev_e + 0.4)
                if end is not None and float(end) <= start:
                    end = start + 5.0
        verbatim = ""
        span_start, span_end = start, end
        if cue_list:
            verbatim, span_start, span_end = _verbatim_passage_for_span(
                cue_list,
                start,
                end,
                goal_text=crafted,
            )
        line = verbatim or crafted or f"Moment @ {start:.0f}s"
        # Build slide from the (possibly expanded) transcript span.
        moment_for_slide = SnapshotContext(
            drive_file_id=moment.drive_file_id,
            name=moment.name,
            timestamp_sec=span_start if verbatim else start,
            end_timestamp_sec=span_end if verbatim else end,
            snippet=line[:800],
            match_type="transcript",
            preview_url=moment.preview_url,
        )
        slide = _slide_from_moment(
            order=i + 1,
            moment=moment_for_slide,
            moment_index=mi,
            hook_line=line,
            caption=None,
        )
        # Full spoken passage for editable field + overlay (not punchy hook rewrite).
        slide["hook_line"] = line[:1200]
        slide["transcript_text"] = line[:1200]
        slide["snippet"] = line[:1200]
        slide["match_type"] = "transcript"
        if crafted and crafted.strip().lower() != line.strip().lower():
            slide["crafted_hook"] = crafted[:400]
        slide["images_ready"] = not defer_images
        if defer_images:
            slide["preview_url"] = None
            slide["frame_source"] = "deferred"
            slide["instagram_ready"] = False
        slides.append(slide)
        used_spans.append(
            (
                float(slide["timestamp_sec"]),
                float(slide["end_timestamp_sec"] or slide["timestamp_sec"]),
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
            snap["preview_url"] = (
                f"/media/video/{best.drive_file_id}/frame?ts={best.timestamp_sec}&cache_only=1"
            )
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
            preview_url=f"/media/video/{drive_file_id}/frame?ts={ts}&cache_only=1",
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
