"""Build Drive file-list cache payloads from Postgres (multi-worker safe).

In-memory ``file_list_cache`` is per-process. Webhooks refresh whichever worker
receives the push and sync into ``drive_files``. API workers should prefer DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveUser


def _parent_id_from_path(path: str, folder_id: str | None) -> str:
    """Best-effort parent id for ConnectorFile shape (UI mostly uses path/name)."""
    if not path or "/" not in path:
        return folder_id or ""
    # Without a full parent map, surface the selected-folder id as parent.
    return folder_id or ""


async def snapshot_drive_files_from_db(session: AsyncSession) -> dict[str, Any]:
    """Serialize the synced Drive listing from Postgres (no Drive API call)."""
    user = (
        await session.execute(select(DriveUser).limit(1))
    ).scalar_one_or_none()

    folder_id = (user.selected_folder_id if user else None) or ""
    folder_name = (user.selected_folder_name if user else None) or ""
    folder = (
        {"id": folder_id, "name": folder_name or "(unknown)"}
        if folder_id
        else None
    )

    rows = list(
        (
            await session.execute(
                select(DriveFile)
                .where(DriveFile.source == "drive")
                .order_by(DriveFile.path)
            )
        ).scalars().all()
    )

    files_out: list[dict[str, Any]] = []
    for row in rows:
        mime = row.mime_type or "application/octet-stream"
        is_folder = mime == "application/vnd.google-apps.folder" or (
            (row.error_message or "") == "folder_marker"
        )
        files_out.append(
            {
                "id": row.id,
                "name": row.name,
                "mimeType": mime,
                "isFolder": is_folder,
                "size": str(row.size) if row.size is not None else None,
                "modifiedTime": (
                    row.modified_time.isoformat() if row.modified_time else None
                ),
                "parentId": _parent_id_from_path(row.path or "", folder_id or None),
                "path": row.path or row.name,
                "md5Checksum": (
                    row.content_hash
                    if row.content_hash_algo == "md5" and row.content_hash
                    else None
                ),
            }
        )

    latest = (
        await session.execute(
            select(func.max(DriveFile.last_synced_at)).where(DriveFile.source == "drive")
        )
    ).scalar_one_or_none()

    cached_at: datetime | None = latest
    if cached_at is None and rows:
        cached_at = datetime.now(tz=timezone.utc)

    return {
        "folder": folder,
        "files": files_out,
        "truncated": False,
        "count": len(files_out),
        "file_count": sum(1 for f in files_out if not f["isFolder"]),
        "folder_count": sum(1 for f in files_out if f["isFolder"]),
        "cached_at": cached_at.isoformat() if cached_at else None,
        "age_seconds": None,
        "source": "db",
        "from_memory": False,
        "from_db": True,
        "last_error": None,
        "refresh_in_flight": False,
    }
