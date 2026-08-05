"""Same-content / same-name conflict detection and user resolutions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, FileIndexConflict
from app.drive.content_hash import (
    DUPLICATE_CONTENT_PREFIX,
    NAME_CONFLICT_PREFIX,
    content_identity_key,
)

logger = logging.getLogger(__name__)

KIND_SAME_CONTENT = "same_content"
KIND_SAME_CONTENT_DIFF_NAME = "same_content_diff_name"
KIND_SAME_NAME_DIFF_CONTENT = "same_name_diff_content"

STATUS_PENDING = "pending"
STATUS_AUTOSKIPPED = "autoskipped"
STATUS_SKIPPED = "skipped"
STATUS_REPLACED = "replaced"
STATUS_MERGED = "merged"


async def find_by_content_hash(
    session: AsyncSession,
    *,
    algo: str,
    digest: str,
    exclude_id: str,
) -> DriveFile | None:
    """Find another file already known with the same content hash."""
    row = (
        await session.execute(
            select(DriveFile)
            .where(
                DriveFile.content_hash == digest.strip().lower(),
                DriveFile.content_hash_algo == algo,
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
            )
            .order_by(DriveFile.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def find_by_same_name(
    session: AsyncSession,
    *,
    name: str,
    exclude_id: str,
) -> DriveFile | None:
    """Find another active (non-folder) file with the same name (case-insensitive)."""
    lowered = name.strip().lower()
    if not lowered:
        return None
    return (
        await session.execute(
            select(DriveFile)
            .where(
                func.lower(DriveFile.name) == lowered,
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                DriveFile.status.in_(
                    (
                        DriveFileStatus.PENDING,
                        DriveFileStatus.PROCESSING,
                        DriveFileStatus.PROCESSED,
                        DriveFileStatus.ERROR,
                    )
                ),
            )
            .order_by(DriveFile.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _upsert_conflict(
    session: AsyncSession,
    *,
    incoming: DriveFile,
    existing: DriveFile,
    kind: str,
    status: str,
    message: str | None = None,
) -> FileIndexConflict:
    prior = (
        await session.execute(
            select(FileIndexConflict)
            .where(
                FileIndexConflict.incoming_file_id == incoming.id,
                FileIndexConflict.existing_file_id == existing.id,
                FileIndexConflict.conflict_kind == kind,
                FileIndexConflict.status.in_([STATUS_PENDING, STATUS_AUTOSKIPPED]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior is not None:
        prior.status = status
        prior.incoming_name = incoming.name
        prior.existing_name = existing.name
        prior.content_hash = incoming.content_hash
        prior.message = message
        if status not in (STATUS_PENDING, STATUS_AUTOSKIPPED):
            prior.resolved_at = datetime.now(tz=timezone.utc)
        return prior

    row = FileIndexConflict(
        incoming_file_id=incoming.id,
        existing_file_id=existing.id,
        conflict_kind=kind,
        status=status,
        incoming_name=incoming.name,
        existing_name=existing.name,
        content_hash=incoming.content_hash,
        message=message,
        resolved_at=(
            datetime.now(tz=timezone.utc)
            if status not in (STATUS_PENDING, STATUS_AUTOSKIPPED)
            else None
        ),
    )
    session.add(row)
    await session.flush()
    return row


async def apply_dedupe_on_upsert(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    algo: str | None,
    digest: str | None,
) -> str | None:
    """
    Apply content / name conflict rules for a newly discovered or updated file.

    Returns a skip reason key when the file should not be indexed further:
    - ``duplicate_content`` → autoskip (identical bytes already indexed)
    - ``name_conflict`` → park pending Replace (same name, different content)
    - ``None`` → proceed with normal indexing
    """
    if algo and digest:
        drive_file.content_hash = digest.strip().lower()
        drive_file.content_hash_algo = algo

    # Same content → autoskip (do not reindex). Record mergeable conflict when names differ.
    if drive_file.content_hash and drive_file.content_hash_algo:
        twin = await find_by_content_hash(
            session,
            algo=drive_file.content_hash_algo,
            digest=drive_file.content_hash,
            exclude_id=drive_file.id,
        )
        if twin is not None:
            same_name = (twin.name or "").strip().lower() == (drive_file.name or "").strip().lower()
            kind = KIND_SAME_CONTENT if same_name else KIND_SAME_CONTENT_DIFF_NAME
            msg = (
                f"{DUPLICATE_CONTENT_PREFIX} identical to {twin.id}"
                + ("" if same_name else f" (also named {twin.name!r})")
            )
            drive_file.status = DriveFileStatus.SKIPPED
            drive_file.error_message = msg
            await _upsert_conflict(
                session,
                incoming=drive_file,
                existing=twin,
                kind=kind,
                status=STATUS_AUTOSKIPPED,
                message=msg,
            )
            logger.info(
                "Autoskip duplicate content %s → matches %s (%s)",
                drive_file.id[:12],
                twin.id[:12],
                kind,
            )
            return "duplicate_content"

    # Same name, different (or unknown) content → do not silently overwrite; await Replace.
    name_twin = await find_by_same_name(session, name=drive_file.name, exclude_id=drive_file.id)
    if name_twin is not None:
        twin_key = content_identity_key(name_twin.content_hash_algo, name_twin.content_hash)
        ours = content_identity_key(drive_file.content_hash_algo, drive_file.content_hash)
        if twin_key and ours and twin_key == ours:
            # Same content already handled above; nothing else.
            return None
        # Different content (or one side missing hash): require explicit Replace.
        msg = (
            f"{NAME_CONFLICT_PREFIX} same name as {name_twin.id} "
            f"({name_twin.name!r}); awaiting replace/skip"
        )
        drive_file.status = DriveFileStatus.SKIPPED
        drive_file.error_message = msg
        await _upsert_conflict(
            session,
            incoming=drive_file,
            existing=name_twin,
            kind=KIND_SAME_NAME_DIFF_CONTENT,
            status=STATUS_PENDING,
            message=msg,
        )
        logger.info(
            "Name conflict parked %s vs %s",
            drive_file.id[:12],
            name_twin.id[:12],
        )
        return "name_conflict"

    return None


async def list_conflicts(
    session: AsyncSession,
    *,
    status: str | None = "pending",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[FileIndexConflict], int]:
    q = select(FileIndexConflict)
    count_q = select(func.count()).select_from(FileIndexConflict)
    if status:
        q = q.where(FileIndexConflict.status == status)
        count_q = count_q.where(FileIndexConflict.status == status)
    total = int((await session.execute(count_q)).scalar_one())
    rows = list(
        (
            await session.execute(
                q.order_by(FileIndexConflict.created_at.desc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return rows, total


async def resolve_conflict(
    session: AsyncSession,
    conflict_id: int,
    *,
    action: str,
) -> FileIndexConflict:
    """
    Apply user choice:
    - skip: keep incoming skipped
    - replace: drop existing index ownership, requeue incoming
    - merge: keep existing index; mark incoming as merged duplicate
    """
    from app.drive.cleanup import remove_drive_file
    from app.gemini.service import get_gemini_service

    conflict = await session.get(FileIndexConflict, conflict_id)
    if conflict is None:
        raise ValueError("conflict not found")
    if conflict.status not in (STATUS_PENDING, STATUS_AUTOSKIPPED):
        raise ValueError(f"conflict already resolved as {conflict.status}")

    action = action.strip().lower()
    incoming = await session.get(DriveFile, conflict.incoming_file_id)
    existing = await session.get(DriveFile, conflict.existing_file_id)
    now = datetime.now(tz=timezone.utc)

    if action == "skip":
        if incoming is not None:
            incoming.status = DriveFileStatus.SKIPPED
            if not (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX) and not (
                incoming.error_message or ""
            ).startswith(NAME_CONFLICT_PREFIX):
                incoming.error_message = f"{NAME_CONFLICT_PREFIX} skipped by user vs {conflict.existing_file_id}"
        conflict.status = STATUS_SKIPPED
        conflict.resolved_at = now
        return conflict

    if action == "merge":
        # Keep existing indexed media; incoming stays skipped as a known duplicate/alias.
        if incoming is not None:
            incoming.status = DriveFileStatus.SKIPPED
            incoming.error_message = (
                f"{DUPLICATE_CONTENT_PREFIX} merged with {conflict.existing_file_id}"
            )
        conflict.status = STATUS_MERGED
        conflict.resolved_at = now
        conflict.conflict_kind = (
            KIND_SAME_CONTENT_DIFF_NAME
            if (incoming and existing and incoming.name != existing.name)
            else conflict.conflict_kind
        )
        return conflict

    if action == "replace":
        # Remove existing indexed artifacts, then requeue incoming.
        if existing is not None:
            from sqlalchemy.orm import selectinload

            existing = (
                await session.execute(
                    select(DriveFile)
                    .where(DriveFile.id == conflict.existing_file_id)
                    .options(selectinload(DriveFile.media))
                )
            ).scalar_one_or_none()
            if existing is not None:
                gemini = get_gemini_service()
                await remove_drive_file(session, existing, gemini=gemini)
        if incoming is not None:
            incoming.status = DriveFileStatus.PENDING
            incoming.error_message = None
            incoming.decode_attempts = 0
        conflict.status = STATUS_REPLACED
        conflict.resolved_at = now
        return conflict

    raise ValueError(f"unknown action: {action}")
