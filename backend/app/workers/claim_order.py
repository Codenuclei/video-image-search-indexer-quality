"""Pending-file claim ordering helpers for the indexing worker."""
from __future__ import annotations

from sqlalchemy import ColumnElement

from app.config import Settings
from app.db.models import DriveFile


def pending_order_by(settings: Settings) -> tuple[ColumnElement, ...]:
    """Smallest files first when enabled; otherwise newest-first."""
    if settings.index_prefer_small_files:
        return (
            DriveFile.size.asc().nulls_last(),
            DriveFile.modified_time.desc().nulls_last(),
            DriveFile.name,
        )
    return (
        DriveFile.modified_time.desc().nulls_last(),
        DriveFile.name,
    )


def claim_window(settings: Settings, slots: int) -> int:
    """How many PENDING rows to scan when filling free slots."""
    mult = max(4, int(settings.index_claim_window_multiplier or 40))
    return max(slots * mult, 50)
