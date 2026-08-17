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
from app.pipelines.common import download_to_temp_file, svg_file_complete
from app.pipelines.image_formats import is_svg_filename
from app.storage import ensure_disk_space

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


def cache_is_incomplete(path: Path, drive_file: "DriveFile") -> bool:
    """True when the on-disk copy is shorter than Drive metadata or a truncated SVG."""
    try:
        actual = path.stat().st_size
    except OSError:
        return True
    if actual <= 0:
        return True
    expected = getattr(drive_file, "size", None)
    if isinstance(expected, int) and expected > 0 and actual < expected:
        return True
    name = getattr(drive_file, "name", None) or ""
    if is_svg_filename(name) and not svg_file_complete(path):
        return True
    return False


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
    if existing is not None and cache_is_incomplete(existing, drive_file):
        logger.warning(
            "Incomplete media cache for %s (%s bytes, expected %s) — re-downloading",
            getattr(drive_file, "id", ""),
            existing.stat().st_size,
            getattr(drive_file, "size", None),
        )
        try:
            existing.unlink(missing_ok=True)
        except OSError:
            pass
        drive_file.cache_rel_path = None
        existing = None
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
    expected_size = getattr(drive_file, "size", None)
    ensure_disk_space(dest, expected_size or 0)
    try:
        async with download_to_temp_file(
            client,
            drive_file.id,
            settings,
            suffix=suffix,
            expected_size=expected_size,
        ) as tmp:
            # Stage onto same filesystem as dest, then atomically publish.
            shutil.move(tmp, partial)
            os.replace(partial, dest)
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)

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


def _is_drive_sourced(drive_file: "DriveFile") -> bool:
    source = (getattr(drive_file, "source", None) or "drive").strip().lower()
    file_id = str(getattr(drive_file, "id", "") or "")
    if source in {"upload", "youtube"}:
        return False
    if file_id.startswith("yt:") or file_id.startswith("upload:"):
        return False
    return True


def unlink_drive_source_cache(
    drive_file: "DriveFile",
    settings: Settings | None = None,
    *,
    clear_rel_path: bool = True,
) -> bool:
    """Remove Drive download leftovers after Media exists or on ERROR.

    Never deletes upload/YouTube source bytes (irreplaceable). Clears
    ``cache_rel_path`` when a file was removed so the next index re-downloads.
    """
    settings = settings or get_settings()
    if not _is_drive_sourced(drive_file):
        return False

    removed = False
    candidates: list[Path] = []

    resolved = resolve_cache_path(settings, drive_file)
    if resolved is not None:
        candidates.append(resolved)

    # Video pipeline stores under video_cache_dir (often only the basename in cache_rel_path).
    try:
        from app.video.youtube_cache import video_cache_path

        video_path = video_cache_path(settings, drive_file)
        if video_path.is_file() and video_path.stat().st_size > 0:
            candidates.append(video_path)
    except Exception:  # noqa: BLE001
        pass

    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            path.unlink(missing_ok=True)
            removed = True
            logger.info(
                "unlink_drive_cache file_id=%s path=%s",
                getattr(drive_file, "id", "")[:12],
                path,
            )
        except OSError as exc:
            logger.warning(
                "unlink_drive_cache_failed file_id=%s path=%s err=%s",
                getattr(drive_file, "id", "")[:12],
                path,
                exc,
            )

    # Drop orphaned *.partial siblings next to known destinations.
    for path in list(candidates):
        partial = Path(f"{path}.partial")
        try:
            if partial.exists():
                partial.unlink(missing_ok=True)
                removed = True
        except OSError:
            pass

    if clear_rel_path and removed:
        drive_file.cache_rel_path = None
    return removed


async def unlink_drive_caches_for_ids(
    session: "AsyncSession",
    file_ids: list[str],
    settings: Settings | None = None,
) -> int:
    """Load DriveFile rows and unlink Drive-sourced caches. Returns unlink count."""
    from sqlalchemy import select

    from app.db.models import DriveFile

    settings = settings or get_settings()
    ids = [fid for fid in file_ids if fid]
    if not ids:
        return 0
    try:
        result = await session.execute(select(DriveFile).where(DriveFile.id.in_(ids)))
        rows = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        logger.exception("unlink_drive_caches_for_ids_lookup_failed count=%d", len(ids))
        return 0
    n = 0
    for row in rows:
        try:
            if unlink_drive_source_cache(row, settings):
                n += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "unlink_drive_cache_row_failed file_id=%s",
                getattr(row, "id", "")[:12],
            )
    return n
