"""English transcript ensure / purge APIs (YouTube + Drive)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.video.english_transcript import (
    EnglishTranscriptError,
    ensure_english_transcript,
    find_non_english_transcript_videos,
    purge_non_english_transcripts,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

_GENERIC_FAILURE = (
    "Something went wrong while preparing the English transcript. "
    "Please try again in a moment."
)


class EnsureEnglishRequest(BaseModel):
    drive_file_id: str = Field(..., min_length=1, description="Drive or YouTube library file id")
    provider: str = Field(
        default="auto",
        description="LLM provider for translation: auto | claude | openrouter | gemini",
    )
    model: str | None = Field(
        default=None,
        description="Optional model id override for the selected provider",
    )
    force: bool = Field(
        default=False,
        description="Re-stitch/translate even when segments are already tagged English",
    )


class TranscriptSegmentOut(BaseModel):
    start_sec: float
    end_sec: float | None = None
    text: str


class EnsureEnglishResponse(BaseModel):
    ok: bool = True
    drive_file_id: str
    media_id: int
    cue_count: int
    translated: bool
    already_english: bool
    language: str
    source: str
    # UI-safe status line — show this to the user.
    message: str = ""
    llm_provider: str | None = None
    deleted_non_english: int = 0
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)


class PurgeNonEnglishRequest(BaseModel):
    dry_run: bool = True
    translate: bool = Field(
        default=True,
        description="When true, replace non-English transcripts with English; "
        "when false, only delete them",
    )
    provider: str = "auto"
    model: str | None = None
    limit: int = Field(default=200, ge=1, le=2000)


def _ui_error_detail(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or _GENERIC_FAILURE


@router.post("/ensure-english", response_model=EnsureEnglishResponse)
async def ensure_english_endpoint(
    body: EnsureEnglishRequest,
    session: AsyncSession = Depends(get_db),
) -> EnsureEnglishResponse:
    """
    Ensure the selected video's stored transcript is English.

    Stitches cues into complete sentences (timestamps preserved). Translates
    non-English text via the configured LLM. On failure, returns a user-friendly
    alert instead of writing partial or invented results.
    """
    try:
        result = await ensure_english_transcript(
            session,
            body.drive_file_id,
            provider=body.provider,
            model=body.model,
            force=body.force,
        )
        await session.commit()
    except EnglishTranscriptError as exc:
        await session.rollback()
        logger.warning(
            "ensure-english failed for %s: %s",
            body.drive_file_id,
            exc,
        )
        raise HTTPException(status_code=422, detail=_ui_error_detail(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.exception("ensure-english unexpected error for %s", body.drive_file_id)
        raise HTTPException(status_code=500, detail=_GENERIC_FAILURE) from exc

    return EnsureEnglishResponse(
        ok=True,
        drive_file_id=result.drive_file_id,
        media_id=result.media_id,
        cue_count=result.cue_count,
        translated=result.translated,
        already_english=result.already_english,
        language=result.language,
        source=result.source,
        message=result.user_message(),
        llm_provider=result.llm_provider,
        deleted_non_english=result.deleted_non_english,
        segments=[
            TranscriptSegmentOut(
                start_sec=s.start_sec,
                end_sec=s.end_sec,
                text=s.text,
            )
            for s in (result.segments or [])
        ],
    )


@router.get("/non-english")
async def list_non_english_transcripts(
    session: AsyncSession = Depends(get_db),
    limit: int = 200,
) -> dict:
    """List library videos whose stored transcript text is not English."""
    videos = await find_non_english_transcript_videos(session, limit=limit)
    if not videos:
        return {
            "ok": True,
            "count": 0,
            "videos": [],
            "message": "All listed transcripts look like English.",
        }
    return {
        "ok": True,
        "count": len(videos),
        "videos": videos,
        "message": f"Found {len(videos)} video(s) with a non-English transcript.",
    }


@router.post("/purge-non-english")
async def purge_non_english_endpoint(
    body: PurgeNonEnglishRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Delete stored non-English transcripts.

    Default dry_run=true lists candidates. With dry_run=false and translate=true,
    replaces each with a validated English transcript (LLM). Failures are
    reported per video with user-friendly messages — no silent false results.
    """
    try:
        result = await purge_non_english_transcripts(
            session,
            dry_run=body.dry_run,
            translate=body.translate,
            provider=body.provider,
            model=body.model,
            limit=body.limit,
        )
    except EnglishTranscriptError as exc:
        raise HTTPException(status_code=422, detail=_ui_error_detail(exc)) from exc

    if result.get("dry_run"):
        count = int(result.get("count") or 0)
        result["message"] = (
            f"Found {count} video(s) with a non-English transcript."
            if count
            else "No non-English transcripts found."
        )
        return result

    processed = int(result.get("processed") or 0)
    failed = int(result.get("failed") or 0)
    if failed and processed:
        result["message"] = (
            f"Updated {processed} video(s). "
            f"{failed} couldn’t be translated — nothing unsafe was saved for those."
        )
    elif failed:
        result["message"] = (
            f"Couldn’t update {failed} video(s). "
            "Nothing was changed for those — please try again."
        )
    elif processed:
        result["message"] = f"Updated {processed} video(s) to English transcripts."
    else:
        result["message"] = "No non-English transcripts needed updating."
    return result
