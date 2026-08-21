"""Idempotent path rewrite: prefix active Drive root with connected folder name.

New syncs already emit ``/{selected_folder_name}/…`` from Drive listing.
This only rewrites ``DriveFile.path`` for the active ``root_folder_id``.

Does not rewrite folder_contexts, pauses, or Qdrant — those need an explicit
verified migration once path usage is confirmed in prod.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveUser
from app.drive.indexing_pause import normalize_file_path, normalize_folder_path

logger = logging.getLogger(__name__)


def connected_folder_prefix(folder_name: str | None) -> str | None:
    name = (folder_name or "").strip().strip("/")
    if not name:
        return None
    return normalize_folder_path("/" + name)


def path_needs_prefix(path: str | None, prefix: str) -> bool:
    fp = normalize_file_path(path or "")
    return not (fp == prefix or fp.startswith(prefix + "/"))


def apply_path_prefix(path: str | None, prefix: str) -> str:
    fp = normalize_file_path(path or "")
    if fp == "/":
        return prefix
    if fp.startswith(prefix + "/") or fp == prefix:
        return fp
    rel = fp.lstrip("/")
    return normalize_file_path(f"{prefix}/{rel}") if rel else prefix


async def migrate_active_root_path_prefix(session: AsyncSession) -> dict[str, Any]:
    """Rewrite DriveFile.path for the active Drive root only."""
    user = (
        await session.execute(select(DriveUser).order_by(DriveUser.id).limit(1))
    ).scalar_one_or_none()
    if user is None or not user.selected_folder_id:
        return {"ok": True, "skipped": True, "reason": "no_active_folder"}

    prefix = connected_folder_prefix(user.selected_folder_name)
    if prefix is None:
        return {"ok": True, "skipped": True, "reason": "no_folder_name"}

    active_id = user.selected_folder_id
    files = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.root_folder_id == active_id,
                    DriveFile.source == "drive",
                )
            )
        ).scalars().all()
    )

    rewritten_files = 0
    for row in files:
        if not path_needs_prefix(row.path, prefix):
            continue
        row.path = apply_path_prefix(row.path, prefix)
        rewritten_files += 1

    if rewritten_files:
        await session.flush()
        try:
            from app.drive.library_shell_cache import get_library_shell_cache

            get_library_shell_cache().invalidate()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "path_prefix_migrate prefix=%s files=%d",
            prefix,
            rewritten_files,
        )

    return {
        "ok": True,
        "skipped": False,
        "prefix": prefix,
        "root_folder_id": active_id,
        "rewritten_files": rewritten_files,
    }
