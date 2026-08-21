"""Safe Drive source-cache cleanup (media_cache + videos).

Only deletes on-disk files for Drive rows that are PROCESSED and have a Postgres
Media row. Never deletes Media rows, thumbnails, youtube/upload sources, or
active/incomplete caches.
"""
from __future__ import annotations

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

logger = logging.getLogger(__name__)

DELETE_POLICY = "delete_processed_drive_with_media"


@dataclass(frozen=True)
class CacheDbState:
    file_id: str
    source: str
    status: str
    carousel_status: str
    has_media: bool


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
    if state.status == DriveFileStatus.PROCESSED.value and state.has_media:
        return AuditRow(
            path,
            size,
            state,
            DELETE_POLICY,
            "inactive processed Drive row has durable Media",
            root_label,
        )
    return AuditRow(
        path,
        size,
        state,
        "keep_incomplete_drive",
        "PROCESSED-without-Media / pending / error — cache kept for repair",
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
    return {
        drive_file.id: CacheDbState(
            file_id=drive_file.id,
            source=drive_file.source or "drive",
            status=(
                drive_file.status.value
                if hasattr(drive_file.status, "value")
                else str(drive_file.status)
            ),
            carousel_status=drive_file.carousel_status or "idle",
            has_media=media_id is not None,
        )
        for drive_file, media_id in rows
    }


async def load_cache_state(file_id: str) -> CacheDbState | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(DriveFile, Media.id)
                .outerjoin(Media, Media.drive_file_id == DriveFile.id)
                .where(DriveFile.id == file_id)
            )
        ).first()
    if row is None:
        return None
    drive_file, media_id = row
    return CacheDbState(
        file_id=drive_file.id,
        source=drive_file.source or "drive",
        status=(
            drive_file.status.value
            if hasattr(drive_file.status, "value")
            else str(drive_file.status)
        ),
        carousel_status=drive_file.carousel_status or "idle",
        has_media=media_id is not None,
    )


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
    """Dry-run or apply safe deletion across media_cache + videos."""
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
            await load_cache_state(file_id_from_cache_path(row.path)),
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
