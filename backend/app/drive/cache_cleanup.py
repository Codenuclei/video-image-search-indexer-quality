"""Safe Drive source-cache cleanup (media_cache + videos).

Only deletes on-disk files for Drive rows that are already PROCESSED in Postgres
and have durable index artifacts:
- images: Media row + Qdrant embedding + valid caption
- videos: Media row (PROCESSED only — never delete while pending/processing)

Never deletes Media rows, thumbnails, youtube/upload sources, or active caches.
Never promotes rows to PROCESSED — wait for indexing/caption/embed to finish first.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus, Media
from app.db.session import get_session_factory
from app.pipelines.common import is_image_mime, is_video_mime

logger = logging.getLogger(__name__)

DELETE_POLICY = "delete_processed_drive_with_media"


@dataclass(frozen=True)
class CacheDbState:
    file_id: str
    source: str
    status: str
    carousel_status: str
    has_media: bool
    is_image: bool = False
    is_video: bool = False
    has_caption: bool = False
    has_embed: bool = False


@dataclass(frozen=True)
class AuditRow:
    path: Path
    size: int
    state: CacheDbState | None
    policy: str
    reason: str
    root: str

    @property
    def deletable(self) -> bool:
        return self.policy == DELETE_POLICY


def classify_cache_path(
    path: Path,
    state: CacheDbState | None,
    *,
    root_label: str = "",
) -> AuditRow:
    size = path.lstat().st_size
    if path.is_symlink():
        return AuditRow(path, size, state, "keep_unknown", "symlink is never deleted", root_label)
    if ".partial" in path.name:
        return AuditRow(
            path, size, state, "keep_partial", "partial file requires manual review", root_label
        )
    if state is None:
        return AuditRow(path, size, None, "keep_unknown", "no matching DB row", root_label)
    source = (state.source or "unknown").lower()
    if source in {"upload", "youtube"}:
        return AuditRow(
            path, size, state, f"keep_{source}", "source bytes are retained", root_label
        )
    if source != "drive":
        return AuditRow(
            path, size, state, "keep_unknown", f"unrecognized source {source!r}", root_label
        )
    if (
        state.status == DriveFileStatus.PROCESSING.value
        or state.carousel_status == "processing"
    ):
        return AuditRow(
            path, size, state, "keep_active", "index or carousel processing is active", root_label
        )
    # Only clean after PROCESSED is durable in Postgres — never while pending.
    if state.status != DriveFileStatus.PROCESSED.value or not state.has_media:
        return AuditRow(
            path,
            size,
            state,
            "keep_incomplete_drive",
            "wait for PROCESSED + Media before cache delete",
            root_label,
        )
    if state.is_image and not (state.has_caption and state.has_embed):
        return AuditRow(
            path,
            size,
            state,
            "keep_awaiting_search",
            "PROCESSED but waiting for caption+embed before cache delete",
            root_label,
        )
    return AuditRow(
        path,
        size,
        state,
        DELETE_POLICY,
        "PROCESSED Drive row has durable Media (+ caption/embed for images)",
        root_label,
    )


def file_id_from_cache_path(path: Path) -> str:
    return path.name.rsplit(".", 1)[0] if "." in path.name else path.name


async def load_cache_states() -> dict[str, CacheDbState]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(DriveFile, Media.id).outerjoin(
                    Media, Media.drive_file_id == DriveFile.id
                )
            )
        ).all()

    provisional: dict[str, CacheDbState] = {}
    image_ids: list[str] = []
    for drive_file, media_id in rows:
        status = (
            drive_file.status.value
            if hasattr(drive_file.status, "value")
            else str(drive_file.status)
        )
        is_image = is_image_mime(drive_file.mime_type, drive_file.name)
        is_video = is_video_mime(drive_file.mime_type)
        has_media = media_id is not None
        provisional[drive_file.id] = CacheDbState(
            file_id=drive_file.id,
            source=drive_file.source or "drive",
            status=status,
            carousel_status=drive_file.carousel_status or "idle",
            has_media=has_media,
            is_image=is_image,
            is_video=is_video,
        )
        if (
            is_image
            and has_media
            and status == DriveFileStatus.PROCESSED.value
        ):
            image_ids.append(drive_file.id)

    embedded: set[str] = set()
    captioned: set[str] = set()
    if image_ids:
        try:
            from app.qdrant.image_captions import valid_caption_ids_sync
            from app.qdrant.images import existing_image_ids_sync

            embedded, captioned = await asyncio.gather(
                asyncio.to_thread(existing_image_ids_sync, image_ids),
                asyncio.to_thread(valid_caption_ids_sync, image_ids),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "cache_cleanup Qdrant lookup failed — images kept until caption+embed known"
            )
            embedded, captioned = set(), set()

    return {
        fid: CacheDbState(
            file_id=state.file_id,
            source=state.source,
            status=state.status,
            carousel_status=state.carousel_status,
            has_media=state.has_media,
            is_image=state.is_image,
            is_video=state.is_video,
            has_caption=fid in captioned,
            has_embed=fid in embedded,
        )
        for fid, state in provisional.items()
    }


async def load_cache_state(file_id: str) -> CacheDbState | None:
    """Re-check one file before unlink (full scan — safe, infrequent)."""
    states = await load_cache_states()
    return states.get(file_id)


def _iter_cache_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() or p.is_symlink())


def audit_roots(
    roots: Iterable[tuple[str, Path]],
    states: dict[str, CacheDbState],
) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for label, root in roots:
        for path in _iter_cache_files(root):
            rows.append(
                classify_cache_path(
                    path,
                    states.get(file_id_from_cache_path(path)),
                    root_label=label,
                )
            )
    return rows


def default_cache_roots() -> list[tuple[str, Path]]:
    settings = get_settings()
    return [
        ("media_cache", Path(settings.media_cache_dir)),
        ("videos", Path(settings.video_cache_dir)),
    ]


async def run_cache_cleanup(*, apply: bool = False) -> dict[str, Any]:
    """Dry-run or apply safe deletion across media_cache + videos.

    Only removes cache for rows already PROCESSED (images also need caption+embed).
    Does not change DriveFile status.
    """
    roots = default_cache_roots()
    states = await load_cache_states()
    rows = audit_roots(roots, states)

    counts = Counter(row.policy for row in rows)
    bytes_by_policy: Counter[str] = Counter()
    for row in rows:
        bytes_by_policy[row.policy] += row.size

    deletable = [r for r in rows if r.deletable]
    deletable_bytes = sum(r.size for r in deletable)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": not apply,
        "policy": DELETE_POLICY,
        "total_files": len(rows),
        "total_bytes": sum(r.size for r in rows),
        "deletable_count": len(deletable),
        "deletable_bytes": deletable_bytes,
        "by_policy": {
            policy: {"files": counts[policy], "bytes": bytes_by_policy[policy]}
            for policy in sorted(counts)
        },
        "roots": [
            {"name": label, "path": str(path.resolve()), "exists": path.is_dir()}
            for label, path in roots
        ],
        "deleted_count": 0,
        "deleted_bytes": 0,
        "refused": [],
    }

    if not apply:
        return result

    deleted_files = deleted_bytes = 0
    refused: list[dict[str, str]] = []
    root_set = {path.resolve() for _, path in roots if path.is_dir()}

    for row in deletable:
        resolved = row.path.resolve()
        if resolved.parent not in root_set or row.path.is_symlink():
            refused.append({"path": str(row.path), "reason": "escaped_root_or_symlink"})
            continue
        current = classify_cache_path(
            row.path,
            states.get(file_id_from_cache_path(row.path)),
            root_label=row.root,
        )
        if not current.deletable:
            refused.append({"path": str(row.path), "reason": "policy_changed"})
            continue
        try:
            row.path.unlink()
        except OSError as exc:
            refused.append({"path": str(row.path), "reason": f"unlink_failed:{exc}"})
            continue
        deleted_files += 1
        deleted_bytes += row.size

    result["deleted_count"] = deleted_files
    result["deleted_bytes"] = deleted_bytes
    result["refused"] = refused[:50]
    logger.info(
        "cache_cleanup apply deleted_files=%d deleted_bytes=%d refused=%d",
        deleted_files,
        deleted_bytes,
        len(refused),
    )
    return result
