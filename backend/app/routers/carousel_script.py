"""Carousel Search script studio: curated hooks/topics + Gemini script drafts."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.carousel_llm import carousel_llm_cache_id, resolve_carousel_llm
from app.db.advisory_locks import (
    LOCK_CAROUSEL_EXTRACT,
    advisory_lock,
    carousel_themes_lock_key,
)
from app.db.models import (
    CarouselGenerationSave,
    CarouselItemFeedback,
    CarouselItemReference,
    DriveFile,
    DriveFileStatus,
    Face,
    Media,
    Person,
    VideoSegment,
)
from app.db.session import get_db, get_session_factory
from app.search.transcript_topics import (
    analyze_transcript_topics,
    compact_transcript,
    fallback_topics_from_cues,
)
from app.search.carousel_pipeline import (
    EXTRACT_PROMPT_VERSION,
    SLIDE_COPY_PROMPT_VERSION,
    THEME_PROMPT_VERSION,
    _carousel_idea_similarity,
    _carousel_quality_text,
    _heuristic_hook_line,
    _hook_is_readable,
    _hook_numbers_are_grounded,
    apply_carousel_quality_pass,
    build_harmonized_themes,
    craft_hooks_for_selected_topics_async,
    cue_preview_lines,
    deduce_directional_intent,
    extract_hooks_and_topics_async,
    find_duplicate_slide_pairs,
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

CAROUSEL_CUT_PROMPT_VERSION = "cuts-v4-theme-topic-story"

# Process-local fast path + Postgres advisory lock for Gunicorn multi-worker.
# Studio remounts / e2e retries used to pile up overlapping extracts and starve
# health + other carousel routes across all 24 workers.
_EXTRACT_LOCK = asyncio.Lock()
_EXTRACT_TIMEOUT_SEC = 900.0
# Interactive select-images must finish before Railway's public edge (~60–100s)
# cancels the socket. Gemini ranking + ffmpeg prewarm used to overrun that and
# surface as an unhandled TimeoutError 500. Local cache ranking is enough here;
# generate still uses Gemini when the caller asks.
_SELECT_IMAGES_TIMEOUT_SEC = 60.0
_SELECT_IMAGES_REQUEST_TIMEOUT_SEC = 75.0
_SELECT_IMAGES_RANK_BATCHES = 3
_SELECT_IMAGES_FACE_WINDOW_SEC = 8.0
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


class CarouselRunLlmFields(BaseModel):
    """Immutable LLM choice carried by every stage in one studio run."""

    llm_provider: str | None = Field(default=None, max_length=32)
    llm_model: str | None = Field(default=None, max_length=160)


class PipelineThemesRequest(CarouselRunLlmFields):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    search_entity: str = Field(default="", max_length=200)
    # When set: presence-check only — never reframe/harmonize themes around the person.
    person_name: str = Field(default="", max_length=200)
    # When False (default), return a matching saved themes row if cache key matches.
    force: bool = False
    # Explicit cold generate when cache misses. Never implied by Continue/Load.
    generate: bool = False


SAVE_KIND_TOPICS = "topics_hooks"
SAVE_KIND_THEMES = "themes"
SAVE_KIND_CAROUSEL = "carousel"
CAROUSEL_ALGORITHM_VERSION = "p0-fast-grouped-v3-quality-diversity-p1-crafted-copy-mu-verbs"
CAROUSEL_STATUS_PROCESSING = "processing"
CAROUSEL_STATUS_IDLE = "idle"


async def _steal_stale_carousel_lock(session: AsyncSession, drive_file_id: str) -> bool:
    """Clear an orphaned processing lock so studio retries are not stuck for 15 minutes.

    A 502 / killed worker leaves ``carousel_status=processing``. Theme generation
    must not wait on that; slide generate/select-images may steal a lock with no
    timestamp or one older than the stale window.
    """
    from app.workers.indexer import CAROUSEL_LOCK_STALE_SEC

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=CAROUSEL_LOCK_STALE_SEC)
    result = await session.execute(
        update(DriveFile)
        .where(
            DriveFile.id == drive_file_id,
            DriveFile.carousel_status == CAROUSEL_STATUS_PROCESSING,
            or_(
                DriveFile.carousel_locked_at.is_(None),
                DriveFile.carousel_locked_at < cutoff,
            ),
        )
        .values(
            carousel_status=CAROUSEL_STATUS_IDLE,
            carousel_lock_token=None,
            carousel_lock_input_hash=None,
            carousel_locked_at=None,
        )
    )
    if result.rowcount:
        await session.commit()
        logger.info("Stole stale carousel lock drive=%s", drive_file_id)
        return True
    return False


async def _claim_carousel(
    session: AsyncSession, drive_file_id: str, input_hash: str | None = None
) -> str:
    """Atomically claim one video's carousel pipeline; prevents duplicate jobs."""
    drive_file_id = drive_file_id.strip()
    token = uuid.uuid4().hex

    async def _try_claim() -> int:
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
        return int(result.rowcount or 0)

    if not await _try_claim():
        await _steal_stale_carousel_lock(session, drive_file_id)
        if not await _try_claim():
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
    # A timeout that cancelled a query mid-flight leaves the transaction
    # invalid; without this rollback the release fails and the video stays
    # locked for the full stale window (15 min of 409s).
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        pass
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
    selection_hash: str | None = None,
) -> CarouselGenerationSave | None:
    """Persist a ready, deterministic artifact for the cache-first endpoint."""
    safe_payload = _jsonb_safe(payload)
    # Prefer request-side selection hash so cache hits work before regenerating.
    input_hash = (selection_hash or "").strip() or carousel_input_hash(drive_file_id, safe_payload)
    existing = await session.scalar(
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == drive_file_id,
            CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
            or_(
                CarouselGenerationSave.input_hash == input_hash,
                CarouselGenerationSave.theme_key == input_hash,
            ),
        )
        .order_by(CarouselGenerationSave.created_at.desc())
    )
    if existing is not None:
        return existing
    save = CarouselGenerationSave(
        drive_file_id=drive_file_id,
        kind=SAVE_KIND_CAROUSEL,
        theme_key=input_hash[:256],
        label=str(payload.get("title") or "Carousel")[:240],
        status="ready",
        input_hash=input_hash,
        layout_mode=layout_mode if layout_mode in {"single_1", "split_2"} else "single_1",
        copy_version=1,
        algorithm_version=(CAROUSEL_ALGORITHM_VERSION or "p0")[:64],
        source=(source or "")[:32] or None,
        payload=safe_payload,
    )
    session.add(save)
    await session.commit()
    await session.refresh(save)
    return save


def _themes_transcript_hash(cues: list[Any]) -> str:
    """Stable cache key for theme generation input (transcript content)."""
    digest = hashlib.sha256()
    for cue in cues:
        try:
            start, end, text = cue
        except (TypeError, ValueError):
            continue
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            continue
        end_value = "" if end is None else f"{float(end):.3f}"
        line = f"{float(start):.3f}\t{end_value}\t{cleaned}\n"
        digest.update(line.encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def themes_cache_model_name(llm_pack: dict[str, Any] | None = None) -> str:
    """Cache identity for themes (provider/model + prompt version)."""
    pack = llm_pack or resolve_carousel_llm()
    return f"{carousel_llm_cache_id(pack)}:{THEME_PROMPT_VERSION}"[:128]


def _themes_save_is_usable(row: CarouselGenerationSave) -> bool:
    if (getattr(row, "status", None) or "ready").strip() != "ready":
        return False
    if (row.source or "").strip() == "fallback":
        return False
    payload = row.payload or {}
    if (payload.get("source") or "").strip() == "fallback":
        return False
    themes = list(payload.get("themes") or [])
    return bool(themes)


async def find_ready_themes_save(
    session: AsyncSession,
    *,
    drive_file_id: str,
    transcript_hash: str,
    model_name: str,
    limit: int = 8,
) -> CarouselGenerationSave | None:
    cached_q = (
        select(CarouselGenerationSave)
        .where(
            CarouselGenerationSave.drive_file_id == drive_file_id,
            CarouselGenerationSave.kind == SAVE_KIND_THEMES,
            CarouselGenerationSave.transcript_hash == transcript_hash,
            CarouselGenerationSave.status == "ready",
        )
        .order_by(CarouselGenerationSave.created_at.desc())
        .limit(limit)
    )
    for row in list((await session.execute(cached_q)).scalars().all()):
        row_model = (row.model or "").strip()
        if not row_model or row_model != model_name:
            continue
        if _themes_save_is_usable(row):
            return row
    return None


async def find_inflight_themes_job(
    session: AsyncSession,
    *,
    drive_file_id: str,
    transcript_hash: str,
    model_name: str,
) -> CarouselGenerationSave | None:
    row = (
        await session.execute(
            select(CarouselGenerationSave)
            .where(
                CarouselGenerationSave.drive_file_id == drive_file_id,
                CarouselGenerationSave.kind == SAVE_KIND_THEMES,
                CarouselGenerationSave.transcript_hash == transcript_hash,
                CarouselGenerationSave.model == model_name,
                CarouselGenerationSave.status == "processing",
            )
            .order_by(CarouselGenerationSave.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def persist_themes_save(
    *,
    drive_file_id: str,
    themes: list[dict[str, Any]],
    source: str,
    cue_count: int,
    transcript_hash: str,
    model_name: str,
    person_name: str | None = None,
    warning: str | None = None,
    existing_save_id: int | None = None,
) -> CarouselGenerationSave | None:
    """Persist a ready themes artifact (studio cache + index-time precompute)."""
    if not themes or (source or "").strip() == "fallback":
        return None
    label = f"{len(themes)} themes · {source}"
    payload = _jsonb_safe(
        {
            "drive_file_id": drive_file_id,
            "themes": themes,
            "source": source,
            "cue_count": cue_count,
            "transcript_hash": transcript_hash,
            "model": model_name,
            "person_name": person_name,
            **({"warning": warning} if warning else {}),
        }
    )
    try:
        async with get_session_factory()() as save_session:
            save: CarouselGenerationSave | None = None
            if existing_save_id is not None:
                save = await save_session.get(CarouselGenerationSave, existing_save_id)
            if save is None:
                save = CarouselGenerationSave(
                    drive_file_id=drive_file_id,
                    kind=SAVE_KIND_THEMES,
                    theme_key="all",
                )
                save_session.add(save)
            save.label = _jsonb_safe(label)[:240]
            save.model = model_name
            save.transcript_hash = transcript_hash
            save.source = (source or "")[:32] or None
            save.status = "ready"
            save.payload = payload
            await save_session.commit()
            await save_session.refresh(save)
            return save
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel themes autosave failed: %s", exc, exc_info=True)
        return None


def _themes_response_from_save(
    *,
    row: CarouselGenerationSave,
    drive_file_id: str,
    drive_name: str,
    check_name: str | None,
    cues_len: int,
    transcript_hash: str,
    model_name: str,
    generated: bool = False,
) -> dict[str, Any]:
    payload = row.payload or {}
    themes = list(payload.get("themes") or [])
    out: dict[str, Any] = {
        "source": row.source or payload.get("source") or "saved",
        "drive_file_id": drive_file_id,
        "name": drive_name,
        "search_entity": check_name or None,
        "person_name": check_name or None,
        "person_found": True if check_name else None,
        "harmonized": False,
        "cue_count": payload.get("cue_count") or cues_len,
        "themes": themes,
        "cache_hit": not generated,
        "generated": generated,
        "save_id": row.id,
        "transcript_hash": transcript_hash,
        "model": row.model or model_name,
        "status": "ready",
        "job_id": row.id,
    }
    if payload.get("warning"):
        out["warning"] = payload.get("warning")
    return out


def _extract_theme_key(slices: list[PipelineThemeSlice]) -> str:
    """Stable cache key for extract across selected theme windows."""
    import json

    payload = [
        {
            "id": (s.theme_id or "").strip(),
            "title": " ".join((s.title or "").split()),
            "start": round(float(s.start_sec or 0), 2),
            "end": None if s.end_sec is None else round(float(s.end_sec), 2),
            "summary": " ".join((s.summary or "").split()),
        }
        for s in slices
    ]
    if not payload:
        return "all"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _carousel_selection_hash(
    *,
    drive_file_id: str,
    hooks: list[TimedPick],
    topics: list[TimedPick],
    themes: list[PipelineThemeSlice] | tuple[PipelineThemeSlice, ...] = (),
    intent: str = "",
    min_slides: int,
    max_slides: int,
    select_images: bool,
    polish_copy: bool = False,
    llm_cache_id: str = "",
) -> str:
    """Stable request-side hash so generate can serve cache without Gemini."""
    import json

    def _pick(p: TimedPick) -> dict[str, Any]:
        return {
            "text": " ".join((p.text or "").lower().split()),
            "start_sec": round(float(p.start_sec or 0), 2),
            "end_sec": None if p.end_sec is None else round(float(p.end_sec), 2),
            "id": (p.id or "").strip(),
        }

    raw = json.dumps(
        {
            "drive_file_id": drive_file_id.strip(),
            "hooks": [_pick(h) for h in hooks],
            "topics": [_pick(t) for t in topics],
            "themes": [
                {
                    "id": (t.theme_id or "").strip(),
                    "title": " ".join((t.title or "").split()),
                    "start": round(float(t.start_sec or 0), 2),
                    "end": None if t.end_sec is None else round(float(t.end_sec), 2),
                }
                for t in themes
            ],
            "intent": " ".join((intent or "").lower().split()),
            "min_slides": int(min_slides),
            "max_slides": int(max_slides),
            "select_images": bool(select_images),
            "polish_copy": bool(polish_copy),
            "llm": llm_cache_id,
            "algo": CAROUSEL_ALGORITHM_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


class PipelineThemeSlice(BaseModel):
    theme_id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    start_sec: float = 0
    end_sec: float | None = None
    summary: str = Field(default="", max_length=800)


class PipelineExtractRequest(CarouselRunLlmFields):
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
    # Cache-first: force regenerates; generate creates on miss; default is cache-only.
    force: bool = False
    generate: bool = False
    # Studio default: topics only. Hooks are generated after the user picks topics.
    include_hooks: bool = False


class PipelineIntentRequest(CarouselRunLlmFields):
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


class PipelineTopicPick(BaseModel):
    """A topic the user selected before hook generation."""

    id: str = Field(default="", max_length=64)
    text: str = Field(..., min_length=1, max_length=400)
    start_sec: float = 0
    end_sec: float | None = None
    summary: str = Field(default="", max_length=800)
    explanation: str = Field(default="", max_length=800)
    theme_id: str | None = Field(default=None, max_length=64)
    time_ranges: list[TimedRange] = Field(default_factory=list, max_length=12)


class PipelineExtractHooksRequest(CarouselRunLlmFields):
    """Generate 2–4 hooks for user-selected topics."""

    drive_file_id: str = Field(..., min_length=1, max_length=128)
    themes: list[PipelineThemeSlice] = Field(default_factory=list, max_length=12)
    topics: list[PipelineTopicPick] = Field(..., min_length=1, max_length=12)
    search_entity: str = Field(default="", max_length=200)
    min_hooks: int = Field(default=2, ge=2, le=4)
    max_hooks: int = Field(default=4, ge=2, le=4)
    force: bool = False
    generate: bool = True


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


class CarouselGenerateRequest(CarouselRunLlmFields):
    """Generate one carousel for a single hook (≥6 one-line exact-transcript slides).

    Product model: one hook per request/job. Batching multiple hooks confuses cache.
    """

    drive_file_id: str = Field(..., min_length=1, max_length=128)
    video_name: str = Field(default="", max_length=400)
    intent: str = Field(default="", max_length=800)
    themes: list[PipelineThemeSlice] = Field(default_factory=list, max_length=12)
    hooks: list[TimedPick] = Field(default_factory=list, max_length=1)
    topics: list[TimedPick] = Field(default_factory=list, max_length=1)
    # Per-hook Instagram one-liners: at least 6 slides when cues allow.
    min_slides: int = Field(default=6, ge=2, le=12)
    max_slides: int = Field(default=10, ge=2, le=12)
    # Transcript-first: defer Gemini/frame selection until the user explicitly asks.
    select_images: bool = False
    # Optional production copy finalization + yellow keyword highlight indices.
    polish_copy: bool = False
    # Cache-first: force regenerates; generate creates on miss; default is cache-only.
    force: bool = False
    generate: bool = False


class CarouselPrerunRequest(CarouselRunLlmFields):
    """Warm theme + extract caches for indexed videos (studio pre-run)."""

    drive_file_ids: list[str] = Field(default_factory=list, max_length=40)
    # When True, regenerate themes/extract even if cached.
    force: bool = False


class CarouselUploadIndexResponse(BaseModel):
    drive_file_id: str
    name: str
    status: str
    message: str
    queued: bool = True


class CarouselSelectImagesBody(CarouselRunLlmFields):
    """Run quality + Gemini frame selection on already-edited carousel slides."""

    drive_file_id: str = Field(..., min_length=1, max_length=128)
    carousels: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    force: bool = False


class CarouselQualityCheckBody(BaseModel):
    """Deterministic quality rescore for edited studio slides. No LLM calls."""

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


class CarouselSlideRegenerateBody(CarouselRunLlmFields):
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


_UPLOAD_VIDEO_MIMES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "video/mpeg",
}
_UPLOAD_EXT_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


async def _index_uploaded_video(drive_file_id: str) -> None:
    """Background: index an uploaded video at max priority (carousel /test fast path)."""
    from app.dependencies import get_indexing_worker

    worker = get_indexing_worker()
    try:
        started = await worker.prioritize_video_index(drive_file_id)
        if not started:
            await worker.ensure_parallel_video_indexing()
        summary = await worker.process_pending(limit=4)
        logger.info(
            "Upload index prioritized id=%s started=%s summary=%s",
            drive_file_id,
            started,
            summary,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Upload index failed for %s", drive_file_id)


@router.post("/upload")
async def upload_video_for_index(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Accept a local video upload, store on the video cache volume, queue indexing.

    Thin carousel-studio entry point — reuses the same DriveFile + indexer pipeline
    as Drive/YouTube sources (source=upload). Does not wipe existing data.
    """
    import os
    from pathlib import Path

    from app.storage import RetryableDiskSpaceError, ensure_disk_space
    from app.video.youtube_cache import video_cache_path

    raw_name = (file.filename or "upload.mp4").strip() or "upload.mp4"
    # Harden filename (no path traversal).
    safe_name = Path(raw_name).name.replace("\x00", "")[:200] or "upload.mp4"
    ext = Path(safe_name).suffix.lower()
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _UPLOAD_VIDEO_MIMES:
        mime = _UPLOAD_EXT_MIME.get(ext, "")
    if mime not in _UPLOAD_VIDEO_MIMES and ext not in _UPLOAD_EXT_MIME:
        raise HTTPException(
            status_code=400,
            detail="Upload a video file (mp4, webm, mov, mkv, avi).",
        )
    if not ext:
        ext = next((e for e, m in _UPLOAD_EXT_MIME.items() if m == mime), ".mp4")
        safe_name = f"{safe_name}{ext}"

    file_id = f"upload:{uuid.uuid4().hex}"
    drive_file = DriveFile(
        id=file_id,
        name=safe_name,
        mime_type=mime or "video/mp4",
        path=f"uploads/{safe_name}",
        status=DriveFileStatus.PENDING,
        source="upload",
        modified_time=datetime.now(timezone.utc),
        last_synced_at=datetime.now(timezone.utc),
    )
    dest = video_cache_path(get_settings(), drive_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")

    written = 0
    try:
        ensure_disk_space(dest)
        with open(partial, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Video exceeds 2 GiB upload limit")
                ensure_disk_space(partial, len(chunk))
                out.write(chunk)
        if written <= 0:
            raise HTTPException(status_code=400, detail="Empty upload")
        os.replace(partial, dest)
    except HTTPException:
        if partial.is_file():
            partial.unlink(missing_ok=True)
        raise
    except RetryableDiskSpaceError as exc:
        if partial.is_file():
            partial.unlink(missing_ok=True)
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if partial.is_file():
            partial.unlink(missing_ok=True)
        logger.exception("carousel upload write failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        await file.close()

    drive_file.size = written
    session.add(drive_file)
    await session.commit()
    background_tasks.add_task(_index_uploaded_video, file_id)
    logger.info("carousel upload queued id=%s name=%s bytes=%d", file_id, safe_name, written)
    return {
        "drive_file_id": file_id,
        "name": safe_name,
        "status": DriveFileStatus.PENDING.value,
        "size": written,
        "queued": True,
        "message": "Upload saved — indexing queued (captions via Whisper when enabled).",
    }


class CarouselPrioritizeRequest(BaseModel):
    drive_file_ids: list[str] = Field(default_factory=list, max_length=40)


@router.post("/prioritize")
async def prioritize_drive_videos_for_carousel(
    body: CarouselPrioritizeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-queue + max-priority index selected Drive (or library) videos for carousel use.

    Same fast path as local /upload — bypasses size-ordered backlog starvation.
    Already-processed library rows never require Drive OAuth. Queuing a Drive-source
    video that is not cached locally does require a connected Drive session.
    """
    from app.db.models import DriveUser
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    ids = [x.strip() for x in (body.drive_file_ids or []) if (x or "").strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="Provide at least one drive_file_id")

    drive_user = (
        await session.execute(select(DriveUser).limit(1))
    ).scalar_one_or_none()
    drive_connected = drive_user is not None
    settings = get_settings()

    items: list[dict[str, Any]] = []
    queued_ids: list[str] = []
    for fid in ids[:40]:
        drive_file = await session.get(DriveFile, fid)
        if drive_file is None:
            items.append({"drive_file_id": fid, "ok": False, "error": "not_found"})
            continue
        if not is_video_mime(drive_file.mime_type):
            items.append(
                {
                    "drive_file_id": fid,
                    "ok": False,
                    "name": drive_file.name,
                    "error": "not_a_video",
                }
            )
            continue
        status_val = (
            drive_file.status.value
            if hasattr(drive_file.status, "value")
            else str(drive_file.status)
        )
        if drive_file.status == DriveFileStatus.PROCESSED:
            _, cues = await _load_video_cues(session, fid)
            cue_n = len(cues)
            items.append(
                {
                    "drive_file_id": fid,
                    "ok": True,
                    "name": drive_file.name,
                    "status": status_val,
                    "queued": False,
                    "has_captions": cue_n > 0,
                    "cue_count": cue_n,
                    "message": (
                        "Already indexed"
                        if cue_n > 0
                        else "Indexed but no transcript cues (themes need captions)"
                    ),
                }
            )
            continue

        source = (getattr(drive_file, "source", None) or "drive").lower()
        needs_live_drive = source == "drive" and not is_youtube_source(drive_file)
        if needs_live_drive and not drive_connected:
            cached = video_cache_path(settings, drive_file).is_file()
            if not cached:
                items.append(
                    {
                        "drive_file_id": fid,
                        "ok": False,
                        "name": drive_file.name,
                        "status": status_val,
                        "queued": False,
                        "error": "drive_not_connected",
                        "message": (
                            "Reconnect Google Drive to index this video "
                            "(not cached locally)."
                        ),
                    }
                )
                continue

        if drive_file.status in (DriveFileStatus.ERROR, DriveFileStatus.SKIPPED):
            drive_file.status = DriveFileStatus.PENDING
            drive_file.error_message = None
        elif drive_file.status not in (
            DriveFileStatus.PENDING,
            DriveFileStatus.PROCESSING,
        ):
            drive_file.status = DriveFileStatus.PENDING
            drive_file.error_message = None
        queued_ids.append(fid)
        items.append(
            {
                "drive_file_id": fid,
                "ok": True,
                "name": drive_file.name,
                "status": DriveFileStatus.PENDING.value,
                "queued": True,
                "message": "Queued for priority indexing",
            }
        )

    await session.commit()

    for fid in queued_ids:
        background_tasks.add_task(_index_uploaded_video, fid)

    failed = sum(1 for it in items if not it.get("ok"))
    return {
        "ok": failed == 0,
        "queued": len(queued_ids),
        "items": items,
        "message": (
            f"Priority indexing queued for {len(queued_ids)} video(s)."
            if queued_ids
            else (
                "Reconnect Google Drive to index videos that are not already processed."
                if failed
                else "No videos needed indexing."
            )
        ),
    }


class EnsureTranscriptRequest(BaseModel):
    drive_file_id: str = Field(min_length=1, max_length=256)
    force: bool = False


@router.get("/videos/{drive_file_id}/transcript-status")
async def carousel_transcript_status(
    drive_file_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll Whisper / caption backfill status for a library video."""
    from app.video.whisper_backfill import transcript_status_payload

    return await transcript_status_payload(session, drive_file_id.strip())


@router.post("/videos/ensure-transcript")
async def carousel_ensure_transcript(
    body: EnsureTranscriptRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Start on-demand Whisper transcription when cues are missing.

    Returns immediately with status=running while a background task extracts
    speech. Poll ``GET /videos/{id}/transcript-status`` until ready/failed.
    """
    from app.video.whisper_backfill import (
        claim_transcript_job,
        ensure_whisper_transcript,
        transcript_status_payload,
    )

    fid = body.drive_file_id.strip()
    if not fid:
        raise HTTPException(status_code=400, detail="drive_file_id is required")

    status = await transcript_status_payload(session, fid)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Drive file not found")
    if status.get("status") == "ready" and not body.force:
        return status

    claim = await claim_transcript_job(session, fid, force=bool(body.force))
    if claim == "missing_file":
        raise HTTPException(status_code=404, detail="Drive file not found")
    if claim == "ready":
        return await transcript_status_payload(session, fid)
    if claim == "claimed":
        background_tasks.add_task(
            ensure_whisper_transcript, fid, force=bool(body.force)
        )

    out = await transcript_status_payload(session, fid)
    if out.get("status") == "missing":
        out["status"] = "running"
        out["message"] = "Getting transcripts from the video…"
        out["phase"] = "starting"
    return out


@router.post("/pipeline/prerun")
async def carousel_pipeline_prerun(
    body: CarouselPrerunRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Warm theme + extract caches for selected/all indexed captioned videos.

    For each video: generate themes if missing (or force), then extract hooks/topics
    across all themes. Studio clicks later hit cache only.
    """
    ids = [x.strip() for x in (body.drive_file_ids or []) if (x or "").strip()]
    if not ids:
        # Default: recent captioned videos.
        recent = await carousel_recent_videos(limit=8, captioned_only=True, session=session)
        ids = [item["id"] for item in recent.get("items") or []]
    ids = ids[:20]
    results: list[dict[str, Any]] = []
    for drive_file_id in ids:
        item: dict[str, Any] = {"drive_file_id": drive_file_id, "ok": False}
        try:
            themes_body = PipelineThemesRequest(
                drive_file_id=drive_file_id,
                force=bool(body.force),
                generate=True,
                llm_provider=body.llm_provider,
                llm_model=body.llm_model,
            )
            themes_res = await carousel_pipeline_themes(themes_body, session)
            themes = list(themes_res.get("themes") or [])
            item["themes_cache_hit"] = bool(themes_res.get("cache_hit"))
            item["themes_generated"] = bool(themes_res.get("generated"))
            item["theme_count"] = len(themes)
            if not themes:
                item["ok"] = False
                item["error"] = themes_res.get("message") or themes_res.get("warning") or "no themes"
                results.append(item)
                continue
            extract_body = PipelineExtractRequest(
                drive_file_id=drive_file_id,
                themes=[
                    PipelineThemeSlice(
                        theme_id=t.get("theme_id") or "",
                        title=t.get("title") or "",
                        start_sec=float(t.get("start_sec") or 0),
                        end_sec=t.get("end_sec"),
                        summary=t.get("summary") or "",
                    )
                    for t in themes
                    if isinstance(t, dict)
                ],
                force=bool(body.force),
                generate=True,
                llm_provider=body.llm_provider,
                llm_model=body.llm_model,
            )
            extract_res = await carousel_pipeline_extract(extract_body, request, session)
            item["extract_cache_hit"] = bool(extract_res.get("cache_hit"))
            item["extract_generated"] = bool(extract_res.get("generated"))
            item["hook_count"] = len(extract_res.get("hooks") or [])
            item["topic_count"] = len(extract_res.get("topics") or [])
            item["ok"] = True
        except HTTPException as exc:
            item["error"] = str(exc.detail)
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)[:240]
            logger.warning("prerun failed for %s: %s", drive_file_id, exc)
        results.append(item)
    return {
        "count": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "force": bool(body.force),
        "items": results,
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

    Themes are generated at most once per (video, transcript_hash, carousel LLM
    cache id) unless ``force=true``. Matching saves are returned immediately.
    Without ``force`` or ``generate``, a cache miss returns empty (no LLM).

    When person_name is set, only verify that person appears in the video (face match).
    If absent, return person_not_found — never reframe/harmonize themes around the person.
    """
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    started = time.perf_counter()
    set_drive_file_id(body.drive_file_id)
    carousel_log(
        "themes_request_start",
        force=bool(body.force),
        generate=bool(body.generate),
        llm_provider=(body.llm_provider or "-"),
        llm_model=(body.llm_model or "-"),
        person=(body.person_name or body.search_entity or "-"),
    )
    # Prefer explicit person_name; search_entity alone is treated as person only when it
    # matches a known Person row used for presence check below.
    explicit_person = (body.person_name or "").strip()
    drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())
    set_drive_file_id(drive_file.id)
    carousel_log(
        "themes_cues_loaded",
        cue_count=len(cues),
        video_name=drive_file.name,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
    # Themes write SAVE_KIND_THEMES only. Do not wait on the slide-generate
    # lock — a 502'd generate/select-images or background carousel job used to
    # 409 this route for up to 15 minutes even on a cache hit.

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
            "warning": "This video doesn’t have a transcript yet. Wait until indexing finishes, then try again.",
        }

    transcript_hash = _themes_transcript_hash(cues)
    llm_pack = resolve_carousel_llm(body.llm_provider, body.llm_model)
    # Cache identity must track Claude/OpenRouter config — never gemini_model alone,
    # or Gemini-era theme saves would incorrectly satisfy Claude requests.
    model_name = themes_cache_model_name(llm_pack)

    if not body.force:
        cached = await find_ready_themes_save(
            session,
            drive_file_id=drive_file.id,
            transcript_hash=transcript_hash,
            model_name=model_name,
        )
        if cached is not None:
            logger.info(
                "carousel themes cache hit drive=%s save_id=%s hash=%s model=%s",
                drive_file.id,
                cached.id,
                transcript_hash[:12],
                model_name,
            )
            carousel_log(
                "themes_cache_hit",
                save_id=cached.id,
                theme_count=len((cached.payload or {}).get("themes") or []),
                model=model_name,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            return _themes_response_from_save(
                row=cached,
                drive_file_id=drive_file.id,
                drive_name=drive_file.name,
                check_name=check_name or None,
                cues_len=len(cues),
                transcript_hash=transcript_hash,
                model_name=model_name,
            )

    # Strict cache-first: Continue/Load never auto-calls Gemini on miss.
    if not body.force and not body.generate:
        logger.info(
            "carousel themes cache miss (no generate) drive=%s hash=%s",
            drive_file.id,
            transcript_hash[:12],
        )
        carousel_log(
            "themes_cache_miss_no_generate",
            model=model_name,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {
            "source": "cache_miss",
            "drive_file_id": drive_file.id,
            "name": drive_file.name,
            "search_entity": check_name or None,
            "person_name": check_name or None,
            "person_found": True if check_name else None,
            "harmonized": False,
            "cue_count": len(cues),
            "themes": [],
            "cache_hit": False,
            "generated": False,
            "transcript_hash": transcript_hash,
            "model": model_name,
            "status": "cache_miss",
            "message": "No cached themes for this transcript. Click Generate themes to create them.",
            "warning": "No cached themes for this transcript. Click Generate themes to create them.",
        }

    # Serialize only the same video/model generation. A single global lock made
    # an unrelated long transcript hold every studio request behind it.
    themes_lock_key = carousel_themes_lock_key(drive_file.id, model_name)
    carousel_log("themes_lock_wait")
    async with advisory_lock(
        themes_lock_key,
        name=f"carousel_themes:{drive_file.id}:{model_name}",
        blocking=True,
    ) as got:
            if not got:
                carousel_log(
                    "themes_lock_unavailable",
                    level=logging.WARNING,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Theme generation lock unavailable; retry shortly.",
                    headers={"Retry-After": "3"},
                )
            carousel_log(
                "themes_lock_acquired",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            # Re-check cache under the lock — a sibling request may have just saved.
            # Skip when force=True (explicit regenerate).
            if not body.force:
                cached = await find_ready_themes_save(
                    session,
                    drive_file_id=drive_file.id,
                    transcript_hash=transcript_hash,
                    model_name=model_name,
                    limit=4,
                )
                if cached is not None:
                    logger.info(
                        "carousel themes cache hit (post-lock) drive=%s save_id=%s model=%s",
                        drive_file.id,
                        cached.id,
                        model_name,
                    )
                    carousel_log(
                        "themes_cache_hit_post_lock",
                        save_id=cached.id,
                        theme_count=len((cached.payload or {}).get("themes") or []),
                        elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    )
                    return _themes_response_from_save(
                        row=cached,
                        drive_file_id=drive_file.id,
                        drive_name=drive_file.name,
                        check_name=check_name or None,
                        cues_len=len(cues),
                        transcript_hash=transcript_hash,
                        model_name=model_name,
                    )

            # Do not hold the request's read transaction/connection while an
            # external LLM runs. Long calls previously returned to a closed
            # asyncpg connection and lost the generated themes during autosave.
            # Snapshot ORM values first: rollback expires model attributes.
            generated_drive_id = str(drive_file.id)
            generated_drive_name = str(drive_file.name)
            del drive_file
            await session.rollback()
            llm = llm_pack
            carousel_log(
                "themes_generate_start",
                provider=llm["provider"],
                openrouter_model=llm.get("openrouter_model") or "-",
                claude_model=llm.get("claude_model") or "-",
                gemini_model=llm.get("model") or "-",
                cue_count=len(cues),
            )
            themes, source, warning = await build_harmonized_themes(
                cues=cues,
                video_name=generated_drive_name,
                search_entity=None,
                api_key=llm["api_key"],
                model=llm["model"],
                claude_api_key=llm["claude_api_key"],
                claude_model=llm["claude_model"],
                provider=llm["provider"],
                openrouter_api_key=llm["openrouter_api_key"],
                openrouter_model=llm["openrouter_model"],
                openrouter_base_url=llm["openrouter_base_url"],
            )
            carousel_log(
                "themes_generate_done",
                source=source,
                theme_count=len(themes),
                warning=warning or "-",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            if source == "fallback":
                carousel_log(
                    "themes_generate_rejected_fallback",
                    level=logging.ERROR,
                    warning=warning or "all configured LLM providers failed",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "The selected AI model could not generate valid themes. "
                        f"{warning or 'Please retry or choose another model.'}"
                    )[:500],
                )
            result: dict[str, Any] = {
                "source": source,
                "drive_file_id": generated_drive_id,
                "name": generated_drive_name,
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
                "status": "ready",
                **({"warning": warning} if warning else {}),
            }

            if themes:
                save = await persist_themes_save(
                    drive_file_id=generated_drive_id,
                    themes=themes,
                    source=source,
                    cue_count=len(cues),
                    transcript_hash=transcript_hash,
                    model_name=model_name,
                    person_name=check_name or None,
                    warning=warning,
                )
                if save is not None:
                    result["save_id"] = save.id
                    result["job_id"] = save.id
                    logger.info(
                        "carousel themes saved drive=%s save_id=%s generated=1 hash=%s",
                        generated_drive_id,
                        save.id,
                        transcript_hash[:12],
                    )

            return result


async def _run_themes_job(
    save_id: int,
    *,
    drive_file_id: str,
    drive_name: str,
    cues: list[Any],
    transcript_hash: str,
    model_name: str,
    llm_pack: dict[str, Any],
    check_name: str | None,
    force: bool,
) -> None:
    """Background worker for async theme generation."""
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    set_drive_file_id(drive_file_id)
    themes_lock_key = carousel_themes_lock_key(drive_file_id, model_name)
    try:
        async with advisory_lock(
            themes_lock_key,
            name=f"carousel_themes:{drive_file_id}:{model_name}",
            blocking=True,
        ) as got:
            if not got:
                async with get_session_factory()() as session:
                    row = await session.get(CarouselGenerationSave, save_id)
                    if row is not None:
                        row.status = "error"
                        row.payload = _jsonb_safe(
                            {
                                **(row.payload or {}),
                                "error": "Theme generation lock unavailable; retry shortly.",
                                "phase": "failed",
                            }
                        )
                        await session.commit()
                return

            if not force:
                async with get_session_factory()() as session:
                    cached = await find_ready_themes_save(
                        session,
                        drive_file_id=drive_file_id,
                        transcript_hash=transcript_hash,
                        model_name=model_name,
                        limit=4,
                    )
                    if cached is not None:
                        # Collapse the placeholder job onto the ready cache row.
                        placeholder = await session.get(CarouselGenerationSave, save_id)
                        if placeholder is not None and placeholder.id != cached.id:
                            placeholder.status = "ready"
                            placeholder.source = cached.source
                            placeholder.payload = cached.payload
                            placeholder.label = cached.label
                            await session.commit()
                        return

            async with get_session_factory()() as session:
                row = await session.get(CarouselGenerationSave, save_id)
                if row is not None:
                    payload = dict(row.payload or {})
                    payload["phase"] = "generating"
                    row.payload = _jsonb_safe(payload)
                    await session.commit()

            carousel_log(
                "themes_job_generate_start",
                job_id=save_id,
                provider=llm_pack.get("provider") or "-",
                cue_count=len(cues),
            )
            themes, source, warning = await build_harmonized_themes(
                cues=cues,
                video_name=drive_name,
                search_entity=None,
                api_key=llm_pack["api_key"],
                model=llm_pack["model"],
                claude_api_key=llm_pack["claude_api_key"],
                claude_model=llm_pack["claude_model"],
                provider=llm_pack["provider"],
                openrouter_api_key=llm_pack["openrouter_api_key"],
                openrouter_model=llm_pack["openrouter_model"],
                openrouter_base_url=llm_pack["openrouter_base_url"],
            )
            if source == "fallback" or not themes:
                async with get_session_factory()() as session:
                    row = await session.get(CarouselGenerationSave, save_id)
                    if row is not None:
                        row.status = "error"
                        row.source = "fallback"
                        row.payload = _jsonb_safe(
                            {
                                "drive_file_id": drive_file_id,
                                "themes": [],
                                "source": "fallback",
                                "error": warning
                                or "The selected AI model could not generate valid themes.",
                                "phase": "failed",
                                "transcript_hash": transcript_hash,
                                "model": model_name,
                            }
                        )
                        await session.commit()
                carousel_log(
                    "themes_job_failed",
                    level=logging.ERROR,
                    job_id=save_id,
                    warning=warning or "fallback",
                )
                return

            await persist_themes_save(
                drive_file_id=drive_file_id,
                themes=themes,
                source=source,
                cue_count=len(cues),
                transcript_hash=transcript_hash,
                model_name=model_name,
                person_name=check_name,
                warning=warning,
                existing_save_id=save_id,
            )
            carousel_log(
                "themes_job_ready",
                job_id=save_id,
                theme_count=len(themes),
                source=source,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("async themes job failed save_id=%s", save_id)
        try:
            async with get_session_factory()() as session:
                row = await session.get(CarouselGenerationSave, save_id)
                if row is not None:
                    row.status = "error"
                    row.payload = _jsonb_safe(
                        {
                            **(row.payload or {}),
                            "error": str(exc)[:500],
                            "phase": "failed",
                        }
                    )
                    await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to mark themes job error save_id=%s", save_id)


@router.post("/pipeline/themes/jobs")
async def carousel_pipeline_themes_start_job(
    body: PipelineThemesRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Start theme generation asynchronously; poll ``GET .../themes/jobs/{id}``.

    Cache hits and cache-only misses return immediately (status ready / cache_miss).
    Generate/force returns ``status=running`` with ``job_id`` while the LLM works.
    """
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    started = time.perf_counter()
    set_drive_file_id(body.drive_file_id)
    carousel_log(
        "themes_job_request_start",
        force=bool(body.force),
        generate=bool(body.generate),
        llm_provider=(body.llm_provider or "-"),
        llm_model=(body.llm_model or "-"),
    )
    drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())
    set_drive_file_id(drive_file.id)

    explicit_person = (body.person_name or "").strip()
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
                "status": "error",
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
            }

    if not cues:
        return {
            "source": "empty",
            "status": "error",
            "drive_file_id": drive_file.id,
            "name": drive_file.name,
            "themes": [],
            "cache_hit": False,
            "generated": False,
            "message": "This video doesn’t have a transcript yet. Wait until indexing finishes, then try again.",
        }

    transcript_hash = _themes_transcript_hash(cues)
    llm_pack = resolve_carousel_llm(body.llm_provider, body.llm_model)
    model_name = themes_cache_model_name(llm_pack)

    if not body.force:
        cached = await find_ready_themes_save(
            session,
            drive_file_id=drive_file.id,
            transcript_hash=transcript_hash,
            model_name=model_name,
        )
        if cached is not None:
            carousel_log(
                "themes_job_cache_hit",
                save_id=cached.id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
            return _themes_response_from_save(
                row=cached,
                drive_file_id=drive_file.id,
                drive_name=drive_file.name,
                check_name=check_name or None,
                cues_len=len(cues),
                transcript_hash=transcript_hash,
                model_name=model_name,
            )

    if not body.force and not body.generate:
        return {
            "source": "cache_miss",
            "status": "cache_miss",
            "drive_file_id": drive_file.id,
            "name": drive_file.name,
            "themes": [],
            "cache_hit": False,
            "generated": False,
            "transcript_hash": transcript_hash,
            "model": model_name,
            "message": "No cached themes for this transcript. Click Generate themes to create them.",
        }

    if not body.force:
        inflight = await find_inflight_themes_job(
            session,
            drive_file_id=drive_file.id,
            transcript_hash=transcript_hash,
            model_name=model_name,
        )
        if inflight is not None:
            return {
                "source": "job",
                "status": "running",
                "job_id": inflight.id,
                "save_id": inflight.id,
                "drive_file_id": drive_file.id,
                "name": drive_file.name,
                "themes": [],
                "cache_hit": False,
                "generated": False,
                "transcript_hash": transcript_hash,
                "model": model_name,
                "message": "Theme generation already in progress…",
                "phase": (inflight.payload or {}).get("phase") or "generating",
            }

    placeholder = CarouselGenerationSave(
        drive_file_id=drive_file.id,
        kind=SAVE_KIND_THEMES,
        theme_key="all",
        label="Generating themes…",
        model=model_name,
        transcript_hash=transcript_hash,
        source="job",
        status="processing",
        payload=_jsonb_safe(
            {
                "drive_file_id": drive_file.id,
                "themes": [],
                "phase": "queued",
                "transcript_hash": transcript_hash,
                "model": model_name,
                "person_name": check_name or None,
            }
        ),
    )
    session.add(placeholder)
    await session.commit()
    await session.refresh(placeholder)

    # Snapshot values for the background task; do not reuse this request session.
    drive_id = str(drive_file.id)
    drive_name = str(drive_file.name)
    cues_copy = list(cues)
    job_id = int(placeholder.id)
    background_tasks.add_task(
        _run_themes_job,
        job_id,
        drive_file_id=drive_id,
        drive_name=drive_name,
        cues=cues_copy,
        transcript_hash=transcript_hash,
        model_name=model_name,
        llm_pack=llm_pack,
        check_name=check_name or None,
        force=bool(body.force),
    )
    carousel_log(
        "themes_job_queued",
        job_id=job_id,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
    return {
        "source": "job",
        "status": "running",
        "job_id": job_id,
        "save_id": job_id,
        "drive_file_id": drive_id,
        "name": drive_name,
        "themes": [],
        "cache_hit": False,
        "generated": False,
        "transcript_hash": transcript_hash,
        "model": model_name,
        "message": "Generating themes…",
        "phase": "queued",
    }


@router.get("/pipeline/themes/jobs/{job_id}")
async def carousel_pipeline_themes_job_status(
    job_id: int,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll an async themes job started by ``POST /pipeline/themes/jobs``."""
    row = await session.get(CarouselGenerationSave, job_id)
    if row is None or row.kind != SAVE_KIND_THEMES:
        raise HTTPException(status_code=404, detail="Themes job not found")
    payload = row.payload or {}
    status = (row.status or "").strip() or "ready"
    if status == "processing":
        return {
            "source": "job",
            "status": "running",
            "job_id": row.id,
            "save_id": row.id,
            "drive_file_id": row.drive_file_id,
            "themes": [],
            "cache_hit": False,
            "generated": False,
            "transcript_hash": row.transcript_hash,
            "model": row.model,
            "phase": payload.get("phase") or "generating",
            "message": "Generating themes…",
        }
    if status == "error":
        return {
            "source": row.source or "error",
            "status": "error",
            "job_id": row.id,
            "save_id": row.id,
            "drive_file_id": row.drive_file_id,
            "themes": [],
            "cache_hit": False,
            "generated": False,
            "transcript_hash": row.transcript_hash,
            "model": row.model,
            "error": payload.get("error") or "Theme generation failed",
            "message": payload.get("error") or "Theme generation failed",
            "phase": "failed",
        }
    themes = list(payload.get("themes") or [])
    return {
        "source": row.source or payload.get("source") or "saved",
        "status": "ready",
        "job_id": row.id,
        "save_id": row.id,
        "drive_file_id": row.drive_file_id,
        "themes": themes,
        "cache_hit": (row.source or "") != "job" and not payload.get("generated"),
        "generated": True,
        "cue_count": payload.get("cue_count"),
        "transcript_hash": row.transcript_hash,
        "model": row.model,
        "harmonized": False,
        **({"warning": payload.get("warning")} if payload.get("warning") else {}),
        "message": f"{len(themes)} themes ready",
        "phase": "ready",
    }


# NOTE: sync themes route is defined above; async job routes sit after it.


@router.post("/pipeline/extract")
async def carousel_pipeline_extract(
    body: PipelineExtractRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 3–4: contextual hooks + theme-generated topics + preview markers + intent.

    Hooks prefer English: parallel English caption track when available, else LLM translate.
    Accepts one theme (legacy fields) or multiple `themes` merged in time order.

    Cache-first: without ``force``/``generate``, return a matching save or an empty miss.
    Never silently call Gemini on an ambiguous Continue/Extract click.
    """
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    set_drive_file_id(body.drive_file_id)
    carousel_log(
        "extract_request_start",
        force=bool(body.force),
        generate=bool(body.generate),
        theme_count=len(body.themes or []),
        llm_provider=(body.llm_provider or "-"),
        llm_model=(body.llm_model or "-"),
    )
    # Cache-only reads skip the extract lock so UI polling stays snappy.
    if not body.force and not body.generate:
        return await _carousel_pipeline_extract_impl(body, session, request)

    # Serialize extracts so remount/retry storms cannot pin every default
    # thread-pool slot with concurrent Gemini jobs (process-local + cross-worker).
    if _EXTRACT_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="Hook/topic extract already running; wait for it to finish.",
            headers={"Retry-After": "5"},
        )

    async with _EXTRACT_LOCK:
        async with advisory_lock(
            LOCK_CAROUSEL_EXTRACT, name="carousel_extract", blocking=False
        ) as got:
            if not got:
                raise HTTPException(
                    status_code=409,
                    detail="Hook/topic extract already running on another worker; wait for it to finish.",
                    headers={"Retry-After": "5"},
                )
            if await request.is_disconnected():
                raise HTTPException(status_code=400, detail="Client disconnected before extract started")
            return await _carousel_pipeline_extract_impl(body, session, request)


@router.post("/pipeline/extract/hooks")
async def carousel_pipeline_extract_hooks(
    body: PipelineExtractHooksRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Phase 3b: craft 2–4 hooks after the user selected one or more topics."""
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    set_drive_file_id(body.drive_file_id)
    carousel_log(
        "extract_hooks_request_start",
        topic_count=len(body.topics or []),
        min_hooks=body.min_hooks,
        max_hooks=body.max_hooks,
        llm_provider=(body.llm_provider or "-"),
        llm_model=(body.llm_model or "-"),
    )
    if not body.topics:
        raise HTTPException(status_code=400, detail="Select at least one topic before generating hooks.")

    if _EXTRACT_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="Hook/topic extract already running; wait for it to finish.",
            headers={"Retry-After": "5"},
        )

    async with _EXTRACT_LOCK:
        async with advisory_lock(
            LOCK_CAROUSEL_EXTRACT, name="carousel_extract", blocking=False
        ) as got:
            if not got:
                raise HTTPException(
                    status_code=409,
                    detail="Hook/topic extract already running on another worker; wait for it to finish.",
                    headers={"Retry-After": "5"},
                )
            if await request.is_disconnected():
                raise HTTPException(status_code=400, detail="Client disconnected before hook craft started")

            drive_file, cues = await _load_video_cues(session, body.drive_file_id.strip())
            english_cues = await _maybe_load_english_cues(drive_file, cues)
            slices = list(body.themes or [])
            theme_titles = [s.title for s in slices if (s.title or "").strip()]
            combined_title = (
                " → ".join(theme_titles[:4]) if theme_titles else "Selected topics"
            )
            combined_summary = " ".join(
                (s.summary or "").strip() for s in slices if (s.summary or "").strip()
            )[:800]
            llm = resolve_carousel_llm(body.llm_provider, body.llm_model)
            selected = [
                {
                    "id": t.id or f"topic_{i + 1}",
                    "text": t.text,
                    "start_sec": float(t.start_sec or 0),
                    "end_sec": t.end_sec,
                    "explanation": t.explanation or t.summary or "",
                    "theme_id": t.theme_id,
                    "time_ranges": [
                        {"start_sec": r.start_sec, "end_sec": r.end_sec}
                        for r in (t.time_ranges or [])
                    ],
                }
                for i, t in enumerate(body.topics)
            ]
            try:
                crafted = await asyncio.wait_for(
                    craft_hooks_for_selected_topics_async(
                        cues,
                        selected_topics=selected,
                        theme_title=combined_title,
                        theme_summary=combined_summary,
                        min_hooks=int(body.min_hooks or 2),
                        max_hooks=int(body.max_hooks or 4),
                        api_key=llm["api_key"],
                        model=llm["model"],
                        claude_api_key=llm["claude_api_key"],
                        claude_model=llm["claude_model"],
                        provider=llm["provider"],
                        openrouter_api_key=llm["openrouter_api_key"],
                        openrouter_model=llm["openrouter_model"],
                        openrouter_base_url=llm["openrouter_base_url"],
                        english_cues=english_cues,
                    ),
                    timeout=min(_EXTRACT_TIMEOUT_SEC, 180.0),
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=504,
                    detail="Hook generation timed out. Please retry.",
                ) from exc

            hooks = list(crafted.get("hooks") or [])
            if len(hooks) < 2:
                raise HTTPException(
                    status_code=502,
                    detail="Could not craft at least 2 hooks for the selected topics. Try different topics.",
                )
            topic_tree = list(crafted.get("topic_tree") or [])
            topics = list(crafted.get("topics") or selected)
            carousel_log(
                "extract_hooks_done",
                hook_count=len(hooks),
                topic_count=len(selected),
            )
            return {
                "drive_file_id": drive_file.id,
                "hooks": hooks,
                "topics": topics,
                "topic_tree": topic_tree,
                "previews": [],
                "verbatim": False,
                "hooks_contextual": True,
                "topics_generated": False,
                "hooks_english": True,
                "topics_english": True,
                "any_translated": False,
                "cache_hit": False,
                "generated": True,
                "min_hooks": int(body.min_hooks or 2),
                "max_hooks": int(body.max_hooks or 4),
                "message": f"{len(hooks)} hooks ready — pick the ones you want on slides.",
            }


def _sanitize_extract_hook_payload(
    hooks: list[dict[str, Any]],
    topic_tree: list[dict[str, Any]] | None = None,
    *,
    theme_title: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Keep crafted display hooks separate from their exact transcript evidence."""
    from app.search.carousel_pipeline import enforce_non_verbatim_hooks

    def _clean_list(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [dict(r) for r in rows if isinstance(r, dict)]
        corpus = [
            str(r.get("original_text") or r.get("text") or "")
            for r in candidates
        ]
        cleaned, _stats = enforce_non_verbatim_hooks(
            candidates,
            corpus,
            theme_title=theme_title,
        )
        for i, h in enumerate(cleaned):
            h["id"] = h.get("id") or f"hook_{i + 1}"
        return cleaned

    flat = _clean_list(list(hooks or []))
    if not topic_tree:
        return flat, topic_tree
    tree_out: list[dict[str, Any]] = []
    for node in topic_tree:
        item = dict(node)
        item["hooks"] = _clean_list(list(node.get("hooks") or []))
        subs = []
        for sub in node.get("subtopics") or []:
            s = dict(sub)
            s["hooks"] = _clean_list(list(sub.get("hooks") or []))
            subs.append(s)
        item["subtopics"] = subs
        tree_out.append(item)
    return flat, tree_out


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
    theme_key = _extract_theme_key(slices_sorted)
    llm_pack = resolve_carousel_llm(body.llm_provider, body.llm_model)
    include_hooks = bool(body.include_hooks)
    stage_tag = "full" if include_hooks else "topics-only"
    llm_cache_id = (
        f"{carousel_llm_cache_id(llm_pack)}:{EXTRACT_PROMPT_VERSION}:{stage_tag}"
    )[:128]

    # Cache lookup before any LLM work — only hit when theme windows AND LLM
    # config match (rejects Gemini-era / other-model precache).
    if not body.force:
        cached_q = (
            select(CarouselGenerationSave)
            .where(
                CarouselGenerationSave.drive_file_id == drive_file.id,
                CarouselGenerationSave.kind == SAVE_KIND_TOPICS,
                CarouselGenerationSave.theme_key == theme_key,
            )
            .order_by(CarouselGenerationSave.created_at.desc())
            .limit(6)
        )
        for row in list((await session.execute(cached_q)).scalars().all()):
            payload = dict(row.payload or {})
            row_model = (row.model or "").strip() or str(payload.get("llm_cache_id") or "").strip()
            if row_model != llm_cache_id:
                continue
            hooks = list(payload.get("hooks") or [])
            topics = list(payload.get("topics") or [])
            topic_tree = list(payload.get("topic_tree") or [])
            if not include_hooks:
                # Topics-only stage: require topics; drop any cached hooks.
                if not topics and not topic_tree:
                    continue
                hooks = []
                for node in topic_tree:
                    node["hooks"] = []
                    for sub in list(node.get("subtopics") or []):
                        sub["hooks"] = []
            elif not hooks and not topics and not topic_tree:
                continue
            logger.info(
                "carousel extract cache hit drive=%s save_id=%s theme_key=%s model=%s",
                drive_file.id,
                row.id,
                theme_key[:48],
                llm_cache_id,
            )
            theme_title = ""
            if slices:
                theme_title = str(getattr(slices[0], "title", "") or "")
            hooks, topic_tree = _sanitize_extract_hook_payload(
                hooks, topic_tree, theme_title=theme_title
            )
            return {
                "drive_file_id": drive_file.id,
                "theme_id": slices[0].theme_id if len(slices) == 1 else None,
                "theme_ids": [s.theme_id for s in slices if s.theme_id],
                "hooks": hooks,
                "topics": topics,
                "topic_tree": topic_tree,
                "previews": list(payload.get("previews") or [])[:40],
                "intent": payload.get("intent"),
                "intent_score": payload.get("intent_score"),
                "intent_source": payload.get("intent_source") or "saved",
                "verbatim": bool(payload.get("verbatim", False)),
                "hooks_contextual": True,
                "topics_generated": True,
                "hooks_english": payload.get("hooks_english", True),
                "topics_english": payload.get("topics_english", True),
                "any_translated": bool(payload.get("any_translated")),
                "english_source": payload.get("english_source"),
                "transcript_meta": payload.get("transcript_meta"),
                "save_id": row.id,
                "cache_hit": True,
                "generated": False,
                "model": llm_cache_id,
            }

    if not body.force and not body.generate:
        logger.info(
            "carousel extract cache miss (no generate) drive=%s theme_key=%s",
            drive_file.id,
            theme_key[:48],
        )
        return {
            "drive_file_id": drive_file.id,
            "theme_id": slices[0].theme_id if len(slices) == 1 else None,
            "theme_ids": [s.theme_id for s in slices if s.theme_id],
            "hooks": [],
            "topics": [],
            "topic_tree": [],
            "previews": [],
            "intent": None,
            "intent_score": None,
            "intent_source": None,
            "verbatim": False,
            "hooks_contextual": True,
            "topics_generated": False,
            "hooks_english": True,
            "topics_english": True,
            "any_translated": False,
            "english_source": None,
            "transcript_meta": None,
            "cache_hit": False,
            "generated": False,
            "message": "No cached topics for these themes. Click Generate to extract.",
            "warning": "No cached topics for these themes. Click Generate to extract.",
        }

    if await request.is_disconnected():
        raise HTTPException(status_code=400, detail="Client disconnected during extract")

    llm = llm_pack
    try:
        extracted = await asyncio.wait_for(
            extract_hooks_and_topics_async(
                cues,
                start_sec=span_start,
                end_sec=span_end,
                theme_title=combined_title,
                theme_summary=combined_summary,
                search_entity=(body.search_entity or "").strip() or None,
                api_key=llm["api_key"],
                model=llm["model"],
                claude_api_key=llm["claude_api_key"],
                claude_model=llm["claude_model"],
                provider=llm["provider"],
                openrouter_api_key=llm["openrouter_api_key"],
                openrouter_model=llm["openrouter_model"],
                openrouter_base_url=llm["openrouter_base_url"],
                english_cues=english_cues,
                include_hooks=include_hooks,
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
        h["id"] = h.get("id") or f"hook_{i + 1}"
    # Preserve original ids (never reassign): hooks carry `topic_id`/`subtopic_id`
    # and subtopics carry `parent_topic_id` pointing at these exact ids — a blind
    # sequential re-id here breaks that linkage without touching those pointers.
    for i, t in enumerate(topics):
        t["id"] = t.get("id") or f"topic_{i + 1}"
    topic_tree = list(extracted.get("topic_tree") or [])[:24]
    for i, node in enumerate(topic_tree):
        node["id"] = node.get("id") or f"topic_{i + 1}"
    if not include_hooks:
        hooks = []
        for node in topic_tree:
            node["hooks"] = []
            for sub in list(node.get("subtopics") or []):
                sub["hooks"] = []

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
        api_key=llm["api_key"],
        model=llm["model"],
        claude_api_key=llm["claude_api_key"],
        claude_model=llm["claude_model"],
        provider=llm["provider"],
        openrouter_api_key=llm["openrouter_api_key"],
        openrouter_model=llm["openrouter_model"],
        openrouter_base_url=llm["openrouter_base_url"],
    )
    # Aggregate per-theme transcript diagnostics (from extract helpers).
    # Structural proof: never ship topic_tree sections with empty hooks arrays
    # after the full extract (topics+hooks). In topics-only mode hooks are
    # intentionally cleared until /extract/hooks — pruning here wiped every
    # topic (topic_tree_count stayed in per_theme meta while the payload was []).
    empty_hook_sections = 0
    if include_hooks:
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
                    topics.append(
                        {
                            **{k: v for k, v in sub.items() if k != "hooks"},
                            "is_subtopic": True,
                            # `_reindex_topic_tree`'s nested subtopic dicts don't carry
                            # this pointer themselves — without it the flat topic list
                            # loses the subtopic → parent link the UI tree relies on.
                            "parent_topic_id": t.get("id"),
                        }
                    )
                    hooks_from_tree.extend(list(sub.get("hooks") or []))
            if hooks_from_tree:
                hooks = hooks_from_tree[:24]
                for i, h in enumerate(hooks):
                    h["id"] = h.get("id") or f"hook_{i + 1}"
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
        "llm_provider": (meta.get("llm_provider") if isinstance(meta, dict) else None),
        "claude_preferred": (meta.get("claude_preferred") if isinstance(meta, dict) else None),
        "empty_hook_sections": (
            int(meta.get("empty_hook_sections") or 0)
            if isinstance(meta, dict) and meta.get("empty_hook_sections") is not None
            else empty_hook_sections
        ),
        "per_theme": per_theme_meta,
    }
    # Prefer live tree count after any final prune.
    transcript_meta["empty_hook_sections"] = empty_hook_sections
    theme_title = str(getattr(slices[0], "title", "") or "") if slices else ""
    hooks, topic_tree = _sanitize_extract_hook_payload(
        hooks, topic_tree, theme_title=theme_title
    )
    transcript_meta["hook_count"] = len(hooks)
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
        "cache_hit": False,
        "generated": True,
        "model": llm_cache_id,
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
                "llm_cache_id": llm_cache_id,
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
            model=llm_cache_id,
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
            CarouselGenerationSave.status == "ready",
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
    from datetime import timedelta

    from app.workers.indexer import CAROUSEL_LOCK_STALE_SEC

    row = await session.get(DriveFile, drive_file_id.strip())
    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")
    # Release orphaned locks on read so Continue/generate aren't stuck forever
    # when a prior worker died mid-generation without reclaiming.
    if getattr(row, "carousel_status", CAROUSEL_STATUS_IDLE) == CAROUSEL_STATUS_PROCESSING:
        locked_at = getattr(row, "carousel_locked_at", None)
        stale = locked_at is None or locked_at < (
            datetime.now(timezone.utc) - timedelta(seconds=CAROUSEL_LOCK_STALE_SEC)
        )
        if stale:
            row.carousel_status = CAROUSEL_STATUS_IDLE
            row.carousel_lock_token = None
            row.carousel_lock_input_hash = None
            row.carousel_locked_at = None
            await session.commit()
            logger.info("Released stale carousel lock on status poll drive=%s", row.id)
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
        # Avoid re-picking the same JPEG; rank locally from cache when possible.
        prev_ts = candidate.get("frame_ts")
        if prev_ts is not None:
            try:
                candidate["_avoid_timestamps"] = [float(prev_ts)]
            except (TypeError, ValueError):
                pass
        polished = await _polish_outline_frames(
            [candidate],
            session,
            prefer_local=True,
            max_candidates=4,
            llm_pack=resolve_carousel_llm(body.llm_provider, body.llm_model),
        )
        _attach_layout_panels([{"slides": polished}])
        await _prewarm_carousel_frames([{"slides": polished}], session, get_settings())
        slides[body.slide_index] = polished[0]
        target_car["slides"] = slides
        target_car["slide_count"] = len(slides)
        payload["carousels"] = carousels
        if carousels and carousels[0] is target_car:
            payload["slides"] = slides
        payload["images_ready"] = True
        payload["layouts"] = {
            "single_1": {
                "layout_mode": "single_1",
                "carousels": _layout_carousels(carousels, split=False),
            },
            "split_2": {
                "layout_mode": "split_2",
                "carousels": _layout_carousels(carousels, split=True),
            },
        }
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
        list_cached_timestamps_in_span,
        load_cached_frame_bytes,
        sample_candidate_timestamps,
        HARVEST_NEAREST_TOLERANCE_SEC,
    )

    settings = get_settings()
    drive_file, cues = await _load_video_cues(session, drive_file_id.strip())
    span_start = float(start_sec or 0)
    span_end = float(end_sec) if end_sec is not None else span_start + 40.0
    if span_end < span_start:
        span_end = span_start + 40.0

    thumb = str(settings.thumbnail_dir)
    fid = drive_file.id
    target = max(1, min(int(limit or 24), 24))

    # Fast path: prefer precomputed on-disk frames so the picker grid fills
    # without waiting on ffmpeg/Drive extracts.
    cached_ts = list_cached_timestamps_in_span(
        thumb, fid, span_start, span_end, pad_sec=1.0, limit=max(target * 2, 32)
    )

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

    def _cue_for(ts: float) -> tuple[float, float | None, str] | None:
        if not window:
            return None
        return min(window, key=lambda c: abs(c[0] - ts))

    by_ts: dict[float, dict[str, Any]] = {}
    # Seed with real cached frames first (snappy img loads via cache_only).
    for ts in cached_ts:
        key = round(float(ts), 2)
        nearest_cue = _cue_for(key)
        by_ts[key] = {
            "start_sec": key,
            "end_sec": nearest_cue[1] if nearest_cue else None,
            "text": (nearest_cue[2][:400] if nearest_cue else ""),
            "frame_ts": key,
            "cue": bool(nearest_cue and abs(nearest_cue[0] - key) < 0.55),
            "cached_seed": True,
        }
    for s, e, text in window:
        key = round(s, 2)
        if key in by_ts:
            by_ts[key]["text"] = text[:400]
            by_ts[key]["cue"] = True
            by_ts[key]["start_sec"] = s
            by_ts[key]["end_sec"] = float(e) if e is not None else None
            continue
        by_ts[key] = {
            "start_sec": s,
            "end_sec": float(e) if e is not None else None,
            "text": text[:400],
            "frame_ts": key,
            "cue": True,
            "cached_seed": False,
        }

    # Light dense fill only when cache is thin — avoid 36 extract storms.
    if len(cached_ts) < target:
        dense_ts = sample_candidate_timestamps(
            span_start,
            span_end,
            max_candidates=min(16, max(8, target)),
            step_sec=0.75,
        )
        for ts in dense_ts:
            key = round(ts, 2)
            if key in by_ts:
                continue
            nearest_cue = _cue_for(key)
            by_ts[key] = {
                "start_sec": key,
                "end_sec": nearest_cue[1] if nearest_cue else None,
                "text": (nearest_cue[2][:400] if nearest_cue else ""),
                "frame_ts": key,
                "cue": False,
                "cached_seed": False,
            }

    raw_items = sorted(by_ts.values(), key=lambda x: float(x["frame_ts"]))
    images: list[bytes | None] = []
    for item in raw_items:
        images.append(
            load_cached_frame_bytes(
                thumb,
                fid,
                float(item["frame_ts"]),
                nearest_tolerance_sec=HARVEST_NEAREST_TOLERANCE_SEC,
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
        row.pop("cached_seed", None)
        ts = float(row["frame_ts"])
        # Prefer cache_only so the grid never triggers N ffmpeg extracts.
        suffix = "&cache_only=1" if cached else ""
        row["preview_url"] = f"/media/video/{fid}/frame?ts={ts:.3f}{suffix}"
        row["cached"] = cached
        return row

    items = [_row(i, cached=True) for i in kept_idx if images[i] is not None]
    # Cap cold fallbacks tightly — browser extract-on-view is what felt eternal.
    cold_budget = 4 if items else min(8, target)
    if len(items) < target:
        claimed = {round(float(x["frame_ts"]), 2) for x in items}
        fallback_order = sorted(
            range(len(raw_items)),
            key=lambda i: (
                0 if raw_items[i].get("cached_seed") or raw_items[i].get("cue") else 1,
                float(raw_items[i]["frame_ts"]),
            ),
        )
        cold_added = 0
        for i in fallback_order:
            if len(items) >= target or cold_added >= cold_budget:
                break
            if images[i] is not None:
                continue
            ts = round(float(raw_items[i]["frame_ts"]), 2)
            if ts in claimed:
                continue
            claimed.add(ts)
            items.append(_row(i, cached=False))
            cold_added += 1
    items.sort(key=lambda x: float(x["frame_ts"]))
    from app.routers.media import schedule_frame_extract

    for row in items:
        if not row.get("cached"):
            schedule_frame_extract(fid, float(row["frame_ts"]))
    return {
        "drive_file_id": fid,
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
    llm = resolve_carousel_llm(body.llm_provider, body.llm_model)
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
        api_key=llm["api_key"],
        model=llm["model"],
        claude_api_key=llm["claude_api_key"],
        claude_model=llm["claude_model"],
        provider=llm["provider"],
        openrouter_api_key=llm["openrouter_api_key"],
        openrouter_model=llm["openrouter_model"],
        openrouter_base_url=llm["openrouter_base_url"],
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

    def _sanitized(cues: list[tuple[float, float | None, str]]) -> list[tuple[float, float | None, str]]:
        out = []
        for s, e, t in cues:
            text = _clean_cue_text(t)
            if text:
                out.append((s, e, text))
        return out

    if english and _english_caption_track_usable(english, indexed):
        preferred = _sanitized(prefer_english_cues(english))
        if len(preferred) >= 6:
            return preferred, True
    return _sanitized(prefer_english_cues(indexed)), False


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


# Caption-track noise that must never reach slide copy, hooks, or titles.
_CUE_NOISE_RE = re.compile(
    r"\[\s*(?:music|applause|laughter|laughs|cheering|cheers|noise|inaudible|silence|__)\s*\]"
    r"|\(\s*(?:music|applause|laughter|laughs|cheering|cheers|noise|inaudible)\s*\)"
    r"|>>+",
    re.IGNORECASE,
)


def _clean_cue_text(text: str) -> str:
    """Strip [music]/(applause)/>> caption noise and collapse whitespace."""
    cleaned = _CUE_NOISE_RE.sub(" ", text or "")
    return " ".join(cleaned.split()).strip()


# Trailing conjunction (optionally followed by a bare pronoun) that leaves a
# headline hanging mid-thought, e.g. "…a tradition in India and it".
_DANGLING_TAIL_RE = re.compile(
    r"\s+(?:and|but|or|so|because|which|that|when|while|if)"
    r"(?:\s+(?:it|they|he|she|we|you|i|this|these|those))?$",
    re.IGNORECASE,
)


def _strip_dangling_tail(text: str) -> str:
    cleaned = " ".join((text or "").split()).strip().rstrip(",;:–—-")
    while True:
        trimmed = _DANGLING_TAIL_RE.sub("", cleaned).rstrip(",;:–—-").strip()
        if trimmed == cleaned or len(trimmed.split()) < 3:
            return cleaned if len(trimmed.split()) < 3 else trimmed
        cleaned = trimmed


def _hook_carousel_title(video_name: str, hook_text: str) -> str:
    """Readable carousel title: crafted hook label, never a raw transcript dump.

    Verbatim hooks glued to the filename used to ship titles like
    "<file> — Ghee more than a food it has been a tradition in India and it".
    """
    base = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
    base = " ".join((base or "").split()).strip()
    raw = _clean_cue_text((hook_text or "").strip())
    label = _heuristic_hook_line(raw)
    if not label or not _hook_is_readable(label):
        candidate = _complete_line(raw, max_len=72)
        label = candidate if candidate and _hook_is_readable(candidate) else ""
    label = _strip_dangling_tail(_complete_line(label, max_len=72)) if label else ""
    if label:
        return _complete_line(f"{base} — {label}", max_len=160) or label
    return _complete_line(base, max_len=160) or "Carousel"


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
    cues = []
    for s in segments:
        text = _clean_cue_text((s.text or "").strip() or (s.vlm_description or "").strip())
        if not text:
            continue
        cues.append(
            (
                float(s.start_sec),
                float(s.end_sec) if s.end_sec is not None else None,
                text,
            )
        )
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
            "warning": "This video doesn’t have a transcript yet. Wait until indexing finishes, then try again.",
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
    from app.search.carousel_trace import carousel_log, set_drive_file_id

    set_drive_file_id(body.drive_file_id)
    carousel_log(
        "generate_request_start",
        force=bool(body.force),
        generate=bool(body.generate),
        select_images=bool(body.select_images),
        hook_count=len(body.hooks or []),
        topic_count=len(body.topics or []),
        llm_provider=(body.llm_provider or "-"),
        llm_model=(body.llm_model or "-"),
    )
    # Cache-only reads must not claim the carousel lock.
    if not body.force and not body.generate:
        return await _carousel_pipeline_generate_impl(body, session)
    drive_file_id = body.drive_file_id.strip()
    token = await _claim_carousel(session, drive_file_id)
    try:
        return await _carousel_pipeline_generate_impl(body, session)
    finally:
        await _release_carousel(session, drive_file_id, token)


def _carousel_llm_pack(
    llm: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Studio LLM pack, or a heuristic-only empty pack when tests pass no keys."""
    if llm:
        return llm
    return {
        "provider": "auto",
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
        "claude_api_key": "",
        "claude_model": "",
        "openrouter_api_key": "",
        "openrouter_model": "",
        "openrouter_base_url": "",
    }


async def _build_hook_carousels(
    *,
    unique_hooks: list["TimedPick"],
    topics: list["TimedPick"],
    themes: list["PipelineThemeSlice"],
    intent: str,
    cue_corpus: list[tuple[float, float | None, str]],
    drive_file_id: str,
    video_name: str,
    min_slides: int,
    max_slides: int,
    select_images: bool,
    llm: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    copy_refs: list[str] | None = None,
    image_ref_bytes: list[bytes] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One carousel per hook, built sequentially against a shared reserved pool.

    Hooks are built in order with a shared pool of claimed lines/timestamps so
    two hooks never steal the same transcript line (root cause of cross-hook
    duplicate slides in the UI).
    """
    reserved_texts: set[str] = set()
    reserved_starts: set[float] = set()
    carousels: list[dict[str, Any]] = []
    copy_refs = list(copy_refs or [])
    image_ref_bytes = list(image_ref_bytes or [])
    references = list(references or [])

    for idx, hook in enumerate(unique_hooks):
        parent_topic = next(
            (
                topic
                for topic in topics
                if (
                    (hook.topic_id and topic.id == hook.topic_id)
                    or (
                        (hook.topic_text or "").strip()
                        and (topic.text or "").strip().casefold()
                        == (hook.topic_text or "").strip().casefold()
                    )
                )
            ),
            topics[0] if len(topics) == 1 else None,
        )
        matching_theme = next(
            (
                theme
                for theme in themes
                if (hook.theme_id or (parent_topic.theme_id if parent_topic else None))
                and theme.theme_id
                == (hook.theme_id or (parent_topic.theme_id if parent_topic else None))
            ),
            themes[0] if len(themes) == 1 else None,
        )
        hs, he = _anchor_hook_span(hook, cue_corpus)
        anchored = hook.model_copy(update={"start_sec": hs, "end_sec": he})
        plan = await _plan_hook_oneline_spans(
            cues=cue_corpus,
            hook=anchored,
            narrative_topic=parent_topic,
            narrative_theme=matching_theme,
            narrative_intent=intent,
            min_slides=min_slides,
            max_slides=max_slides,
            llm=_carousel_llm_pack(llm, api_key=api_key, model=model),
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
            copy_refs=copy_refs,
            image_ref_bytes=image_ref_bytes,
        )
        spans = list(plan.get("spans") or [])
        story_cues = list(plan.get("scoped_cues") or cue_corpus)
        if len(spans) < 2:
            logger.warning("hook carousel skipped (thin plan) id=%s", hook.id or idx)
            continue
        slides = _slides_from_exact_spans(
            spans,
            cues=story_cues,
            drive_file_id=drive_file_id,
            video_name=video_name,
            crafted_hook=hook.text,
            defer_images=not select_images,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        slides = _top_up_oneline_slides(
            slides,
            cues=story_cues,
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
        slides, duplicate_repairs = repair_duplicate_slides(
            slides,
            cues=story_cues,
            hook=anchored,
            min_slides=min_slides,
            drive_file_id=drive_file_id,
            video_name=video_name,
            defer_images=not select_images,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
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
        title = _hook_carousel_title(video_name, hook.text or "")
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
                "theme_context": matching_theme.title if matching_theme else None,
                "topic_context": (
                    parent_topic.text
                    if parent_topic is not None
                    else (hook.topic_text or None)
                ),
                "intent": intent or None,
                "plan_source": plan.get("source"),
                "images_ready": select_images,
                "hook_start_sec": hs,
                "hook_end_sec": he,
                "references": references,
                "duplicate_repairs": duplicate_repairs,
            }
        )
    return carousels


async def _carousel_pipeline_generate_impl(
    body: CarouselGenerateRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Generate Instagram-style carousels: exactly one hook (or topic) per request.

    Each hook gets ≥6 one-line slides of *exact* VTT text. The studio-selected
    LLM (Claude / OpenRouter / Gemini / auto) only proposes cut timestamps;
    the server never displays rewritten copy.

    Cache-first: without ``force``/``generate``, return a matching save or empty miss.
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
    # Product model: one hook (or one topic-as-goal) per request/job unit.
    if len(hooks) > 1 or (not hooks and len(topics) > 1):
        raise HTTPException(
            status_code=400,
            detail="Generate one hook at a time (one hook per request).",
        )

    # Product rule: ≥6 one-liners per hook group when cues allow.
    min_slides = min(max(int(body.min_slides), 6), 12)
    max_slides = min(max(int(body.max_slides), min_slides), 12)
    select_images = bool(body.select_images)

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
    if not unique_hooks:
        raise HTTPException(status_code=400, detail="Select at least one topic or hook")
    if len(unique_hooks) > 1:
        raise HTTPException(
            status_code=400,
            detail="Generate one hook at a time (one hook per request).",
        )

    polish_copy = bool(getattr(body, "polish_copy", False))
    llm = resolve_carousel_llm(body.llm_provider, body.llm_model)
    llm_cache_id = (
        f"{carousel_llm_cache_id(llm)}:{CAROUSEL_CUT_PROMPT_VERSION}:{SLIDE_COPY_PROMPT_VERSION}"
    )[:128]
    selection_hash = _carousel_selection_hash(
        drive_file_id=drive_file_id,
        hooks=unique_hooks if hooks else [],
        topics=topics,
        themes=list(body.themes or []),
        intent=body.intent or "",
        min_slides=min_slides,
        max_slides=max_slides,
        select_images=select_images,
        polish_copy=polish_copy,
        llm_cache_id=llm_cache_id,
    )

    if not body.force:
        cached = await session.scalar(
            select(CarouselGenerationSave)
            .where(
                CarouselGenerationSave.drive_file_id == drive_file_id,
                CarouselGenerationSave.kind == SAVE_KIND_CAROUSEL,
                or_(
                    CarouselGenerationSave.input_hash == selection_hash,
                    CarouselGenerationSave.theme_key == selection_hash,
                ),
                CarouselGenerationSave.status == "ready",
            )
            .order_by(CarouselGenerationSave.created_at.desc())
        )
        if cached is not None:
            payload = dict(cached.payload or {})
            if payload.get("carousels") or payload.get("slides"):
                logger.info(
                    "carousel generate cache hit drive=%s save_id=%s hash=%s",
                    drive_file_id,
                    cached.id,
                    selection_hash[:12],
                )
                if not payload.get("references"):
                    # Older saves may lack refs — attach live ones for UI + polish.
                    live_refs = await _load_attached_references(
                        session,
                        drive_file_id=drive_file_id,
                        hooks=unique_hooks,
                        themes=list(body.themes or []),
                    )
                    if live_refs:
                        payload["references"] = live_refs
                        for car in payload.get("carousels") or []:
                            if isinstance(car, dict) and not car.get("references"):
                                car["references"] = live_refs
                return {
                    **payload,
                    "save_id": cached.id,
                    "cache_hit": True,
                    "generated": False,
                    "input_hash": selection_hash,
                }

    if not body.force and not body.generate:
        logger.info(
            "carousel generate cache miss (no generate) drive=%s hash=%s",
            drive_file_id,
            selection_hash[:12],
        )
        return {
            "source": "cache_miss",
            "title": "",
            "slide_count": 0,
            "hooks": [h.text for h in unique_hooks],
            "topics": [t.text for t in topics],
            "slides": [],
            "carousels": [],
            "carousel_count": 0,
            "images_ready": False,
            "cache_hit": False,
            "generated": False,
            "input_hash": selection_hash,
            "message": "No cached carousel for this hook. Click Generate to build it.",
            "warning": "No cached carousel for this hook. Click Generate to build it.",
        }

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
    attached_refs = await _load_attached_references(
        session,
        drive_file_id=drive_file_id,
        hooks=unique_hooks,
        themes=list(body.themes or []),
    )
    copy_refs = [
        str(r.get("copy_text") or "").strip()
        for r in attached_refs
        if (r.get("ref_kind") or "").strip().lower() == "copy" and (r.get("copy_text") or "").strip()
    ]
    image_ref_bytes = await _load_reference_image_bytes_list(
        [
            str(r.get("image_url") or "").strip()
            for r in attached_refs
            if (r.get("ref_kind") or "").strip().lower() == "image" and (r.get("image_url") or "").strip()
        ],
        session=session,
        settings=settings,
    )
    if attached_refs:
        logger.info(
            "carousel generate refs drive=%s copy=%d image=%d (bytes_ok=%d)",
            drive_file_id,
            len(copy_refs),
            sum(1 for r in attached_refs if (r.get("ref_kind") or "").lower() == "image"),
            len(image_ref_bytes),
        )
    carousels = await _build_hook_carousels(
        unique_hooks=unique_hooks,
        topics=topics,
        themes=list(body.themes or []),
        intent=(body.intent or "").strip(),
        cue_corpus=cue_corpus,
        drive_file_id=drive_file_id,
        video_name=video_name,
        min_slides=min_slides,
        max_slides=max_slides,
        select_images=select_images,
        llm=llm,
        copy_refs=copy_refs,
        image_ref_bytes=image_ref_bytes,
        references=attached_refs,
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
            topics=topics,
            themes=list(body.themes or []),
            intent=(body.intent or "").strip(),
            cue_corpus=cue_corpus,
            drive_file_id=drive_file_id,
            video_name=video_name,
            min_slides=min_slides,
            max_slides=max_slides,
            select_images=select_images,
            llm=llm,
            copy_refs=copy_refs,
            image_ref_bytes=image_ref_bytes,
            references=attached_refs,
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
        llm=llm,
        used_english_track=used_english_track,
    )
    copy_provider = "verbatim"
    if polish_copy:
        from app.search.carousel_pipeline import finalize_carousels_instagram_copy

        carousels, copy_provider = await finalize_carousels_instagram_copy(
            carousels,
            intent=(body.intent or "").strip(),
            api_key=llm["api_key"],
            model=llm["model"],
            claude_api_key=llm["claude_api_key"],
            claude_model=llm["claude_model"] or "claude-sonnet-4-5-20250929",
            provider=llm["provider"],
            openrouter_api_key=llm["openrouter_api_key"],
            openrouter_model=llm["openrouter_model"],
            openrouter_base_url=llm["openrouter_base_url"],
        )
    carousels = _repair_generated_carousels_duplicates(
        carousels,
        unique_hooks=unique_hooks,
        cue_corpus=cue_corpus,
        min_slides=min_slides,
        drive_file_id=drive_file_id,
        video_name=video_name,
        select_images=select_images,
    )
    # Always cross-check slide lines against the indexed transcript at each
    # timestamp so invented polish/translation cannot ship.
    transcript_guard = _enforce_slides_match_transcript(carousels, cue_corpus)
    if transcript_guard.get("snapped"):
        logger.info(
            "carousel transcript guard snapped=%s ok=%s drive=%s",
            transcript_guard.get("snapped"),
            transcript_guard.get("ok"),
            drive_file_id,
        )
    # Deterministic and source-safe: no extra model call. The transcript guard
    # remains authoritative and is re-applied after any concise-prefix repair.
    carousels, quality_summary = apply_carousel_quality_pass(carousels)
    final_guard = _enforce_slides_match_transcript(carousels, cue_corpus)
    for key in ("checked", "ok", "snapped", "empty"):
        transcript_guard[key] = int(transcript_guard.get(key) or 0) + int(
            final_guard.get(key) or 0
        )
    if select_images:
        # One grouped frame pass for all carousel slides; the selector itself
        # scales Gemini rank batches to the number of ambiguous slides.
        flat_slides = [
            slide
            for carousel in carousels
            for slide in (carousel.get("slides") or [])
        ]
        polished = await _polish_outline_frames(
            flat_slides,
            session,
            style_copy_refs=copy_refs,
            style_image_bytes=image_ref_bytes,
            llm_pack=llm,
        )
        cursor = 0
        for carousel in carousels:
            count = len(carousel.get("slides") or [])
            carousel["slides"] = polished[cursor : cursor + count]
            carousel["slide_count"] = count
            cursor += count
    _attach_layout_panels(carousels)
    # Transcript-first: do not block generate on frame materialization when the
    # user has not asked for images yet. Hard prewarm runs on select-images /
    # select_images=true only.
    if select_images:
        frames_prewarmed = await _prewarm_carousel_frames(carousels, session, settings)
    else:
        frames_prewarmed = False
        for carousel in carousels:
            carousel["frames_prewarmed"] = False

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
        "copy_source": copy_provider,
        "quality_summary": quality_summary,
        "llm_model": llm_cache_id,
        "transcript_guard": transcript_guard,
        "cache_hit": False,
        "generated": True,
        "input_hash": selection_hash,
        "references": attached_refs,
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
            selection_hash=selection_hash,
        )
        result["save_id"] = save.id if save else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("carousel artifact save failed: %s", exc)
        await session.rollback()
    return result


@router.post("/pipeline/quality-check")
async def carousel_pipeline_quality_check(
    body: CarouselQualityCheckBody,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-score edited carousels without invoking an LLM."""
    drive_file_id = body.drive_file_id.strip()
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    carousels = [dict(item) for item in (body.carousels or []) if isinstance(item, dict)]
    if not carousels:
        raise HTTPException(status_code=400, detail="carousels are required")
    transcript_guard: dict[str, Any] = {"checked": 0, "ok": 0, "snapped": 0, "empty": 0}
    try:
        _drive_file, indexed_cues = await _load_video_cues(session, drive_file_id)
        english_cues = await _maybe_load_english_cues(_drive_file, indexed_cues)
        cue_corpus, _used = _select_carousel_cue_corpus(indexed_cues, english_cues)
        if cue_corpus:
            transcript_guard = _enforce_slides_match_transcript(carousels, cue_corpus)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("quality-check cue load failed drive=%s: %s", drive_file_id, exc)
    repaired, quality_summary = apply_carousel_quality_pass(carousels)
    return {
        "drive_file_id": drive_file_id,
        "carousels": repaired,
        "quality_summary": quality_summary,
        "transcript_guard": transcript_guard,
        "status": "current",
    }


@router.post("/pipeline/select-images")
async def carousel_pipeline_select_images(
    body: CarouselSelectImagesBody,
    session: AsyncSession = Depends(get_db),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    drive_file_id = body.drive_file_id.strip()
    trace_id = (request_id or uuid.uuid4().hex)[:64]
    started = time.monotonic()
    token = ""
    slide_count = sum(
        len(item.get("slides") or [])
        for item in (body.carousels or [])
        if isinstance(item, dict)
    )
    logger.info(
        "select-images trace=%s event=request_start drive=%s carousels=%d slides=%d provider=%s model=%s budget_sec=%.0f",
        trace_id,
        drive_file_id,
        len(body.carousels or []),
        slide_count,
        body.llm_provider or "default",
        body.llm_model or "default",
        _SELECT_IMAGES_REQUEST_TIMEOUT_SEC,
    )

    async def _claim_and_run() -> dict[str, Any]:
        nonlocal token
        claim_started = time.monotonic()
        logger.info(
            "select-images trace=%s event=lock_claim_start drive=%s elapsed_ms=%d",
            trace_id,
            drive_file_id,
            round((claim_started - started) * 1000),
        )
        token = await _claim_carousel(session, drive_file_id)
        logger.info(
            "select-images trace=%s event=lock_claim_done drive=%s stage_ms=%d elapsed_ms=%d",
            trace_id,
            drive_file_id,
            round((time.monotonic() - claim_started) * 1000),
            round((time.monotonic() - started) * 1000),
        )
        result = await _carousel_pipeline_select_images_impl(
            body, session, trace_id=trace_id
        )
        logger.info(
            "select-images trace=%s event=pipeline_done drive=%s elapsed_ms=%d",
            trace_id,
            drive_file_id,
            round((time.monotonic() - started) * 1000),
        )
        return result

    try:
        # Claim included in the budget: a wedged claim must still answer the
        # socket instead of letting the proxy cancel with no response.
        return await asyncio.wait_for(
            _claim_and_run(),
            timeout=_SELECT_IMAGES_REQUEST_TIMEOUT_SEC,
        )
    except HTTPException as exc:
        logger.warning(
            "select-images trace=%s event=http_error drive=%s status=%d elapsed_ms=%d detail=%s",
            trace_id,
            drive_file_id,
            exc.status_code,
            round((time.monotonic() - started) * 1000),
            str(exc.detail)[:160],
        )
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        logger.warning(
            "select-images trace=%s event=request_timeout drive=%s elapsed_ms=%d budget_sec=%.0f",
            trace_id,
            drive_file_id,
            round((time.monotonic() - started) * 1000),
            _SELECT_IMAGES_REQUEST_TIMEOUT_SEC,
        )
        raise HTTPException(
            status_code=504,
            detail="Image selection took too long. Retry — local frames will be used.",
        ) from exc
    except asyncio.CancelledError as exc:
        logger.warning(
            "select-images trace=%s event=request_cancelled drive=%s elapsed_ms=%d",
            trace_id,
            drive_file_id,
            round((time.monotonic() - started) * 1000),
        )
        raise HTTPException(
            status_code=504,
            detail="Image selection was interrupted. Retry — local frames will be used.",
        ) from exc
    finally:
        if token:
            release_started = time.monotonic()
            try:
                await asyncio.shield(_release_carousel(session, drive_file_id, token))
                logger.info(
                    "select-images trace=%s event=lock_release_done drive=%s stage_ms=%d elapsed_ms=%d",
                    trace_id,
                    drive_file_id,
                    round((time.monotonic() - release_started) * 1000),
                    round((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "select-images trace=%s event=lock_release_failed drive=%s stage_ms=%d elapsed_ms=%d error=%s",
                    trace_id,
                    drive_file_id,
                    round((time.monotonic() - release_started) * 1000),
                    round((time.monotonic() - started) * 1000),
                    str(exc)[:240],
                )


def _slide_display_copy_snapshot(slide: dict[str, Any]) -> dict[str, Any]:
    """Capture studio-facing copy fields so frame selection cannot rewrite them."""
    return {
        key: slide.get(key)
        for key in (
            "hook_line",
            "transcript_text",
            "caption",
            "snippet",
            "original_text",
            "highlight",
            "highlight_words",
            "copy_source",
            "copy_crafted",
            "invented_text",
            "transcript_verified",
            "transcript_snapped",
        )
    }


def _restore_slide_display_copy(slide: dict[str, Any], snapshot: dict[str, Any]) -> None:
    for key, value in snapshot.items():
        if value is not None:
            slide[key] = value


async def _carousel_pipeline_select_images_impl(
    body: CarouselSelectImagesBody,
    session: AsyncSession = Depends(get_db),
    *,
    trace_id: str = "-",
) -> dict[str, Any]:
    """Final image selection pass after the user reviewed/edited slide transcripts."""
    drive_file_id = body.drive_file_id.strip()
    if not drive_file_id:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    raw = list(body.carousels or [])
    if not raw:
        raise HTTPException(status_code=400, detail="No carousels to polish")

    polished: list[dict[str, Any]] = []
    copy_snapshots: list[list[dict[str, Any]]] = []
    for car in raw:
        item = dict(car)
        slides = [dict(s) for s in (item.get("slides") or []) if isinstance(s, dict)]
        item["slides"] = slides
        copy_snapshots.append([_slide_display_copy_snapshot(s) for s in slides])
        polished.append(item)

    # Select-images must only attach frames. Re-running the transcript snap /
    # quality pass here replaced Instagram-polished (or user-edited) copy with
    # raw VTT lines — the studio then showed different text, and Back kept it.
    transcript_guard: dict[str, Any] = {
        "checked": sum(len(s) for s in copy_snapshots),
        "ok": sum(len(s) for s in copy_snapshots),
        "snapped": 0,
        "empty": 0,
        "preserved_copy": True,
    }
    quality_summary: dict[str, Any] = {
        "carousel_count": len(polished),
        "average_score": 0,
        "needs_attention": 0,
        "issue_count": 0,
        "repair_count": 0,
        "algorithm": "select_images_preserves_copy",
    }
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

    from app.search.carousel_frame_select import index_cached_video_frames

    settings = get_settings()
    index_started = time.monotonic()
    cached_frame_index = await asyncio.to_thread(
        index_cached_video_frames,
        str(settings.thumbnail_dir),
        {
            str(slide.get("drive_file_id") or "").strip()
            for slide in all_slides
            if str(slide.get("drive_file_id") or "").strip()
        },
    )
    logger.info(
        "select-images trace=%s event=frame_index_done drive=%s stage_ms=%d files=%d frames=%d",
        trace_id,
        drive_file_id,
        round((time.monotonic() - index_started) * 1000),
        len(cached_frame_index),
        sum(len(index.timestamps) for index in cached_frame_index.values()),
    )

    # Harvest all slides locally and rank in grouped requests. This keeps the
    # default image pass at the hard three-call Gemini cap even with several
    # carousel tabs.
    style_refs: list[dict[str, Any]] = []
    for item in polished:
        for r in item.get("references") or []:
            if isinstance(r, dict):
                style_refs.append(r)
    if not style_refs:
        # Fall back to persisted theme/hook refs for this video.
        stage_started = time.monotonic()
        style_refs = await _load_attached_references(
            session,
            drive_file_id=drive_file_id,
            hooks=[],
            themes=[],
            include_all_for_drive=True,
        )
        logger.info(
            "select-images trace=%s event=references_loaded drive=%s stage_ms=%d references=%d",
            trace_id,
            drive_file_id,
            round((time.monotonic() - stage_started) * 1000),
            len(style_refs),
        )
    llm_pack = resolve_carousel_llm(body.llm_provider, body.llm_model)
    copy_refs = [
        str(r.get("copy_text") or "").strip()
        for r in style_refs
        if (r.get("ref_kind") or "").strip().lower() == "copy" and (r.get("copy_text") or "").strip()
    ]
    stage_started = time.monotonic()
    total_cached_frames = sum(
        len(index.timestamps) for index in cached_frame_index.values()
    )
    # Interactive select prefers cache-only speed, but a cold thumbnail volume
    # (frames=0) must still extract a few stills or Step 5 has nothing to pick.
    allow_extracts = total_cached_frames == 0
    logger.info(
        "select-images trace=%s event=frame_polish_start drive=%s slides=%d prefer_local=true allow_extracts=%s cached_frames=%d llm=%s mode=identity",
        trace_id,
        drive_file_id,
        len(all_slides),
        allow_extracts,
        total_cached_frames,
        carousel_llm_cache_id(llm_pack),
    )
    # Seed a sparse cache when the volume is empty so the identity catalog
    # has frames to scan (quote midpoints + heuristic samples).
    if allow_extracts:
        seed_ts: list[float] = []
        for slide in all_slides:
            try:
                start = float(slide.get("timestamp_sec") or 0)
                end = float(slide.get("end_timestamp_sec") or start)
            except (TypeError, ValueError):
                continue
            mid = round(start + max(0.0, end - start) * 0.5, 3)
            seed_ts.extend([round(start, 3), mid, round(end, 3)])
        unique_seeds: list[float] = []
        seen_seed: set[float] = set()
        for ts in seed_ts:
            if ts in seen_seed:
                continue
            seen_seed.add(ts)
            unique_seeds.append(ts)
        for ts in unique_seeds[:24]:
            try:
                await _ensure_outline_frame_bytes(
                    drive_file_id, ts, session, settings, exact_only=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "select-images seed extract failed %s@%.3f: %s",
                    drive_file_id,
                    ts,
                    exc,
                )
        cached_frame_index = await asyncio.to_thread(
            index_cached_video_frames,
            str(settings.thumbnail_dir),
            {
                str(slide.get("drive_file_id") or "").strip()
                for slide in all_slides
                if str(slide.get("drive_file_id") or "").strip()
            },
        )

    from app.search.carousel_identity_catalog import apply_identity_selection_to_slides

    selected_slides, identity_summary = await asyncio.to_thread(
        apply_identity_selection_to_slides,
        all_slides,
        thumbnail_dir=str(settings.thumbnail_dir),
        drive_file_id=drive_file_id,
        force_catalog=bool(getattr(body, "force", False)),
        prefer_hdr=True,
    )
    # Fallback: if identity catalog found nothing usable, keep the previous
    # span-local polish path so Step 5 still has options on sparse videos.
    empty_after = sum(
        1
        for slide in selected_slides
        if not (isinstance(slide.get("frame_candidate_items"), list) and slide["frame_candidate_items"])
    )
    if empty_after == len(selected_slides) and selected_slides:
        logger.info(
            "select-images trace=%s event=identity_empty_fallback drive=%s empty_slides=%d",
            trace_id,
            drive_file_id,
            empty_after,
        )
        selected_slides = await _polish_outline_frames(
            all_slides,
            session,
            prefer_local=True,
            max_candidates=3,
            max_rank_batches=2,
            timeout_sec=_SELECT_IMAGES_TIMEOUT_SEC,
            style_copy_refs=copy_refs,
            llm_pack=llm_pack,
            allow_extracts=True,
            trace_id=trace_id,
            cached_frame_index=cached_frame_index,
        )
        allow_extracts = True
        # Clear auto-applied previews so studio stays text-first.
        for slide in selected_slides:
            items = slide.get("frame_candidate_items")
            frame_ts = slide.get("frame_ts")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item["selected"] = False
                    item.setdefault("category", "same_person")
                    if frame_ts is not None and abs(
                        float(item.get("frame_ts") or -1) - float(frame_ts)
                    ) < 0.011:
                        item["recommended"] = True
                        item["recommendation_source"] = (
                            item.get("recommendation_source") or "local"
                        )
            slide["preview_url"] = None
            slide["frame_ts"] = None
            identity_summary = {
                "algorithm": "identity-fallback-local",
                "modes": {"fallback": len(selected_slides)},
                "slides": len(selected_slides),
            }
    if allow_extracts:
        # Extracts wrote new JPEGs; refresh the index so snap/canonical URLs hit.
        cached_frame_index = await asyncio.to_thread(
            index_cached_video_frames,
            str(settings.thumbnail_dir),
            {
                str(slide.get("drive_file_id") or "").strip()
                for slide in selected_slides
                if str(slide.get("drive_file_id") or "").strip()
            },
        )
    frame_sources: dict[str, int] = {}
    for slide in selected_slides:
        source = str(slide.get("frame_source") or "unknown")
        frame_sources[source] = frame_sources.get(source, 0) + 1
    logger.info(
        "select-images trace=%s event=frame_polish_done drive=%s stage_ms=%d slides=%d sources=%s allow_extracts=%s cached_frames=%d identity=%s",
        trace_id,
        drive_file_id,
        round((time.monotonic() - stage_started) * 1000),
        len(selected_slides),
        ",".join(f"{key}:{value}" for key, value in sorted(frame_sources.items())),
        allow_extracts,
        sum(len(index.timestamps) for index in cached_frame_index.values()),
        identity_summary,
    )
    for s in selected_slides:
        if isinstance(s.get("frame_quality"), dict):
            quality_rollup.append(s["frame_quality"])
    for (car_index, flat_index), slide in zip(slide_locations, selected_slides):
        car_slides = polished[car_index].setdefault("slides", [])
        local_index = sum(1 for c, _ in slide_locations[:flat_index] if c == car_index)
        if local_index < len(car_slides):
            car_slides[local_index] = slide
    for car_index, item in enumerate(polished):
        slides = list(item.get("slides") or [])
        snaps = copy_snapshots[car_index] if car_index < len(copy_snapshots) else []
        for slide_index, slide in enumerate(slides):
            if slide_index < len(snaps):
                _restore_slide_display_copy(slide, snaps[slide_index])
        _attach_layout_panels(
            [{"slides": slides}], cached_frame_index=cached_frame_index
        )
        _snap_slides_to_cached_preview(
            slides, settings, cached_frame_index=cached_frame_index
        )
        # Frame helpers can mutate slide dicts; restore display copy once more.
        for slide_index, slide in enumerate(slides):
            if slide_index < len(snaps):
                _restore_slide_display_copy(slide, snaps[slide_index])
        item["slides"] = slides
        item["slide_count"] = len(slides)
        item["images_ready"] = True
        if style_refs and not item.get("references"):
            item["references"] = style_refs

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
        "cache_hit": False,
        "generated": True,
        "llm_model": carousel_llm_cache_id(
            resolve_carousel_llm(body.llm_provider, body.llm_model)
        ),
        "title": primary.get("title"),
        "slides": primary.get("slides") or [],
        "slide_count": primary.get("slide_count") or 0,
        "references": style_refs,
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
        "quality_summary": quality_summary,
        "identity": identity_summary,
        "transcript_guard": transcript_guard,
    }
    try:
        stage_started = time.monotonic()
        logger.info(
            "select-images trace=%s event=persist_start drive=%s payload_carousels=%d payload_slides=%d",
            trace_id,
            drive_file_id,
            len(polished),
            len(all_slides),
        )
        save = await _persist_carousel_artifact(
            session,
            drive_file_id=drive_file_id,
            payload=result,
            source="select_images",
        )
        result["save_id"] = save.id if save else None
        logger.info(
            "select-images trace=%s event=persist_done drive=%s stage_ms=%d saved=%s",
            trace_id,
            drive_file_id,
            round((time.monotonic() - stage_started) * 1000),
            bool(save),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "select-images trace=%s event=persist_failed drive=%s stage_ms=%d error=%s",
            trace_id,
            drive_file_id,
            round((time.monotonic() - stage_started) * 1000),
            str(exc)[:240],
        )
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
        slides = await _polish_outline_frames(
            slides, session, llm_pack=resolve_carousel_llm()
        )
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
    slides = await _polish_outline_frames(
        slides, session, llm_pack=resolve_carousel_llm()
    )
    return {
        "source": "fallback",
        "title": title,
        "slide_count": len(slides),
        "hooks": selected_hooks,
        "topics": selected_topics,
        "slides": slides,
        "cues": _cues_from_slides(selected_hooks, selected_topics, slides),
    }


def _faces_near_slide(
    faces: list[dict[str, Any]],
    slide: dict[str, Any],
    *,
    window_sec: float = _SELECT_IMAGES_FACE_WINDOW_SEC,
) -> list[dict[str, Any]]:
    """Keep only detections near this slide so ranking cannot ship the full video."""
    try:
        start = float(slide.get("timestamp_sec") or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(slide.get("end_timestamp_sec") or start)
    except (TypeError, ValueError):
        end = start
    lo, hi = start - window_sec, end + window_sec
    near: list[dict[str, Any]] = []
    for face in faces:
        try:
            ts = float(face.get("timestamp_sec") or 0)
        except (TypeError, ValueError):
            continue
        if lo <= ts <= hi:
            near.append(face)
    return near


def _strip_slide_ranking_fields(slide: dict[str, Any]) -> dict[str, Any]:
    """Drop ranking-only blobs so select-images responses stay small."""
    cleaned = dict(slide)
    for key in ("faces", "face_detections", "frame_faces"):
        cleaned.pop(key, None)
    return cleaned


def _snap_slides_to_cached_preview(
    slides: list[dict[str, Any]],
    settings,
    *,
    cached_frame_index: dict[str, Any] | None = None,
) -> None:
    """Point each slide at a nearby indexer JPEG. Never ffmpeg/Drive on this path."""
    from app.search.carousel_frame_select import (
        HARVEST_NEAREST_TOLERANCE_SEC,
        carousel_frame_preview_url,
        nearest_cached_frame,
    )

    used: set[float] = set()
    tolerance = max(float(HARVEST_NEAREST_TOLERANCE_SEC), 5.0)
    for slide in slides:
        fid = str(slide.get("drive_file_id") or "").strip()
        if not fid:
            continue
        source = str(slide.get("frame_source") or "").strip().lower()
        if source == "manual" or slide.get("frame_locked"):
            if slide.get("frame_ts") is not None:
                used.add(round(float(slide["frame_ts"]), 3))
                slide["preview_url"] = (
                    slide.get("preview_url")
                    or carousel_frame_preview_url(fid, float(slide["frame_ts"]))
                )
            continue
        ts = slide.get("frame_ts")
        if ts is None:
            ts = _frame_ts(float(slide.get("timestamp_sec") or 0), slide.get("end_timestamp_sec"))
            slide["frame_ts"] = ts
        target = float(ts)
        # Always retarget to a real cached stem before emitting cache_only URLs.
        snapped = nearest_cached_frame(
            str(settings.thumbnail_dir),
            fid,
            target,
            nearest_tolerance_sec=tolerance,
            exclude_ts=used,
            cached_frames=(cached_frame_index or {}).get(fid),
        )
        if snapped is None:
            snapped = nearest_cached_frame(
                str(settings.thumbnail_dir),
                fid,
                target,
                nearest_tolerance_sec=tolerance,
                exclude_ts=None,
                cached_frames=(cached_frame_index or {}).get(fid),
            )
        if snapped is not None:
            snap_ts, _path = snapped
            slide["frame_ts"] = snap_ts
            slide["preview_url"] = carousel_frame_preview_url(fid, snap_ts)
            used.add(snap_ts)
            # Keep candidate items aligned with on-disk stems when present.
            items = slide.get("frame_candidate_items")
            if isinstance(items, list):
                verified_items: list[dict[str, Any]] = []
                for item in items:
                    if not isinstance(item, dict) or item.get("frame_ts") is None:
                        continue
                    item_snap = nearest_cached_frame(
                        str(settings.thumbnail_dir),
                        fid,
                        float(item["frame_ts"]),
                        nearest_tolerance_sec=tolerance,
                        cached_frames=(cached_frame_index or {}).get(fid),
                    )
                    if item_snap is None:
                        continue
                    item_ts = item_snap[0]
                    next_item = dict(item)
                    next_item["frame_ts"] = item_ts
                    next_item["preview_url"] = carousel_frame_preview_url(fid, item_ts)
                    verified_items.append(next_item)
                slide["frame_candidate_items"] = verified_items[:3]
                slide["frame_candidates"] = [
                    float(item["frame_ts"]) for item in verified_items[:3]
                ]
        else:
            # Never invent a cache_only URL for a timestamp with no JPEG.
            slide["preview_url"] = None
            items = slide.get("frame_candidate_items")
            if not isinstance(items, list) or not items:
                slide["frame_ts"] = None
                slide["frame_candidates"] = []
                slide["frame_candidate_items"] = []
            else:
                # Drop unverifiable candidates; leave slide without a selection.
                slide["frame_candidate_items"] = []
                slide["frame_candidates"] = []
                slide["frame_ts"] = None

        # Identity text_only + snap wipe can leave the picker empty while layout
        # panels still reference quote-window timestamps. Seed candidates from
        # those panels so Step 5 still offers Choose image options.
        _seed_candidates_from_layout_panels(
            slide,
            fid=fid,
            settings=settings,
            cached_frame_index=cached_frame_index,
            tolerance=tolerance,
        )


def _seed_candidates_from_layout_panels(
    slide: dict[str, Any],
    *,
    fid: str,
    settings: Any,
    cached_frame_index: dict[str, Any] | None,
    tolerance: float,
) -> None:
    """Backfill frame_candidate_items from panels when identity list is empty."""
    from app.search.carousel_frame_select import (
        carousel_frame_preview_url,
        nearest_cached_frame,
    )

    existing = slide.get("frame_candidate_items")
    if isinstance(existing, list) and existing:
        return
    panels = list(slide.get("panels") or []) + list(slide.get("_split_panels") or [])
    if not panels:
        return
    seen: set[float] = set()
    seeded: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict) or panel.get("frame_ts") is None:
            continue
        try:
            target = float(panel["frame_ts"])
        except (TypeError, ValueError):
            continue
        snapped = nearest_cached_frame(
            str(settings.thumbnail_dir),
            fid,
            target,
            nearest_tolerance_sec=tolerance,
            cached_frames=(cached_frame_index or {}).get(fid),
        )
        preview = None
        frame_ts = target
        if snapped is not None:
            frame_ts, _path = snapped
            preview = carousel_frame_preview_url(fid, frame_ts)
        else:
            raw_preview = panel.get("preview_url")
            if isinstance(raw_preview, str) and raw_preview.strip():
                # Keep panel URL as a last-resort picker option (may 404 until extract).
                preview = raw_preview.strip()
        if not preview:
            continue
        key = round(frame_ts, 3)
        if key in seen:
            continue
        seen.add(key)
        seeded.append(
            {
                "frame_ts": frame_ts,
                "preview_url": preview,
                "label": "quote window",
                "order": len(seeded),
                "quality_score": 0.0,
                "front_face_score": float(panel.get("front_face_score") or 0.0),
                "selected": False,
                "recommended": len(seeded) == 0,
                "recommendation_source": "quote_window" if len(seeded) == 0 else None,
                "category": "same_person",
                "identity_id": None,
                "identity_label": None,
                "hdr": False,
            }
        )
        if len(seeded) >= 3:
            break
    if not seeded:
        return
    slide["frame_candidate_items"] = seeded
    slide["frame_candidates"] = [float(item["frame_ts"]) for item in seeded]
    slide["frame_source"] = "quote_window"
    # Studio stays text-first: do not auto-select.
    slide["preview_url"] = None
    slide["frame_ts"] = None
    slide.pop("frame_warning", None)

async def _polish_outline_frames(
    slides: list[dict[str, Any]],
    session: AsyncSession,
    *,
    prefer_local: bool = False,
    max_candidates: int = 4,
    style_copy_refs: list[str] | None = None,
    style_image_bytes: list[bytes] | None = None,
    max_rank_batches: int = _SELECT_IMAGES_RANK_BATCHES,
    timeout_sec: float = _SELECT_IMAGES_TIMEOUT_SEC,
    llm_pack: dict[str, Any] | None = None,
    allow_extracts: bool = True,
    trace_id: str = "-",
    cached_frame_index: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Studio-picker rank + Instagram-ready check (span text unchanged)."""
    from app.llm.carousel_llm import vision_ready
    from app.search.carousel_frame_select import polish_slides_instagram_frames

    settings = get_settings()
    if not slides or not vision_ready(llm_pack, api_key=settings.gemini_api_key or ""):
        logger.info(
            "select-images trace=%s event=frame_polish_heuristic reason=%s slides=%d",
            trace_id,
            "no_slides" if not slides else "vision_not_configured",
            len(slides),
        )
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
        return [_strip_slide_ranking_fields(s) for s in slides]

    async def ensure_frame(drive_file_id: str, ts: float) -> bytes | None:
        return await _ensure_outline_frame_bytes(drive_file_id, ts, session, settings)

    try:
        # Feed indexed InsightFace detections into the local candidate scorer.
        # Older rows have no yaw; confidence and normalized box area still
        # provide a useful front-facing/portrait prior.
        face_started = time.monotonic()
        face_rows: dict[str, list[dict[str, Any]]] = {}
        for fid in {str(s.get("drive_file_id") or "") for s in slides}:
            if not fid:
                continue
            media = await session.scalar(select(Media).where(Media.drive_file_id == fid))
            if media is None:
                continue
            owned = [s for s in slides if str(s.get("drive_file_id") or "") == fid]
            bounds: list[tuple[float, float]] = []
            for slide in owned:
                try:
                    start = float(slide.get("timestamp_sec") or 0)
                    end = float(slide.get("end_timestamp_sec") or start)
                except (TypeError, ValueError):
                    continue
                bounds.append(
                    (start - _SELECT_IMAGES_FACE_WINDOW_SEC, end + _SELECT_IMAGES_FACE_WINDOW_SEC)
                )
            if not bounds:
                continue
            lo, hi = min(b[0] for b in bounds), max(b[1] for b in bounds)
            faces = list(
                (
                    await session.execute(
                        select(Face).where(
                            Face.media_id == media.id,
                            Face.frame_timestamp.is_not(None),
                            Face.frame_timestamp >= lo,
                            Face.frame_timestamp <= hi,
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
        logger.info(
            "select-images trace=%s event=face_metadata_done files=%d faces=%d stage_ms=%d",
            trace_id,
            len(face_rows),
            sum(len(rows) for rows in face_rows.values()),
            round((time.monotonic() - face_started) * 1000),
        )
        for slide in slides:
            fid = str(slide.get("drive_file_id") or "")
            if face_rows.get(fid):
                slide["faces"] = _faces_near_slide(face_rows[fid], slide)
        polished = await asyncio.wait_for(
            polish_slides_instagram_frames(
                slides,
                thumbnail_dir=str(settings.thumbnail_dir),
                api_key=settings.gemini_api_key or "",
                model=settings.gemini_model or "",
                max_candidates=max_candidates,
                # Interactive paths must never ffmpeg/Drive-extract mid-request:
                # a cancelled extract poisons the DB session and blows the proxy
                # budget. Cache-only there; background generate keeps extracts.
                ensure_frame=ensure_frame if allow_extracts else None,
                concurrency=3,
                prefer_local=prefer_local,
                style_copy_refs=style_copy_refs,
                style_image_bytes=style_image_bytes,
                max_rank_batches=max_rank_batches,
                timeout_sec=timeout_sec,
                llm_pack=llm_pack,
                trace_id=trace_id,
                cached_frame_index=cached_frame_index,
            ),
            timeout=timeout_sec,
        )
        return [_strip_slide_ranking_fields(s) for s in polished]
    except asyncio.TimeoutError:
        logger.warning(
            "select-images trace=%s event=frame_polish_timeout timeout_sec=%.0f slides=%d",
            trace_id,
            timeout_sec,
            len(slides),
        )
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
        return [_strip_slide_ranking_fields(s) for s in slides]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "select-images trace=%s event=frame_polish_failed slides=%d error=%s",
            trace_id,
            len(slides),
            str(exc)[:240],
        )
        for s in slides:
            s.setdefault("frame_source", "heuristic")
            s.setdefault("instagram_ready", False)
        return [_strip_slide_ranking_fields(s) for s in slides]


async def _prewarm_carousel_frames(
    carousels: list[dict[str, Any]],
    session: AsyncSession,
    settings,
) -> bool:
    """Materialize every selected carousel JPEG before publishing a ready artifact.

    Prefer an exact on-disk / extracted frame. When the local video is missing
    (common for yt: ids without a volume download), snap ``frame_ts`` to the
    nearest indexer JPEG so ``cache_only`` preview URLs resolve.
    """
    from app.search.carousel_frame_select import (
        HARVEST_NEAREST_TOLERANCE_SEC,
        cached_frame_path,
        nearest_cached_frame,
    )

    missing: list[str] = []
    # Widen beyond harvest when ffmpeg cannot run — still prefer close frames.
    _SNAP_TOLERANCE_SEC = max(float(HARVEST_NEAREST_TOLERANCE_SEC), 5.0)

    async def _warm_one(slide: dict[str, Any], *, used: set[float]) -> str | None:
        fid = str(slide.get("drive_file_id") or "").strip()
        if not fid:
            return "missing drive_file_id"
        ts = slide.get("frame_ts")
        if ts is None:
            ts = _frame_ts(float(slide.get("timestamp_sec") or 0), slide.get("end_timestamp_sec"))
            slide["frame_ts"] = ts
        target = float(ts)
        data = await _ensure_outline_frame_bytes(
            fid, target, session, settings, exact_only=True
        )
        if data:
            path = cached_frame_path(str(settings.thumbnail_dir), fid, target)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file():
                path.write_bytes(data)
            slide["frame_ts"] = round(target, 3)
            slide["preview_url"] = (
                f"/media/video/{fid}/frame?ts={round(target, 3):.3f}&cache_only=1"
            )
            used.add(round(target, 3))
            return None

        # Exact extract failed — retarget to a distinct nearby cached JPEG.
        snapped = nearest_cached_frame(
            str(settings.thumbnail_dir),
            fid,
            target,
            nearest_tolerance_sec=_SNAP_TOLERANCE_SEC,
            exclude_ts=used,
        )
        if snapped is None:
            # Last resort: allow reuse of an already-used neighbour rather than 503.
            snapped = nearest_cached_frame(
                str(settings.thumbnail_dir),
                fid,
                target,
                nearest_tolerance_sec=_SNAP_TOLERANCE_SEC,
                exclude_ts=None,
            )
        if snapped is None:
            return f"{fid}@{target:.3f}"
        snap_ts, snap_path = snapped
        slide["frame_ts"] = snap_ts
        slide["preview_url"] = (
            f"/media/video/{fid}/frame?ts={snap_ts:.3f}&cache_only=1"
        )
        if abs(snap_ts - target) > 0.05:
            logger.info(
                "carousel prewarm snapped %s@%.3f → %.3f (%s)",
                fid,
                target,
                snap_ts,
                snap_path.name,
            )
        used.add(snap_ts)
        return None

    for carousel in carousels:
        frame_items = list(carousel.get("slides") or [])
        for slide in list(frame_items):
            frame_items.extend(slide.get("panels") or [])
            frame_items.extend(slide.get("_split_panels") or [])
        used_ts: set[float] = set()
        # Sequential within a carousel so split panels can exclude claimed timestamps.
        results: list[str | None] = []
        for item in frame_items:
            results.append(await _warm_one(item, used=used_ts))
        carousel_missing = False
        for err in results:
            if err:
                missing.append(err)
                carousel_missing = True
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
    return cleaned, ""


def _pick_split_frame_timestamps(
    *,
    selected_ts: float,
    start: float,
    end_f: float,
    drive_file_id: str,
    thumbnail_dir: str | None,
    cached_frames: Any | None = None,
) -> tuple[float, float]:
    """Pick two panel timestamps that resolve to different stills when possible.

    Prefer on-disk frames inside the spoken span (far apart). Fall back to the
    span endpoints / a small nudge so split_2 never intentionally duplicates.
    """
    min_gap = 0.45
    left = round(float(selected_ts), 3)
    cached: list[float] = []
    if thumbnail_dir and drive_file_id:
        from app.search.carousel_frame_select import list_cached_timestamps_in_span

        cached = list_cached_timestamps_in_span(
            thumbnail_dir,
            drive_file_id,
            start,
            end_f,
            limit=32,
            cached_frames=cached_frames,
        )
    if len(cached) >= 2:
        anchor = min(cached, key=lambda t: abs(t - left))
        partner = max(cached, key=lambda t: abs(t - anchor))
        if abs(partner - anchor) >= min_gap:
            # Keep AI/heuristic pick on the left when it is already a cached ts.
            if abs(anchor - left) < 0.05:
                return round(anchor, 3), round(partner, 3)
            return round(anchor, 3), round(partner, 3)
        if abs(cached[-1] - cached[0]) >= min_gap:
            return round(cached[0], 3), round(cached[-1], 3)

    alternate = round(end_f if abs(end_f - left) >= min_gap else start, 3)
    if abs(alternate - left) < 0.01:
        alternate = round(left + 0.5, 3)
    if abs(alternate - left) < min_gap:
        if abs(end_f - start) >= min_gap:
            return round(start, 3), round(end_f, 3)
        alternate = round(left + 1.0, 3)
    return left, alternate


def _highlight_for_caption(
    caption: str,
    *,
    parent_text: str,
    parent_highlight: list[int] | None,
    parent_words: list[str] | None,
) -> tuple[list[int], list[str]]:
    """Map parent-slide highlight words onto a (possibly shorter) panel caption."""
    from app.search.carousel_pipeline import _normalize_highlight_indices

    cap = " ".join((caption or "").split()).strip()
    if not cap:
        return [], []
    words = parent_words or []
    if not words and parent_highlight and parent_text:
        parent_tokens = [w for w in re.split(r"\s+", parent_text.strip()) if w]
        words = [parent_tokens[i] for i in parent_highlight if 0 <= i < len(parent_tokens)]
    indices, hl_words = _normalize_highlight_indices(cap, None, words)
    return indices, hl_words


def _attach_layout_panels(
    carousels: list[dict[str, Any]],
    *,
    cached_frame_index: dict[str, Any] | None = None,
) -> None:
    """Attach cache-backed single and two-panel layout metadata to slides."""
    from app.search.carousel_frame_select import carousel_frame_preview_url, focal_point_for_slide

    try:
        thumbnail_dir = str(get_settings().thumbnail_dir)
    except Exception:  # noqa: BLE001
        thumbnail_dir = None

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
            left_ts, right_ts = _pick_split_frame_timestamps(
                selected_ts=selected_ts,
                start=start,
                end_f=end_f,
                drive_file_id=fid,
                thumbnail_dir=thumbnail_dir,
                cached_frames=(cached_frame_index or {}).get(fid),
            )
            text = str(
                slide.get("transcript_text")
                or slide.get("hook_line")
                or slide.get("snippet")
                or ""
            )
            parent_hl = slide.get("highlight") if isinstance(slide.get("highlight"), list) else []
            parent_words = (
                slide.get("highlight_words")
                if isinstance(slide.get("highlight_words"), list)
                else []
            )
            top, bottom = _split_exact_caption_lines(text)
            if top and bottom and top.strip() == bottom.strip():
                bottom = ""
            fx, fy, fs = focal_point_for_slide(slide, left_ts)
            ax, ay, afs = focal_point_for_slide(slide, right_ts)
            def panel(ts: float, caption: str, px: float, py: float, score: float) -> dict[str, Any]:
                hl, hl_words = _highlight_for_caption(
                    caption,
                    parent_text=text,
                    parent_highlight=list(parent_hl or []),
                    parent_words=[str(w) for w in (parent_words or [])],
                )
                return {
                    "drive_file_id": fid,
                    "frame_ts": ts,
                    "preview_url": carousel_frame_preview_url(fid, ts) if fid else None,
                    "caption": caption[:400] or None,
                    "highlight": hl,
                    "highlight_words": hl_words,
                    "focal_x": px,
                    "focal_y": py,
                    "front_face_score": score,
                }
            # single_1 is represented by one panel and the existing bottom
            # caption; split_2 is materialized in the artifact layouts below.
            slide["panels"] = [panel(left_ts, bottom or text, fx, fy, fs)]
            slide["_split_panels"] = [
                panel(left_ts, top, fx, fy, fs),
                panel(right_ts, bottom, ax, ay, afs),
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
    *,
    exact_only: bool = False,
) -> bytes | None:
    """Load or extract a JPEG for Gemini ranking (best-effort; never raises).

    When exact_only=True (carousel prewarm / split panels), never satisfy a
    timestamp with a neighbour's cached JPEG — that made both split panels
    show the same still after writing identical bytes under two paths.
    """
    from app.routers.media import _extract_frame_on_demand
    from app.search.carousel_frame_select import cached_frame_path, load_cached_frame_bytes

    out_path = cached_frame_path(str(settings.thumbnail_dir), drive_file_id, ts)
    if exact_only:
        if out_path.is_file():
            data = out_path.read_bytes()
            if data:
                return data
    else:
        cached = load_cached_frame_bytes(str(settings.thumbnail_dir), drive_file_id, ts)
        if cached:
            return cached
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
    from app.search.carousel_frame_select import carousel_frame_preview_url

    return carousel_frame_preview_url(fid, _frame_ts(start, end))


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
    used_english_track: bool,
    llm: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translate non-English slide one-liners to English for display (faithful, cached)."""
    from app.search.carousel_pipeline import _llm_has_any_key

    pack = _carousel_llm_pack(llm, api_key=api_key, model=model)
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
    model_key = carousel_llm_cache_id(pack)

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

    can_translate = pending and _llm_has_any_key(
        api_key=pack.get("api_key"),
        claude_api_key=pack.get("claude_api_key"),
        openrouter_api_key=pack.get("openrouter_api_key"),
        openrouter_model=str(pack.get("openrouter_model") or ""),
    )
    if can_translate:
        # Deduplicate strings for the LLM batch.
        unique: list[str] = []
        uniq_index: dict[str, int] = {}
        for line in pending:
            if line not in uniq_index:
                uniq_index[line] = len(unique)
                unique.append(line)
        try:
            translated = await _llm_translate_lines(
                unique,
                api_key=pack.get("api_key"),
                model=str(pack.get("model") or ""),
                claude_api_key=pack.get("claude_api_key"),
                claude_model=str(pack.get("claude_model") or ""),
                provider=str(pack.get("provider") or "auto"),
                openrouter_api_key=pack.get("openrouter_api_key"),
                openrouter_model=str(pack.get("openrouter_model") or ""),
                openrouter_base_url=str(pack.get("openrouter_base_url") or ""),
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


# One complete spoken thought — not a 3–12 word chip, and not a paragraph.
_ONELINE_MAX_WORDS = 28
_ONELINE_MIN_WORDS = 6
# Finished punctuated sentences can be a bit shorter ("Welcome to Physics Wallah.").
_ONELINE_TERMINATED_MIN_WORDS = 4
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
    words = [re.sub(r"[^\w']", "", w.lower()) for w in cleaned.split()]
    first = words[0] if words else ""
    # "And / But / So …" is a continuation even when YouTube capitalizes it.
    if not first or first in _MIDCLAUSE_OPENERS:
        return False
    if first in _PRONOUN_OPENERS and len(words) > 1 and words[1] in _MIDCLAUSE_OPENERS:
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
    # Auto-caption tracks are entirely lowercase, so casing carries no signal.
    if _RELAXED_CUE_LINES.get():
        return True
    return False


def _line_complete_enough(text: str) -> bool:
    """Require a finished spoken unit — punctuation for English; cue-sized for Indic."""
    t = (text or "").strip()
    n = len(t.split())
    if not t or n < _ONELINE_TERMINATED_MIN_WORDS:
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
    if n < _ONELINE_MIN_WORDS:
        return False
    # Many Hindi/Hinglish VTTs and all ASR tracks omit terminators — accept a
    # short clean cue line instead of dropping the transcript wholesale.
    if (_is_indic_or_non_latin_heavy(t) or _RELAXED_CUE_LINES.get()) and _line_starts_clean(t):
        last = t.split()[-1].lower().rstrip(".,;:…।॥\"')")
        if last in _DANGLING_ENDS:
            return False
        return n <= _ONELINE_MAX_WORDS + 2
    return False


def _trim_to_oneline(text: str, *, max_words: int = _ONELINE_MAX_WORDS) -> str:
    """Keep exact words but cut at a natural short boundary for one carousel line."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    # Prefer a single complete sentence/question (never pack multiple thoughts).
    qm = re.search(r"^(.{8,}?[?])(?:\s|$)", cleaned)
    if qm and _ONELINE_TERMINATED_MIN_WORDS <= len(qm.group(1).split()) <= max_words + 2:
        return qm.group(1).strip()
    sm = re.search(rf"^(.{{8,}}?[.!…।॥])(?:\s|$)", cleaned)
    if sm and _ONELINE_TERMINATED_MIN_WORDS <= len(sm.group(1).split()) <= max_words + 2:
        return sm.group(1).strip()
    # If multiple sentences remain, keep only the first finished one.
    multi = re.match(rf"^(.+?{_SENTENCE_END})\s+\S", cleaned)
    if multi and _ONELINE_TERMINATED_MIN_WORDS <= len(multi.group(1).split()) <= max_words + 4:
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


def _norm_transcript_key(text: str) -> str:
    return " ".join(
        w.lower().strip(".,!?;:\"'()[]")
        for w in re.split(r"\s+", (text or "").strip())
        if w
    )


def _line_exists_in_cues_near(
    text: str,
    cues: list[tuple[float, float | None, str]],
    *,
    start_sec: float,
    end_sec: float | None,
    window_pad: float = 8.0,
) -> bool:
    """True when slide text is an exact/near-exact cue substring around the timestamp."""
    needle = _norm_transcript_key(text)
    if len(needle) < 8:
        return False
    lo = max(0.0, float(start_sec) - window_pad)
    hi = float(end_sec) if end_sec is not None else float(start_sec) + window_pad
    hi = max(hi, float(start_sec)) + window_pad
    window_keys: list[str] = []
    for s, e, t in cues:
        cue_end = float(e) if e is not None else float(s) + 3.0
        if cue_end < lo or float(s) > hi:
            continue
        hay = _norm_transcript_key(t)
        if not hay:
            continue
        if needle == hay or needle in hay or hay in needle:
            return True
        window_keys.append(hay)
    if not window_keys:
        return False
    # Rolling/auto captions split one spoken line across several short cues, so
    # slide seeds stitched from consecutive cues never fit inside a single cue.
    # Verify against the stitched window before declaring the text invented.
    stitched = " ".join(window_keys)
    if needle in stitched:
        return True
    needle_tokens = needle.split()
    if len(needle_tokens) >= 6:
        window_tokens = set(stitched.split())
        matched = sum(1 for tok in needle_tokens if tok in window_tokens)
        return matched / len(needle_tokens) >= 0.9
    return False


def _enforce_slides_match_transcript(
    carousels: list[dict[str, Any]],
    cues: list[tuple[float, float | None, str]],
) -> dict[str, int]:
    """Snap invented slide lines back to exact transcript at the slide timestamp.

    Returns counters for logging / API diagnostics.
    """
    stats = {"checked": 0, "ok": 0, "snapped": 0, "empty": 0}
    if not cues:
        return stats
    for car in carousels:
        for slide in car.get("slides") or []:
            if not isinstance(slide, dict):
                continue
            stats["checked"] += 1
            start = float(slide.get("timestamp_sec") or slide.get("frame_ts") or 0)
            end_raw = slide.get("end_timestamp_sec")
            try:
                end = float(end_raw) if end_raw is not None else start + 3.0
            except (TypeError, ValueError):
                end = start + 3.0
            text = str(
                slide.get("transcript_text")
                or slide.get("hook_line")
                or slide.get("caption")
                or ""
            ).strip()
            if not text:
                stats["empty"] += 1
                continue
            original = str(slide.get("original_text") or "").strip()
            nearby = original or _exact_text_for_span(cues, start, end)
            if _line_exists_in_cues_near(text, cues, start_sec=start, end_sec=end):
                stats["ok"] += 1
                slide["transcript_verified"] = True
                continue
            original_verified = bool(original) and _line_exists_in_cues_near(
                original, cues, start_sec=start, end_sec=end
            )
            # Crafted copy may stay if the seed is on the transcript and numbers
            # are grounded — do not snap honest rewrites back to raw VTT. Ground
            # numbers against the whole spoken window, not just this slide's
            # seed, so figures quoted a few seconds away stay legal.
            window_lo = max(0.0, start - 8.0)
            window_hi = max(end, start) + 8.0
            window_text = " ".join(
                t
                for s, e, t in cues
                if not (
                    (float(e) if e is not None else float(s) + 3.0) < window_lo
                    or float(s) > window_hi
                )
            )
            if (
                original_verified
                and _hook_numbers_are_grounded(text, f"{nearby} {window_text}".strip())
                and 4 <= len(text.split()) <= 22
            ):
                stats["ok"] += 1
                slide["transcript_verified"] = True
                slide["copy_crafted"] = True
                continue
            # Snap to a clean exact one-liner first; the raw stitched seed is a
            # last resort (it can be a mid-clause rolling-caption dump).
            fixed = (
                _exact_text_for_span(cues, start, end)
                or (original if original_verified else "")
                or nearby
            )
            if not fixed:
                stats["empty"] += 1
                slide["transcript_verified"] = False
                continue
            slide.setdefault("invented_text", text[:400])
            slide["hook_line"] = fixed[:280]
            slide["transcript_text"] = fixed[:280]
            slide["caption"] = fixed[:280]
            slide["snippet"] = fixed[:280]
            slide["transcript_verified"] = True
            slide["transcript_snapped"] = True
            stats["snapped"] += 1
    return stats


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

    # Walk the talk forward: keep complete standalone lines, skip junk, never go back.
    complete = [
        c
        for c in candidates
        if _line_starts_clean(str(c["text"])) and _line_complete_enough(str(c["text"]))
    ]
    complete.sort(key=lambda c: float(c["start_sec"]))
    picked: list[dict[str, float]] = []
    seen_txt: set[str] = set()
    min_gap = 2.0
    running_toks = set(hook_toks)

    def _follows(text: str, *, is_first: bool) -> bool:
        score = _cue_relevance(text, running_toks if running_toks else hook_toks)
        if is_first:
            return (not hook_toks) or score >= _MIN_CUE_RELEVANCE or score == 0.0
        if not running_toks:
            return True
        return score >= _MIN_CUE_RELEVANCE * 0.5

    for c in complete:
        text = str(c["text"])
        key = text.lower()
        start = float(c["start_sec"])
        if key in seen_txt or any(key in s0 or s0 in key for s0 in seen_txt if len(s0) >= 12):
            continue
        if any(abs(start - p["start_sec"]) < min_gap for p in picked):
            continue
        if picked and start + 0.05 < float(picked[-1]["start_sec"]):
            continue
        if not _follows(text, is_first=not picked):
            continue
        seen_txt.add(key)
        running_toks |= _content_tokens(text, min_len=3)
        picked.append({"start_sec": start, "end_sec": float(c["end_sec"])})
        if len(picked) >= max(max_slides, min_slides):
            break

    # If cohesion skipped too aggressively, fill remaining gaps in time order only.
    if len(picked) < min_slides:
        for c in complete:
            if len(picked) >= min_slides:
                break
            text = str(c["text"])
            key = text.lower()
            start = float(c["start_sec"])
            if key in seen_txt or any(key in s0 or s0 in key for s0 in seen_txt if len(s0) >= 12):
                continue
            if any(abs(start - p["start_sec"]) < min_gap for p in picked):
                continue
            if picked and start + 0.05 < float(picked[-1]["start_sec"]):
                continue
            seen_txt.add(key)
            picked.append({"start_sec": start, "end_sec": float(c["end_sec"])})

    picked.sort(key=lambda c: c["start_sec"])
    return picked[: max(max_slides, min_slides)]


async def _plan_hook_oneline_spans_llm(
    cues: list[tuple[float, float | None, str]],
    *,
    hook: "TimedPick",
    narrative_topic: "TimedPick | None" = None,
    narrative_theme: "PipelineThemeSlice | None" = None,
    narrative_intent: str = "",
    min_slides: int,
    max_slides: int,
    llm: dict[str, Any],
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
    copy_refs: list[str] | None = None,
    image_ref_bytes: list[bytes] | None = None,
) -> tuple[list[dict[str, float]] | None, str]:
    """Any selected carousel LLM proposes cut timestamps; text is filled verbatim later."""
    import json

    from app.search.carousel_pipeline import _llm_complete_json, _llm_has_any_key

    if not _llm_has_any_key(
        api_key=llm.get("api_key"),
        claude_api_key=llm.get("claude_api_key"),
        openrouter_api_key=llm.get("openrouter_api_key"),
        openrouter_model=str(llm.get("openrouter_model") or ""),
    ):
        return None, "none"

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
        return None, "none"

    reserved_note = ""
    if reserved_starts:
        reserved_note = (
            f"\nDo NOT reuse these already-claimed start times (other hooks): "
            f"{sorted(list(reserved_starts))[:40]}\n"
        )

    copy_note = ""
    clean_copy = [c.strip() for c in (copy_refs or []) if (c or "").strip()][:8]
    if clean_copy:
        listed = "\n".join(f"- {c[:400]}" for c in clean_copy)
        copy_note = (
            "\nAttached COPY references (tone/angle inspiration only — NEVER output these "
            "as slide text; slides must stay exact transcript):\n"
            f"{listed}\n"
        )

    image_note = ""
    if image_ref_bytes:
        image_note = (
            "\nIMAGE references were attached in studio as mood/visual inspiration. "
            "Use that only when choosing which transcript cuts best support this hook "
            "(still: never invent spoken words).\n"
        )

    topic_label = (
        (narrative_topic.text if narrative_topic else None)
        or getattr(hook, "topic_text", None)
        or ""
    ).strip()
    theme_label = (narrative_theme.title if narrative_theme else "").strip()
    theme_summary = (narrative_theme.summary if narrative_theme else "").strip()
    prompt = (
        "You pick Instagram carousel CUTS from a video transcript catalog.\n"
        "These cuts are spoken-evidence windows. Display copy is crafted later — "
        "do not write slide captions here, but pick complete chronological beats "
        "a copywriter can turn into finished sentences.\n"
        "Build ONE coherent carousel argument, not a roundup of nearby tactics or "
        "a collection of disconnected quotes.\n"
        "Return ONLY JSON: {\"spans\":[{\"start_sec\":number,\"end_sec\":number,\"cue_i\":number}]}\n"
        f"Chosen theme (hard story boundary): {theme_label or '(not supplied)'}\n"
        f"Theme meaning: {theme_summary or '(not supplied)'}\n"
        f"Chosen topic (the ONE argument every slide must advance): "
        f"{topic_label or '(use the hook as the topic)'}\n"
        f"Directional intent: {narrative_intent or '(not supplied)'}\n"
        f"Selected performance hook: {hook.text}\n"
        f"Hook transcript anchor: {float(hook.start_sec or 0):.2f}s"
        f"–{float(hook.end_sec or (float(hook.start_sec or 0) + 8.0)):.2f}s\n"
        f"{copy_note}"
        f"{image_note}"
        f"Produce between {min_slides} and {max_slides} spans.\n"
        "Rules:\n"
        "- First decide the single claim the chosen topic makes. Reject every catalog row "
        "that introduces a different tactic, framework, or subject—even when nearby in time.\n"
        "- Build: hook/setup → problem or tension → explanation/evidence → payoff/action. "
        "Each slide must answer or deepen the previous slide; the final slide must resolve "
        "the opening promise.\n"
        "- Include the hook anchor once. The copywriter will place the crafted hook where it "
        "performs best: cover when it opens curiosity, middle when it is a reveal, or ending "
        "when it is the payoff. Do not pad around it with unrelated transcript cuts.\n"
        "- A spoken sentence must be completed on ONE slide whenever possible. If the exact "
        "sentence is split across catalog rows, it may occupy TWO consecutive slides only. "
        "Never stretch one sentence across 3+ slides.\n"
        "- Never select an orphan fragment. A slide that ends mid-sentence is valid only when "
        "the immediately following slide continues and completes that same sentence. A slide "
        "that starts mid-sentence is valid only as the second half of that pair.\n"
        "- Read adjacent catalog rows together before choosing them. Reject sequences whose "
        "combined slide text leaves an unfinished sentence, abruptly changes subject, repeats "
        "the same point, or requires missing context.\n"
        "- Do not start a new thought with And/But/So/Because/Then unless it is the second "
        "slide of a two-slide sentence pair.\n"
        "- You MAY skip ahead in the catalog when the next cue is filler or a "
        "tangent. Do NOT go backwards in time.\n"
        "- start_sec must be strictly increasing (≥2s gap); no overlapping clips.\n"
        "- start_sec/end_sec must match catalog times (use cue_i).\n"
        "- Stay on THIS hook/theme; timestamps must be accurate to the spoken line.\n"
        "- If you cannot build a meaningful 6-line arc from the catalog, return "
        "fewer good spans rather than dummy cuts.\n"
        f"{reserved_note}\n"
        f"Cue catalog JSON:\n{json.dumps(catalog, ensure_ascii=False)}"
    )
    try:
        raw, used = await _llm_complete_json(
            prompt=prompt,
            system=(
                "You plan carousel cuts from a transcript catalog. "
                "Return ONLY valid JSON. Never invent spoken words."
            ),
            temperature=0.2,
            max_tokens=2500,
            api_key=llm.get("api_key"),
            model=str(llm.get("model") or ""),
            claude_api_key=llm.get("claude_api_key"),
            claude_model=str(llm.get("claude_model") or ""),
            provider=str(llm.get("provider") or "auto"),
            openrouter_api_key=llm.get("openrouter_api_key"),
            openrouter_model=str(llm.get("openrouter_model") or ""),
            openrouter_base_url=str(llm.get("openrouter_base_url") or ""),
        )
        text = (raw or "").strip()
        if not text:
            return None, used
        m = re.search(r"\{[\s\S]*\}", text)
        parsed = json.loads(m.group() if m else text)
        rows = parsed.get("spans") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            return None, used
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
            if catalog and min(abs(s - float(c["s"])) for c in catalog) > 2.5:
                nearest = min(catalog, key=lambda c: abs(float(c["s"]) - s))
                s, e = float(nearest["s"]), float(nearest["e"])
            if reserved_starts and any(abs(s - x) < 1.8 for x in reserved_starts):
                continue
            spans.append({"start_sec": s, "end_sec": e})
            if len(spans) >= max_slides:
                break
        uniq: list[dict[str, float]] = []
        for sp in sorted(spans, key=lambda x: x["start_sec"]):
            if any(abs(sp["start_sec"] - u["start_sec"]) < 2.0 for u in uniq):
                continue
            uniq.append(sp)
        return (uniq if len(uniq) >= 2 else None), used
    except Exception as exc:  # noqa: BLE001
        logger.warning("hook oneline span plan (llm) failed: %s", exc)
        return None, "failed"


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
    narrative_topic: "TimedPick | None" = None,
    narrative_theme: "PipelineThemeSlice | None" = None,
    narrative_intent: str = "",
    min_slides: int,
    max_slides: int,
    llm: dict[str, Any] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
    copy_refs: list[str] | None = None,
    image_ref_bytes: list[bytes] | None = None,
) -> dict[str, Any]:
    """Plan ≥6 short exact-transcript spans that convey a hook's goal."""
    from app.search.carousel_pipeline import _llm_has_any_key

    source = "heuristic"
    spans: list[dict[str, float]] = []
    hs = float(hook.start_sec or 0)
    he = float(hook.end_sec) if hook.end_sec is not None else hs + 15.0
    pack = _carousel_llm_pack(llm, api_key=api_key, model=model)
    llm_used = "none"

    # Scope evidence to the chosen topic first, then its theme. Nearby transcript
    # is not necessarily the same story (the customer-acquisition talk switches
    # from Reddit to warm networks to lead scraping within seconds).
    scope_ranges: list[tuple[float, float]] = []
    if narrative_topic is not None:
        for item in narrative_topic.time_ranges or []:
            start = float(item.start_sec or 0)
            end = float(item.end_sec) if item.end_sec is not None else start + 45.0
            if end > start:
                scope_ranges.append((start, end))
        if not scope_ranges and (
            narrative_topic.start_sec or narrative_topic.end_sec is not None
        ):
            start = float(narrative_topic.start_sec or 0)
            end = (
                float(narrative_topic.end_sec)
                if narrative_topic.end_sec is not None
                else start + 45.0
            )
            if end > start:
                scope_ranges.append((start, end))
    if not scope_ranges and narrative_theme is not None:
        start = float(narrative_theme.start_sec or 0)
        end = (
            float(narrative_theme.end_sec)
            if narrative_theme.end_sec is not None
            else max(start + 90.0, he + 45.0)
        )
        if end > start:
            scope_ranges.append((start, end))

    scoped_cues = cues
    if scope_ranges:
        pad = 3.0
        scoped_cues = [
            cue
            for cue in cues
            if any(
                float(cue[0]) <= end + pad
                and float(cue[1] if cue[1] is not None else cue[0] + 3.0)
                >= start - pad
                for start, end in scope_ranges
            )
        ]
        # A malformed/narrow topic range should not make generation impossible.
        if len(scoped_cues) < 3:
            scoped_cues = cues

    def _localize(cands: list[dict[str, float]], forward: float) -> list[dict[str, float]]:
        local_lo = max(0.0, hs - 12.0)
        local_hi = he + forward
        local = [sp for sp in cands if local_lo <= float(sp["start_sec"]) <= local_hi]
        return local

    if scoped_cues and _llm_has_any_key(
        api_key=pack.get("api_key"),
        claude_api_key=pack.get("claude_api_key"),
        openrouter_api_key=pack.get("openrouter_api_key"),
        openrouter_model=str(pack.get("openrouter_model") or ""),
    ):
        planned, llm_used = await _plan_hook_oneline_spans_llm(
            scoped_cues,
            hook=hook,
            narrative_topic=narrative_topic,
            narrative_theme=narrative_theme,
            narrative_intent=narrative_intent,
            min_slides=min_slides,
            max_slides=max_slides,
            llm=pack,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
            copy_refs=copy_refs,
            image_ref_bytes=image_ref_bytes,
        )
        if planned:
            # Prefer cuts near the hook; widen only if the tight window is thin.
            for fwd in (55.0, 90.0, 140.0):
                local = _localize(planned, fwd)
                if len(local) >= min(min_slides, 4) or fwd == 140.0:
                    spans = local if local else planned
                    break
            tag = llm_used if llm_used not in ("none", "failed") else "llm"
            source = f"{tag}_cuts"

    # Always run heuristic — either as primary or to top up the LLM's short lists.
    heur = _plan_hook_oneline_spans_heuristic(
        scoped_cues,
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
        tag = llm_used if llm_used not in ("none", "failed") else "llm"
        source = f"{tag}+heuristic"

    # Wider window top-up if still thin (still respect reserved pool).
    if len(spans) < min_slides:
        wider = _plan_hook_oneline_spans_heuristic(
            scoped_cues,
            hook_start=max(0.0, hs - 12.0),
            hook_end=he + 55.0,
            min_slides=min_slides,
            max_slides=max_slides,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
            hook=hook,
        )
        spans = _merge_span_lists(spans, wider, limit=max_slides)
        if source.endswith("_cuts"):
            tag = llm_used if llm_used not in ("none", "failed") else "llm"
            source = f"{tag}+heuristic"
    return {
        "spans": spans[:max_slides],
        "source": source,
        "scoped_cues": scoped_cues,
    }


def _slide_idea_collides(text: str, others: list[str], *, threshold: float = 0.8) -> bool:
    return any(
        other and _carousel_idea_similarity(text, other) >= threshold for other in others
    )


def repair_duplicate_slides(
    slides: list[dict[str, Any]],
    *,
    cues: list[tuple[float, float | None, str]],
    hook: "TimedPick",
    min_slides: int,
    drive_file_id: str,
    video_name: str,
    defer_images: bool,
    reserved_texts: set[str] | None = None,
    reserved_starts: set[float] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace later duplicate-idea slides from unused transcript spans.

    Drops a duplicate only when no safe replacement exists and the deck remains
    above ``min_slides``. Timestamps stay chronological after a final sort.
    """
    working = [dict(slide) for slide in slides if isinstance(slide, dict)]
    repairs: list[str] = []
    if len(working) < 2:
        return working, repairs

    hs = float(hook.start_sec or 0)
    he = float(hook.end_sec) if hook.end_sec is not None else hs + 8.0
    hook_toks = _hook_token_set(hook)
    crafted = hook.text

    def _local_reserved(skip: dict[str, Any] | None = None) -> tuple[set[str], set[float]]:
        texts = set(reserved_texts or ())
        starts = set(reserved_starts or ())
        for slide in working:
            if skip is not None and slide is skip:
                continue
            key = _norm_slide_key(slide.get("transcript_text") or slide.get("hook_line") or "")
            if key:
                texts.add(key)
            try:
                starts.add(round(float(slide.get("timestamp_sec") or 0), 1))
            except (TypeError, ValueError):
                pass
        return texts, starts

    def _replacement_for(target: dict[str, Any]) -> dict[str, Any] | None:
        keep_texts = [
            _carousel_quality_text(slide)
            for slide in working
            if slide is not target
        ]
        local_texts, local_starts = _local_reserved(target)
        sentence_cands = _sentence_cut_candidates(
            cues,
            hook_start=hs,
            hook_end=he,
            reserved_texts=local_texts,
            reserved_starts=local_starts,
        )
        catalog = _cues_near_hook(
            cues,
            hs,
            he,
            back_sec=20.0,
            forward_sec=60.0,
            reserved_texts=local_texts,
            reserved_starts=local_starts,
        )
        options: list[tuple[float, float, float, str, float]] = []
        seen_opt: set[str] = set()
        for cand in sentence_cands:
            options.append(
                (
                    _cue_relevance(str(cand.get("text") or ""), hook_toks),
                    abs(float(cand.get("start_sec") or 0) - float(target.get("timestamp_sec") or 0)),
                    float(cand.get("start_sec") or 0),
                    str(cand.get("text") or ""),
                    float(cand.get("end_sec") or float(cand.get("start_sec") or 0) + 2.5),
                )
            )
        for row in catalog:
            text = _exact_text_for_span(cues, float(row["s"]), float(row["e"]))
            options.append(
                (
                    _cue_relevance(text, hook_toks),
                    abs(float(row["s"]) - float(target.get("timestamp_sec") or 0)),
                    float(row["s"]),
                    text,
                    float(row["e"]),
                )
            )
        options.sort(key=lambda row: (-row[0], row[1], row[2]))
        for score, _dist, start, text, end in options:
            key = _norm_slide_key(text)
            if not key or key in seen_opt:
                continue
            seen_opt.add(key)
            if (
                len(text.split()) < _ONELINE_MIN_WORDS
                or not _line_starts_clean(text)
                or not _line_complete_enough(text)
            ):
                continue
            if _is_reserved_line(
                text,
                start,
                reserved_texts=local_texts,
                reserved_starts=local_starts,
            ):
                continue
            if _slide_idea_collides(text, keep_texts):
                continue
            return _make_oneline_slide(
                text=text,
                start_sec=start,
                end_sec=end,
                drive_file_id=drive_file_id,
                video_name=video_name,
                crafted_hook=crafted,
                defer_images=defer_images,
                order=int(target.get("index") or len(working)),
            )
        return None

    # Walk later duplicates until the deck is unique or cannot be repaired.
    safety = 0
    while safety < 12:
        safety += 1
        pairs = find_duplicate_slide_pairs(working)
        if not pairs:
            break
        left, right = pairs[0]
        if not 0 <= right < len(working):
            break
        target = working[right]
        replacement = _replacement_for(target)
        if replacement is not None:
            working[right] = replacement
            repairs.append(f"slide_{right + 1}:replaced_duplicate")
            continue
        if len(working) > max(2, int(min_slides)):
            working.pop(right)
            repairs.append(f"slide_{right + 1}:dropped_duplicate")
            continue
        break

    working.sort(key=lambda slide: float(slide.get("timestamp_sec") or 0))
    for i, slide in enumerate(working):
        slide["index"] = i + 1
    return working, repairs


def _repair_generated_carousels_duplicates(
    carousels: list[dict[str, Any]],
    *,
    unique_hooks: list["TimedPick"],
    cue_corpus: list[tuple[float, float | None, str]],
    min_slides: int,
    drive_file_id: str,
    video_name: str,
    select_images: bool,
) -> list[dict[str, Any]]:
    """Second-pass duplicate repair after copy polish."""
    out: list[dict[str, Any]] = []
    for idx, carousel in enumerate(carousels):
        item = dict(carousel)
        hook = unique_hooks[min(idx, len(unique_hooks) - 1)] if unique_hooks else TimedPick(
            text=str(item.get("hook_goal") or item.get("title") or "Hook"),
            start_sec=float(item.get("hook_start_sec") or 0),
            end_sec=item.get("hook_end_sec"),
        )
        reserved_texts: set[str] = set()
        reserved_starts: set[float] = set()
        for prior in out:
            for slide in prior.get("slides") or []:
                key = _norm_slide_key(slide.get("transcript_text") or slide.get("hook_line") or "")
                if key:
                    reserved_texts.add(key)
                try:
                    reserved_starts.add(round(float(slide.get("timestamp_sec") or 0), 1))
                except (TypeError, ValueError):
                    pass
        slides, repairs = repair_duplicate_slides(
            list(item.get("slides") or []),
            cues=cue_corpus,
            hook=hook,
            min_slides=min_slides,
            drive_file_id=drive_file_id,
            video_name=video_name,
            defer_images=not select_images,
            reserved_texts=reserved_texts,
            reserved_starts=reserved_starts,
        )
        prior_repairs = [str(r) for r in (item.get("duplicate_repairs") or []) if str(r).strip()]
        item["slides"] = slides
        item["slide_count"] = len(slides)
        item["duplicate_repairs"] = prior_repairs + repairs
        out.append(item)
    return out


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
            expanded, es, ee = _verbatim_passage_for_span(
                cues,
                s,
                e,
                goal_text=None,
                min_words=_ONELINE_MIN_WORDS,
                target_sentences=1,
                max_sentences=1,
                max_words=_ONELINE_MAX_WORDS,
                max_back_sec=6.0,
                max_forward_sec=18.0,
                max_span_sec=22.0,
            )
            if expanded:
                text, s, e = expanded, es, float(ee if ee is not None else s + 2.5)
            else:
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


class CarouselFeedbackUpsert(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    target_kind: str = Field(..., min_length=1, max_length=16)  # theme | hook
    target_key: str = Field(..., min_length=1, max_length=256)
    target_label: str = Field(default="", max_length=400)
    rating: str | None = Field(default=None, max_length=8)  # up | down | null
    comment: str = Field(default="", max_length=800)


def _feedback_row_dict(row: CarouselItemFeedback) -> dict[str, Any]:
    return {
        "id": row.id,
        "drive_file_id": row.drive_file_id,
        "target_kind": row.target_kind,
        "target_key": row.target_key,
        "target_label": row.target_label,
        "rating": row.rating,
        "comment": row.comment,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/pipeline/feedback")
async def carousel_pipeline_feedback_list(
    drive_file_id: str = Query(..., min_length=1, max_length=128),
    target_kind: str | None = Query(default=None, max_length=16),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fid = drive_file_id.strip()
    if not fid:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    stmt = select(CarouselItemFeedback).where(CarouselItemFeedback.drive_file_id == fid)
    kind = (target_kind or "").strip().lower()
    if kind in {"theme", "hook"}:
        stmt = stmt.where(CarouselItemFeedback.target_kind == kind)
    rows = (await session.scalars(stmt.order_by(CarouselItemFeedback.updated_at.desc()))).all()
    return {"drive_file_id": fid, "items": [_feedback_row_dict(r) for r in rows]}


@router.put("/pipeline/feedback")
async def carousel_pipeline_feedback_upsert(
    body: CarouselFeedbackUpsert,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fid = body.drive_file_id.strip()
    kind = body.target_kind.strip().lower()
    key = body.target_key.strip()[:256]
    if not fid or not key:
        raise HTTPException(status_code=400, detail="drive_file_id and target_key are required")
    if kind not in {"theme", "hook"}:
        raise HTTPException(status_code=400, detail="target_kind must be theme or hook")
    rating = (body.rating or "").strip().lower() or None
    if rating not in {None, "up", "down"}:
        raise HTTPException(status_code=400, detail="rating must be up, down, or null")
    comment = (body.comment or "").strip()[:800] or None
    label = (body.target_label or "").strip()[:400] or None

    existing = await session.scalar(
        select(CarouselItemFeedback).where(
            CarouselItemFeedback.drive_file_id == fid,
            CarouselItemFeedback.target_kind == kind,
            CarouselItemFeedback.target_key == key,
        )
    )
    if existing is None:
        existing = CarouselItemFeedback(
            drive_file_id=fid,
            target_kind=kind,
            target_key=key,
            target_label=label,
            rating=rating,
            comment=comment,
        )
        session.add(existing)
    else:
        existing.target_label = label or existing.target_label
        existing.rating = rating
        existing.comment = comment
        existing.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(existing)
    return {"ok": True, "item": _feedback_row_dict(existing)}


class CarouselReferenceCreate(BaseModel):
    drive_file_id: str = Field(..., min_length=1, max_length=128)
    target_kind: str = Field(..., min_length=1, max_length=16)  # theme | hook
    target_key: str = Field(..., min_length=1, max_length=256)
    target_label: str = Field(default="", max_length=400)
    ref_kind: str = Field(..., min_length=1, max_length=16)  # image | copy
    image_url: str = Field(default="", max_length=2000)
    frame_ts: float | None = None
    copy_text: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=200)


async def _load_attached_references(
    session: AsyncSession,
    *,
    drive_file_id: str,
    hooks: list["TimedPick"],
    themes: list["PipelineThemeSlice"],
    include_all_for_drive: bool = False,
) -> list[dict[str, Any]]:
    """Load persisted theme/hook image+copy refs that apply to this generate job."""
    fid = (drive_file_id or "").strip()
    if not fid:
        return []
    rows = list(
        (
            await session.scalars(
                select(CarouselItemReference)
                .where(CarouselItemReference.drive_file_id == fid)
                .order_by(CarouselItemReference.updated_at.desc())
            )
        ).all()
    )
    if not rows:
        return []
    if include_all_for_drive:
        return [_reference_row_dict(r) for r in rows[:32]]

    hook_keys: set[str] = set()
    for h in hooks:
        if (h.id or "").strip():
            hook_keys.add(h.id.strip())
        if (h.text or "").strip():
            hook_keys.add(h.text.strip())
    theme_keys: set[str] = set()
    for t in themes:
        if (t.theme_id or "").strip():
            theme_keys.add(t.theme_id.strip())
        if (t.title or "").strip():
            theme_keys.add(t.title.strip())
        # Also match hooks that carry theme_id.
    for h in hooks:
        if (h.theme_id or "").strip():
            theme_keys.add(h.theme_id.strip())

    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        kind = (row.target_kind or "").strip().lower()
        key = (row.target_key or "").strip()
        keep = False
        if kind == "hook" and key in hook_keys:
            keep = True
        elif kind == "theme" and key in theme_keys:
            keep = True
        if not keep:
            continue
        if row.id in seen:
            continue
        seen.add(row.id)
        out.append(_reference_row_dict(row))
        if len(out) >= 24:
            break
    return out


async def _load_one_reference_image_bytes(
    url: str,
    *,
    session: AsyncSession,
    settings,
) -> bytes | None:
    """Best-effort load of a reference image into bytes for multimodal Gemini."""
    from pathlib import Path
    from urllib.parse import parse_qs, urlparse

    import httpx

    from app.search.carousel_frame_select import load_cached_frame_bytes

    raw = (url or "").strip()
    if not raw:
        return None

    # Uploaded carousel refs: /media/carousel-ref/{file_id}
    m = re.match(r"^/media/carousel-ref/([A-Za-z0-9_.-]+)$", raw)
    if m:
        path = Path(settings.thumbnail_dir) / "carousel_refs" / m.group(1)
        if path.is_file():
            data = path.read_bytes()
            return data if data else None
        return None

    # Video frame paths: /media/video/{fid}/frame?ts=...
    m = re.match(r"^/media/video/([^/]+)/frame", raw)
    if m:
        fid = m.group(1)
        qs = parse_qs(urlparse(raw).query)
        try:
            ts = float((qs.get("ts") or ["0"])[0])
        except (TypeError, ValueError):
            ts = 0.0
        cached = load_cached_frame_bytes(str(settings.thumbnail_dir), fid, ts)
        if cached:
            return cached
        return await _ensure_outline_frame_bytes(fid, ts, session, settings)

    if raw.startswith("http://") or raw.startswith("https://"):
        lower = raw.lower()
        # Skip Drive HTML view pages — not direct image bytes.
        if "drive.google.com/file/" in lower and "/view" in lower:
            return None
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(raw)
                if resp.status_code >= 400:
                    return None
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith("image/"):
                    return None
                data = resp.content
                if not data or len(data) > _MAX_REF_IMAGE_BYTES:
                    return None
                return data
        except Exception as exc:  # noqa: BLE001
            logger.debug("reference image fetch failed: %s", exc)
            return None
    return None


async def _load_reference_image_bytes_list(
    urls: list[str],
    *,
    session: AsyncSession,
    settings,
    limit: int = 4,
) -> list[bytes]:
    out: list[bytes] = []
    for url in urls:
        if len(out) >= limit:
            break
        data = await _load_one_reference_image_bytes(url, session=session, settings=settings)
        if data:
            out.append(data)
    return out


def _reference_row_dict(row: CarouselItemReference) -> dict[str, Any]:
    return {
        "id": row.id,
        "drive_file_id": row.drive_file_id,
        "target_kind": row.target_kind,
        "target_key": row.target_key,
        "target_label": row.target_label,
        "ref_kind": row.ref_kind,
        "image_url": row.image_url,
        "frame_ts": row.frame_ts,
        "copy_text": row.copy_text,
        "note": row.note,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _normalize_reference_image_url(raw: str) -> str | None:
    url = (raw or "").strip()
    if not url:
        return None
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="image_url is too long")
    lower = url.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return url
    if url.startswith("/media/") or url.startswith("/api/"):
        return url
    # Allow bare Drive file ids as a convenience (frontend can also pass full URLs).
    if re.fullmatch(r"[A-Za-z0-9_-]{10,128}", url):
        return f"https://drive.google.com/file/d/{url}/view"
    raise HTTPException(
        status_code=400,
        detail="image_url must be http(s), /media/…, or a Drive file id",
    )


_REF_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}
_REF_IMAGE_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_REF_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MiB


@router.post("/pipeline/references/upload-image")
async def carousel_pipeline_references_upload_image(
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Accept a local image for theme/hook refs; store on volume; return /media URL."""
    import os
    from pathlib import Path

    from app.storage import RetryableDiskSpaceError, ensure_disk_space

    raw_name = (file.filename or "ref.jpg").strip() or "ref.jpg"
    safe_name = Path(raw_name).name.replace("\x00", "")[:200] or "ref.jpg"
    ext = Path(safe_name).suffix.lower()
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in _REF_IMAGE_MIMES:
        mime = _REF_IMAGE_EXT_MIME.get(ext, "")
    if mime not in _REF_IMAGE_MIMES and ext not in _REF_IMAGE_EXT_MIME:
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (jpg, png, webp, gif).",
        )
    if not ext or ext not in _REF_IMAGE_EXT_MIME:
        ext = next((e for e, m in _REF_IMAGE_EXT_MIME.items() if m == mime), ".jpg")
        if ext == ".jpeg":
            ext = ".jpg"

    file_id = f"{uuid.uuid4().hex}{ext}"
    refs_dir = Path(get_settings().thumbnail_dir) / "carousel_refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    dest = refs_dir / file_id
    partial = dest.with_suffix(dest.suffix + ".partial")

    written = 0
    try:
        ensure_disk_space(dest)
        with open(partial, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_REF_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="Image exceeds 15 MiB upload limit")
                ensure_disk_space(partial, len(chunk))
                out.write(chunk)
        if written <= 0:
            raise HTTPException(status_code=400, detail="Empty upload")
        os.replace(partial, dest)
    except HTTPException:
        if partial.is_file():
            partial.unlink(missing_ok=True)
        raise
    except RetryableDiskSpaceError as exc:
        if partial.is_file():
            partial.unlink(missing_ok=True)
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if partial.is_file():
            partial.unlink(missing_ok=True)
        logger.exception("carousel ref image upload write failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        await file.close()

    url = f"/media/carousel-ref/{file_id}"
    logger.info("carousel ref image saved id=%s name=%s bytes=%d", file_id, safe_name, written)
    return {
        "ok": True,
        "url": url,
        "name": safe_name,
        "size": written,
    }


@router.get("/pipeline/references")
async def carousel_pipeline_references_list(
    drive_file_id: str = Query(..., min_length=1, max_length=128),
    target_kind: str | None = Query(default=None, max_length=16),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fid = drive_file_id.strip()
    if not fid:
        raise HTTPException(status_code=400, detail="drive_file_id is required")
    stmt = select(CarouselItemReference).where(CarouselItemReference.drive_file_id == fid)
    kind = (target_kind or "").strip().lower()
    if kind in {"theme", "hook"}:
        stmt = stmt.where(CarouselItemReference.target_kind == kind)
    rows = (await session.scalars(stmt.order_by(CarouselItemReference.updated_at.desc()))).all()
    return {"drive_file_id": fid, "items": [_reference_row_dict(r) for r in rows]}


@router.post("/pipeline/references")
async def carousel_pipeline_references_create(
    body: CarouselReferenceCreate,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fid = body.drive_file_id.strip()
    kind = body.target_kind.strip().lower()
    key = body.target_key.strip()[:256]
    ref_kind = body.ref_kind.strip().lower()
    if not fid or not key:
        raise HTTPException(status_code=400, detail="drive_file_id and target_key are required")
    if kind not in {"theme", "hook"}:
        raise HTTPException(status_code=400, detail="target_kind must be theme or hook")
    if ref_kind not in {"image", "copy"}:
        raise HTTPException(status_code=400, detail="ref_kind must be image or copy")

    label = (body.target_label or "").strip()[:400] or None
    note = (body.note or "").strip()[:200] or None
    image_url: str | None = None
    copy_text: str | None = None
    frame_ts: float | None = None

    if ref_kind == "image":
        image_url = _normalize_reference_image_url(body.image_url)
        if not image_url:
            raise HTTPException(status_code=400, detail="image_url is required for image refs")
        if body.frame_ts is not None:
            try:
                frame_ts = float(body.frame_ts)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="frame_ts must be a number") from exc
            if frame_ts < 0:
                frame_ts = 0.0
    else:
        copy_text = (body.copy_text or "").strip()[:4000] or None
        if not copy_text:
            raise HTTPException(status_code=400, detail="copy_text is required for copy refs")

    # Cap attachments per target so a row cannot grow without bound.
    existing_count = len(
        (
            await session.scalars(
                select(CarouselItemReference.id).where(
                    CarouselItemReference.drive_file_id == fid,
                    CarouselItemReference.target_kind == kind,
                    CarouselItemReference.target_key == key,
                )
            )
        ).all()
    )
    if existing_count >= 24:
        raise HTTPException(status_code=400, detail="At most 24 references per theme/hook")

    row = CarouselItemReference(
        drive_file_id=fid,
        target_kind=kind,
        target_key=key,
        target_label=label,
        ref_kind=ref_kind,
        image_url=image_url,
        frame_ts=frame_ts,
        copy_text=copy_text,
        note=note,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"ok": True, "item": _reference_row_dict(row)}


@router.delete("/pipeline/references/{ref_id}")
async def carousel_pipeline_references_delete(
    ref_id: int,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await session.get(CarouselItemReference, ref_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Reference not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True, "id": ref_id}


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
