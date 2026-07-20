from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config import Settings

if TYPE_CHECKING:
    from app.db.models import DriveFile

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _suffix_for_drive_file(drive_file: Any) -> str:
    """
    Pick a safe on-disk extension.

    YouTube titles often contain periods ("Ep. #5", "ft. …") — never treat the
    title as a filename extension or cache paths become unusable.
    """
    source = getattr(drive_file, "source", None) or "drive"
    file_id = getattr(drive_file, "id", "") or ""
    if source == "youtube" or str(file_id).startswith("yt:"):
        mime = (getattr(drive_file, "mime_type", None) or "").lower()
        if "mp4" in mime:
            return ".mp4"
        return ".webm"

    name = getattr(drive_file, "name", None) or ""
    match = _EXT_RE.search(name)
    if match:
        return match.group(0).lower()

    mime = (getattr(drive_file, "mime_type", None) or "").lower()
    if "mp4" in mime:
        return ".mp4"
    if "webm" in mime:
        return ".webm"
    return ".mp4"


def video_cache_path(settings: Settings, drive_file: "DriveFile") -> Path:
    """Persistent on-disk path for a video (Drive or YouTube local library)."""
    cache_dir = Path(settings.video_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{drive_file.id}{_suffix_for_drive_file(drive_file)}"
