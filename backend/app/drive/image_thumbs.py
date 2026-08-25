"""Compressed JPEG thumbs for image grids.

Grid UIs must not stream original Drive/cache bytes. Prefer an existing thumb
under thumbnail_dir/images/; otherwise build a small JPEG from local cache or
a one-shot Drive download. Clients only ever receive the compressed thumb.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.config import Settings, get_settings
from app.pipelines.common import open_image_rgb
from app.storage import ensure_disk_space

THUMB_MAX_EDGE = 480
JPEG_QUALITY = 72


def image_thumb_path(settings: Settings | None, drive_file_id: str) -> Path:
    settings = settings or get_settings()
    return Path(settings.thumbnail_dir) / "images" / f"{drive_file_id}.jpg"


def write_image_thumbnail(
    source: Path,
    drive_file_id: str,
    settings: Settings | None = None,
    file_name: str = "",
) -> Path:
    """Write a compressed JPEG thumb from a local original. Idempotent."""
    settings = settings or get_settings()
    dest = image_thumb_path(settings, drive_file_id)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    raw = source.read_bytes()
    img = open_image_rgb(raw, file_name=file_name or source.name)
    img.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    try:
        payload_estimate = THUMB_MAX_EDGE * THUMB_MAX_EDGE * 3
        ensure_disk_space(str(dest), payload_estimate)
        img.save(partial, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        partial.replace(dest)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)
    return dest
