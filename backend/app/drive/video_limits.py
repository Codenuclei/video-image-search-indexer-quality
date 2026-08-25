"""Hard limits for video indexing (skip, never PROCESSED)."""

from __future__ import annotations

# Videos larger than this are marked SKIPPED with reason ``video_too_large``.
VIDEO_MAX_INDEX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
VIDEO_TOO_LARGE_PREFIX = "video_too_large"


def is_video_too_large(size: int | None) -> bool:
    return size is not None and int(size) > VIDEO_MAX_INDEX_BYTES


def video_too_large_message(size: int | None) -> str:
    n = int(size or 0)
    return f"{VIDEO_TOO_LARGE_PREFIX}: exceeds 10GB (size={n})"


def apply_video_too_large_skip(drive_file) -> bool:
    """If video is over the size cap, mark SKIPPED and return True."""
    from app.db.models import DriveFileStatus
    from app.pipelines.common import is_video_mime

    if not is_video_mime(getattr(drive_file, "mime_type", None)):
        return False
    if not is_video_too_large(getattr(drive_file, "size", None)):
        return False
    drive_file.status = DriveFileStatus.SKIPPED
    drive_file.error_message = video_too_large_message(drive_file.size)
    return True
