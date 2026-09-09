"""Load OCR spans for brand association (testv1). Never blocks search on Drive I/O."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Media, MediaOcrSpan
from app.db.session import get_session_factory
from app.ocr.rapidocr_engine import OCR_MODEL_VERSION, normalize_ocr_text
from app.workers.ocr_queue import enqueue_ocr_job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrSpanView:
    text: str
    normalized_text: str
    confidence: float
    region: str
    x: float
    y: float
    w: float
    h: float


async def load_ocr_spans_by_drive_ids(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    model_version: str = OCR_MODEL_VERSION,
) -> dict[str, list[OcrSpanView]]:
    if not drive_file_ids:
        return {}
    rows = (
        await session.execute(
            select(Media.drive_file_id, MediaOcrSpan)
            .join(Media, Media.id == MediaOcrSpan.media_id)
            .where(
                Media.drive_file_id.in_(drive_file_ids),
                MediaOcrSpan.model_version == model_version,
            )
        )
    ).all()
    out: dict[str, list[OcrSpanView]] = {}
    for drive_file_id, span in rows:
        out.setdefault(drive_file_id, []).append(
            OcrSpanView(
                text=span.text,
                normalized_text=span.normalized_text or normalize_ocr_text(span.text),
                confidence=float(span.confidence or 0.0),
                region=span.region or "other",
                x=float(span.bbox_x),
                y=float(span.bbox_y),
                w=float(span.bbox_width),
                h=float(span.bbox_height),
            )
        )
    return out


async def ensure_ocr_subset_for_search(
    session: AsyncSession,
    drive_file_ids: list[str],
    *,
    limit: int = 24,
    allow_download: bool = False,
) -> dict[str, list[OcrSpanView]]:
    """
    Read OCR spans for a candidate subset. Missing IDs are enqueued for the
    idle face-worker OCR lane (separate session). Search never downloads.
    ``allow_download`` is ignored (kept for call-site compat) — downloads stay
    off the request path so /search and /search/testv1 cannot stall or kill
    the request DB connection.
    """
    del allow_download  # unused on purpose
    ids = [fid for fid in drive_file_ids if fid][: max(0, limit)]
    if not ids:
        return {}

    existing = await load_ocr_spans_by_drive_ids(session, ids)
    missing = [fid for fid in ids if fid not in existing]
    if missing:
        try:
            async with get_session_factory()() as ocr_session:
                for fid in missing:
                    await enqueue_ocr_job(ocr_session, fid)
                await ocr_session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("ocr enqueue failed", exc_info=True)
    return existing
