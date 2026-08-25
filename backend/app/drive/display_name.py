"""Display helpers for Drive files (index name vs raw Drive name)."""
from __future__ import annotations

from app.db.models import DriveFile


def drive_file_display_name(drive_file: DriveFile) -> str:
    return (drive_file.index_name or drive_file.name or "").strip()
