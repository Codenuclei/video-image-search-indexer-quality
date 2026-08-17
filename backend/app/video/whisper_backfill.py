"""On-demand Whisper transcript backfill for indexed videos missing speech cues."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus, Media, MediaType, VideoSegment
from app.db.session import get_session_factory
from app.video.youtube_cache import video_cache_path
from app.video.youtube_registry import is_youtube_source

logger = logging.getLogger(__name__)

TRANSCRIPT_RUNNING_PREFIX = "__transcript__:running"
TRANSCRIPT_FAILED_PREFIX = "__transcript__:failed:"


def transcript_job_phase(error_message: str | None) -> str | None:
    """Return running phase label, 'failed', or None when idle/ready."""
    msg = (error_message or "").strip()
    if msg.startswith(TRANSCRIPT_RUNNING_PREFIX):
        rest = msg[len(TRANSCRIPT_RUNNING_PREFIX) :].lstrip(":").strip()
        return rest or "transcribing"
    if msg.startswith(TRANSCRIPT_FAILED_PREFIX):
        return "failed"
    return None


def transcript_job_failed_message(error_message: str | None) -> str | None:
    msg = (error_message or "").strip()
    if msg.startswith(TRANSCRIPT_FAILED_PREFIX):
        return msg[len(TRANSCRIPT_FAILED_PREFIX) :].strip() or "Transcript generation failed"
    return None


def _phase_message(phase: str) -> str:
    labels = {
        "starting": "Getting transcripts from the video…",
        "loading_model": "Loading speech recognition model…",
        "extracting_audio": "Extracting audio from the video…",
        "transcribing": "Transcribing speech…",
        "saving": "Saving transcript cues…",
        "indexing": "Indexing transcript for search…",
    }
    return labels.get(phase, "Getting transcripts from the video…")


async def _set_transcript_phase(session: AsyncSession, drive_file_id: str, phase: str) -> None:
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        return
    drive_file.error_message = f"{TRANSCRIPT_RUNNING_PREFIX}:{phase}"
    await session.commit()


async def _set_transcript_failed(session: AsyncSession, drive_file_id: str, message: str) -> None:
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        return
    drive_file.error_message = f"{TRANSCRIPT_FAILED_PREFIX}{message[:1800]}"
    await session.commit()


async def _clear_transcript_job(session: AsyncSession, drive_file_id: str) -> None:
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        return
    msg = (drive_file.error_message or "").strip()
    if msg.startswith(TRANSCRIPT_RUNNING_PREFIX) or msg.startswith(TRANSCRIPT_FAILED_PREFIX):
        drive_file.error_message = None
        await session.commit()


async def count_text_cues(session: AsyncSession, media_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(VideoSegment)
                .where(
                    VideoSegment.media_id == media_id,
                    or_(VideoSegment.text != "", VideoSegment.vlm_description != ""),
                )
            )
        ).scalar_one()
        or 0
    )


async def transcript_status_payload(
    session: AsyncSession, drive_file_id: str
) -> dict[str, Any]:
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        return {"ok": False, "status": "not_found", "cue_count": 0, "message": "Video not found"}

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
    ).scalar_one_or_none()
    cue_count = await count_text_cues(session, media.id) if media is not None else 0
    phase = transcript_job_phase(drive_file.error_message)
    failed = transcript_job_failed_message(drive_file.error_message)

    if cue_count > 0:
        return {
            "ok": True,
            "status": "ready",
            "drive_file_id": drive_file_id,
            "name": drive_file.name,
            "cue_count": cue_count,
            "has_captions": True,
            "phase": None,
            "message": f"Transcript ready ({cue_count} cues).",
        }
    if phase == "failed" or failed:
        return {
            "ok": False,
            "status": "failed",
            "drive_file_id": drive_file_id,
            "name": drive_file.name,
            "cue_count": 0,
            "has_captions": False,
            "phase": "failed",
            "message": failed or "Transcript generation failed",
        }
    if phase:
        return {
            "ok": True,
            "status": "running",
            "drive_file_id": drive_file_id,
            "name": drive_file.name,
            "cue_count": 0,
            "has_captions": False,
            "phase": phase,
            "message": _phase_message(phase),
        }
    return {
        "ok": True,
        "status": "missing",
        "drive_file_id": drive_file_id,
        "name": drive_file.name,
        "cue_count": 0,
        "has_captions": False,
        "phase": None,
        "message": "No transcript cues yet.",
    }


async def claim_transcript_job(
    session: AsyncSession, drive_file_id: str, *, force: bool = False
) -> str:
    """Claim Whisper job. Returns ready|running|claimed|missing_file."""
    drive_file = await session.get(DriveFile, drive_file_id)
    if drive_file is None:
        return "missing_file"

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
    ).scalar_one_or_none()
    if media is not None and await count_text_cues(session, media.id) > 0 and not force:
        await _clear_transcript_job(session, drive_file_id)
        return "ready"

    phase = transcript_job_phase(drive_file.error_message)
    if phase and phase != "failed" and not force:
        return "running"

    drive_file.error_message = f"{TRANSCRIPT_RUNNING_PREFIX}:starting"
    await session.commit()
    return "claimed"


async def ensure_whisper_transcript(
    drive_file_id: str,
    *,
    force: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run Whisper and persist cue segments. Safe to call from a background task."""
    settings = settings or get_settings()
    factory = get_session_factory()

    async with factory() as session:
        drive_file = await session.get(DriveFile, drive_file_id)
        if drive_file is None:
            return {"ok": False, "error": "not_found", "cue_count": 0}

        media = (
            await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
        ).scalar_one_or_none()
        if media is None:
            media = Media(drive_file_id=drive_file.id, type=MediaType.VIDEO)
            session.add(media)
            await session.flush()

        existing = await count_text_cues(session, media.id)
        if existing > 0 and not force:
            await _clear_transcript_job(session, drive_file_id)
            return {
                "ok": True,
                "cue_count": existing,
                "queued": False,
                "message": "Transcript already present",
            }

        if force and existing > 0:
            rows = (
                await session.execute(
                    select(VideoSegment).where(VideoSegment.media_id == media.id)
                )
            ).scalars().all()
            for seg in rows:
                if (seg.text or "").strip() or (seg.vlm_description or "").strip():
                    await session.delete(seg)
            await session.flush()

        local_path = video_cache_path(settings, drive_file)
        if not local_path.is_file() or local_path.stat().st_size <= 0:
            if is_youtube_source(drive_file):
                await _set_transcript_failed(
                    session,
                    drive_file_id,
                    "Local YouTube file missing on volume — cannot transcribe.",
                )
                return {"ok": False, "error": "cache_missing", "cue_count": 0}
            try:
                await _set_transcript_phase(session, drive_file_id, "extracting_audio")
                from app.drive.google_client import DriveDirectClient, DriveDirectError
                from app.pipelines.common import download_to_temp_file
                from app.storage import ensure_disk_space

                client = DriveDirectClient(session_factory=factory, settings=settings)
                expected_size = drive_file.size if drive_file.size and drive_file.size > 0 else None
                ensure_disk_space(str(local_path), expected_size or 0)
                partial = f"{local_path}.partial"
                suffix = local_path.suffix or ".mp4"
                try:
                    async with download_to_temp_file(
                        client,
                        drive_file.id,
                        settings,
                        suffix=suffix,
                        expected_size=expected_size,
                    ) as tmp:
                        shutil.move(tmp, partial)
                        os.replace(partial, str(local_path))
                finally:
                    if os.path.exists(partial):
                        os.remove(partial)
            except DriveDirectError as exc:
                await _set_transcript_failed(session, drive_file_id, str(exc))
                return {
                    "ok": False,
                    "error": "drive_required",
                    "cue_count": 0,
                    "message": str(exc),
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("Transcript cache download failed for %s", drive_file_id)
                await _set_transcript_failed(session, drive_file_id, str(exc))
                return {
                    "ok": False,
                    "error": "download_failed",
                    "cue_count": 0,
                    "message": str(exc),
                }

        try:
            await _set_transcript_phase(session, drive_file_id, "loading_model")
            await _set_transcript_phase(session, drive_file_id, "transcribing")
            from app.pipelines.video import _whisper_cues_for_video

            cues = await _whisper_cues_for_video(
                str(local_path), settings, drive_file_id=drive_file_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Whisper failed for %s", drive_file_id)
            await _set_transcript_failed(
                session,
                drive_file_id,
                f"Speech recognition failed: {exc}",
            )
            return {"ok": False, "error": "whisper_failed", "cue_count": 0, "message": str(exc)}

        if not cues:
            await _set_transcript_failed(
                session,
                drive_file_id,
                "No speech detected in this video (empty transcript).",
            )
            return {"ok": False, "error": "empty_transcript", "cue_count": 0}

        await _set_transcript_phase(session, drive_file_id, "saving")
        drive_file = await session.get(DriveFile, drive_file_id)
        media = (
            await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))
        ).scalar_one_or_none()
        if media is None:
            media = Media(drive_file_id=drive_file_id, type=MediaType.VIDEO)
            session.add(media)
            await session.flush()

        new_segments: list[VideoSegment] = []
        for cue in cues:
            seg = VideoSegment(
                media_id=media.id,
                start_sec=cue.start_sec,
                end_sec=cue.end_sec,
                text=cue.text,
            )
            session.add(seg)
            new_segments.append(seg)
        await session.flush()

        if settings.gemini_api_key:
            await _set_transcript_phase(session, drive_file_id, "indexing")
            from app.pipelines.video import _index_transcripts_local

            await _index_transcripts_local(
                drive_file_id=drive_file_id,
                segments=new_segments,
                settings=settings,
            )

        if drive_file is not None and drive_file.status != DriveFileStatus.PROCESSED:
            drive_file.status = DriveFileStatus.PROCESSED
        await _clear_transcript_job(session, drive_file_id)
        await session.commit()

        cue_count = await count_text_cues(session, media.id)
        logger.info(
            "Whisper transcript backfill complete for %s: %d cue(s)",
            drive_file_id,
            cue_count,
        )
        return {
            "ok": True,
            "cue_count": cue_count,
            "has_captions": cue_count > 0,
            "message": f"Transcript ready ({cue_count} cues).",
        }
