from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db.models import DriveFile


def video_cache_path(settings: Settings, drive_file: DriveFile) -> Path:
    """Persistent on-disk path for a video (Drive or YouTube local library)."""
    cache_dir = Path(settings.video_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = ""
    if "." in drive_file.name:
        suffix = drive_file.name[drive_file.name.rindex(".") :]
    if not suffix:
        suffix = ".webm"
    return cache_dir / f"{drive_file.id}{suffix}"
