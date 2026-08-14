"""Pending-file claim ordering helpers for the indexing worker."""
from __future__ import annotations

from sqlalchemy import ColumnElement, case

from app.config import Settings
from app.db.models import DriveFile


def pending_order_by(settings: Settings) -> tuple[ColumnElement, ...]:
    """Carousel studio sources first, then size/recency.

    Uploads and YouTube rows from the carousel /test|/carousel flow must not
    sit behind a large Drive backlog — claim them before ordinary Drive files.
    """
    # 0 = carousel fast-path sources, 1 = everything else.
    source_rank = case(
        (DriveFile.source.in_(("upload", "youtube")), 0),
        else_=1,
    ).asc()
    if settings.index_prefer_small_files:
        return (
            source_rank,
            DriveFile.size.asc().nulls_last(),
            DriveFile.modified_time.desc().nulls_last(),
            DriveFile.name,
        )
    return (
        source_rank,
        DriveFile.modified_time.desc().nulls_last(),
        DriveFile.name,
    )


def claim_window(settings: Settings, slots: int) -> int:
    """How many PENDING rows to scan when filling free slots."""
    mult = max(4, int(settings.index_claim_window_multiplier or 40))
    return max(slots * mult, 50)
