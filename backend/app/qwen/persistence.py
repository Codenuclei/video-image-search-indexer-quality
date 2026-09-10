"""Durable, versioned Qwen enrichment records.

The Qwen response is the only caption source. Gemini is used later only to
embed the canonical caption text.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    QwenCaption,
    QwenEnrichmentJob,
    VideoSegmentLabel,
)
from app.objects.identify_tags import IdentifyResult, persist_rows

QWEN_ENRICHMENT_PROMPT_VERSION = "caption-objects-actions-v1"


def qwen_target_key(*, media_id: int | None = None, video_segment_id: int | None = None) -> str:
    if (media_id is None) == (video_segment_id is None):
        raise ValueError("exactly one Qwen target is required")
    return f"segment:{video_segment_id}" if video_segment_id is not None else f"media:{media_id}"


async def ensure_qwen_job(
    session: AsyncSession,
    *,
    media_id: int | None = None,
    video_segment_id: int | None = None,
    prompt_version: str = QWEN_ENRICHMENT_PROMPT_VERSION,
    model_version: str,
) -> QwenEnrichmentJob:
    target_key = qwen_target_key(media_id=media_id, video_segment_id=video_segment_id)
    job = await session.scalar(
        select(QwenEnrichmentJob).where(
            QwenEnrichmentJob.target_key == target_key,
            QwenEnrichmentJob.prompt_version == prompt_version,
            QwenEnrichmentJob.model_version == model_version,
        )
    )
    if job is None:
        job = QwenEnrichmentJob(
            target_key=target_key,
            media_id=media_id,
            video_segment_id=video_segment_id,
            prompt_version=prompt_version,
            model_version=model_version,
        )
        session.add(job)
        await session.flush()
    return job


async def persist_qwen_result(
    session: AsyncSession,
    *,
    parsed: IdentifyResult,
    raw_text: str,
    model_version: str,
    media_id: int | None = None,
    video_segment_id: int | None = None,
    prompt_version: str = QWEN_ENRICHMENT_PROMPT_VERSION,
) -> QwenEnrichmentJob:
    """Atomically persist raw output, normalized result, caption, and moment labels."""
    job = await ensure_qwen_job(
        session,
        media_id=media_id,
        video_segment_id=video_segment_id,
        prompt_version=prompt_version,
        model_version=model_version,
    )
    job.status = "processing"
    job.attempts += 1
    job.error_message = None

    caption = " ".join((parsed.caption or "").split()).strip()
    if not caption:
        raise ValueError("Qwen output did not contain the canonical caption")

    rows = persist_rows(parsed)
    normalized = {
        "caption": caption,
        "objects": [item.label for item in parsed.objects],
        "actions": [item.label for item in parsed.actions],
    }
    target_key = qwen_target_key(media_id=media_id, video_segment_id=video_segment_id)
    caption_stmt = insert(QwenCaption).values(
        target_key=target_key,
        media_id=media_id,
        video_segment_id=video_segment_id,
        caption=caption,
        prompt_version=prompt_version,
        model_version=model_version,
    )
    await session.execute(
        caption_stmt.on_conflict_do_update(
            constraint="uq_qwen_caption_target_prompt_model",
            set_={"caption": caption_stmt.excluded.caption, "updated_at": datetime.now(timezone.utc)},
        )
    )

    if video_segment_id is not None:
        await session.execute(
            delete(VideoSegmentLabel).where(
                VideoSegmentLabel.video_segment_id == video_segment_id,
                VideoSegmentLabel.prompt_version == prompt_version,
                VideoSegmentLabel.model_version == model_version,
            )
        )
        payload = [
            {
                "video_segment_id": video_segment_id,
                "canonical_label": str(row["canonical_label"])[:96],
                "category": str(row["category"])[:48],
                "confidence": float(row["confidence"]),
                "evidence_text": (
                    str(row["evidence_text"])[:240] if row.get("evidence_text") else None
                ),
                "prompt_version": prompt_version,
                "model_version": model_version,
            }
            for row in rows
            if row.get("canonical_label")
        ]
        if payload:
            await session.execute(insert(VideoSegmentLabel).values(payload))

    job.raw_response = {"text": raw_text}
    job.normalized_result = normalized
    job.status = "done"
    job.completed_at = datetime.now(timezone.utc)
    return job


async def mark_qwen_job_failed(
    session: AsyncSession,
    *,
    error: Exception | str,
    model_version: str,
    media_id: int | None = None,
    video_segment_id: int | None = None,
    prompt_version: str = QWEN_ENRICHMENT_PROMPT_VERSION,
) -> QwenEnrichmentJob:
    job = await ensure_qwen_job(
        session,
        media_id=media_id,
        video_segment_id=video_segment_id,
        prompt_version=prompt_version,
        model_version=model_version,
    )
    job.status = "pending" if job.attempts < 3 else "error"
    job.error_message = str(error)[:500]
    return job
