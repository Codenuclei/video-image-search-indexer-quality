"""Durable media cache: download to temp, atomic move onto stable volume paths."""
from __future__ import annotations

import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from app.config import Settings, get_settings
from app.pipelines.common import download_to_temp_file

if TYPE_CHECKING:
    from app.db.models import DriveFile
    from app.drive.client import DriveConnectorClient

logger = logging.getLogger(__name__)

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_PARTIAL_SUFFIX = ".partial"


def _suffix_for_file(drive_file: "DriveFile") -> str:
    name = getattr(drive_file, "name", None) or ""
    match = _EXT_RE.search(name)
    if match:
        return match.group(0).lower()
    mime = (getattr(drive_file, "mime_type", None) or "").lower()
    if mime.startswith("image/"):
        subtype = mime.split("/", 1)[-1].split(";")[0].strip()
        if subtype in {"jpeg", "jpg", "png", "gif", "webp", "heic", "heif", "tiff", "bmp"}:
            return f".{subtype if subtype != 'jpeg' else 'jpg'}"
        return ".bin"
    if "pdf" in mime:
        return ".pdf"
    if "mp4" in mime:
        return ".mp4"
    if "webm" in mime:
        return ".webm"
    return ".bin"


def media_cache_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = Path(settings.media_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_cache_path(settings: Settings, drive_file: "DriveFile") -> Path:
    """Stable on-disk path owned by this Drive file id (no half-copied state)."""
    filename = f"{drive_file.id}{_suffix_for_file(drive_file)}"
    return media_cache_dir(settings) / filename


def cache_rel_path_for(settings: Settings, absolute: Path) -> str:
    """Store path relative to media_cache_dir when possible."""
    root = media_cache_dir(settings).resolve()
    try:
        return str(absolute.resolve().relative_to(root))
    except ValueError:
        return str(absolute)


def resolve_cache_path(settings: Settings, drive_file: "DriveFile") -> Path | None:
    """Return existing complete cache file, or None."""
    if drive_file.cache_rel_path:
        candidate = media_cache_dir(settings) / drive_file.cache_rel_path
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
        absolute = Path(drive_file.cache_rel_path)
        if absolute.is_file() and absolute.stat().st_size > 0:
            return absolute
    primary = media_cache_path(settings, drive_file)
    if primary.is_file() and primary.stat().st_size > 0:
        return primary
    return None


async def ensure_media_cached(
    client: "DriveConnectorClient",
    drive_file: "DriveFile",
    settings: Settings | None = None,
) -> Path:
    """
    Ensure Drive media bytes exist at a stable cache path.

    Pattern matches video caching: stream → temp → shutil.move (atomic on same FS).
    Never leaves a truncated final path: writes to ``*.partial`` then renames.
    """
    settings = settings or get_settings()
    existing = resolve_cache_path(settings, drive_file)
    if existing is not None:
        drive_file.cache_rel_path = cache_rel_path_for(settings, existing)
        return existing

    dest = media_cache_path(settings, drive_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + _PARTIAL_SUFFIX)
    if partial.exists():
        try:
            partial.unlink()
        except OSError:
            pass

    suffix = dest.suffix or _suffix_for_file(drive_file)
    async with download_to_temp_file(client, drive_file.id, settings, suffix=suffix) as tmp:
        # Stage onto same filesystem as dest, then atomic replace.
        shutil.move(tmp, partial)
        os.replace(partial, dest)

    drive_file.cache_rel_path = cache_rel_path_for(settings, dest)
    logger.info("Cached media %s → %s", drive_file.id[:12], dest)
    return dest


@asynccontextmanager
async def open_cached_or_download(
    client: "DriveConnectorClient",
    drive_file: "DriveFile",
    settings: Settings | None = None,
) -> AsyncIterator[Path]:
    """Yield a readable cache path (ensuring the copy exists). Does not delete after."""
    settings = settings or get_settings()
    path = await ensure_media_cached(client, drive_file, settings)
    yield path


def read_cached_bytes(path: Path) -> bytes:
    return path.read_bytes()
