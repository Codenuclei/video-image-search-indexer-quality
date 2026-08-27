"""Same-content / same-name conflict detection and user resolutions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DriveFile,
    DriveFileStatus,
    FaceJob,
    FaceJobStatus,
    FileIndexConflict,
    Media,
)
from app.drive.content_hash import (
    DUPLICATE_CONTENT_PREFIX,
    NAME_CONFLICT_PREFIX,
    content_identity_key,
)
from app.drive.disambiguate import allocate_index_name, effective_index_name, find_by_effective_name
from app.drive.perceptual_hash import hamming_hex

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
    """Find another file whose identical bytes are already claimed or indexed.

    Prefers PROCESSED, then PROCESSING. PENDING peers are ignored here — use
    ``find_older_pending_same_hash`` at claim time so only one PENDING runner
    starts the pipeline for a given hash.
    """
    row = (
        await session.execute(
            select(DriveFile)
            .where(
                DriveFile.content_hash == digest.strip().lower(),
                DriveFile.content_hash_algo == algo,
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                # A processed duplicate pointer is not itself a canonical
                # searchable copy. Allowing it as a twin lets two concurrent
                # copies point at each other and leaves both without faces.
                or_(
                    DriveFile.error_message.is_(None),
                    DriveFile.error_message.not_like(f"{DUPLICATE_CONTENT_PREFIX}%"),
                ),
                DriveFile.status.in_(
                    (
                        DriveFileStatus.PROCESSING,
                        DriveFileStatus.PROCESSED,
                    )
                ),
            )
            .order_by(
                # Prefer a finished twin so autoskip means "already indexed".
                case(
                    (DriveFile.status == DriveFileStatus.PROCESSED, 0),
                    else_=1,
                ),
                DriveFile.created_at.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def find_older_pending_same_hash(
    session: AsyncSession,
    *,
    algo: str,
    digest: str,
    exclude_id: str,
    created_at,
) -> DriveFile | None:
    """Older PENDING twin with the same content — let that row own the pipeline."""
    if created_at is None:
        return None
    return (
        await session.execute(
            select(DriveFile)
            .where(
                DriveFile.content_hash == digest.strip().lower(),
                DriveFile.content_hash_algo == algo,
                DriveFile.id != exclude_id,
                DriveFile.status == DriveFileStatus.PENDING,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                DriveFile.created_at < created_at,
            )
            .order_by(DriveFile.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def find_by_same_name(
    session: AsyncSession,
    *,
    name: str,
    exclude_id: str,
) -> DriveFile | None:
    """Backward-compatible alias for effective index name lookup."""
    return await find_by_effective_name(session, name=name, exclude_id=exclude_id)


async def find_by_visual_hash_exact(
    session: AsyncSession,
    *,
    visual_hash: str,
    exclude_id: str,
) -> DriveFile | None:
    vh = visual_hash.strip().lower()
    if len(vh) != 16:
        return None
    return (
        await session.execute(
            select(DriveFile)
            .where(
                DriveFile.visual_hash == vh,
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                or_(
                    DriveFile.error_message.is_(None),
                    DriveFile.error_message.not_like(f"{DUPLICATE_CONTENT_PREFIX}%"),
                ),
                DriveFile.status.in_(
                    (
                        DriveFileStatus.PROCESSING,
                        DriveFileStatus.PROCESSED,
                    )
                ),
                DriveFile.mime_type.like("image/%"),
            )
            .order_by(
                case(
                    (DriveFile.status == DriveFileStatus.PROCESSED, 0),
                    else_=1,
                ),
                DriveFile.created_at.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def find_visual_twin(
    session: AsyncSession,
    *,
    visual_hash: str,
    exclude_id: str,
    max_hamming: int = 5,
    basename: str | None = None,
) -> DriveFile | None:
    """Find a processed twin with exact or near-identical dHash.

    Near-match Hamming scans only rows sharing the same Drive basename so
    indexing stays O(peers) instead of scanning the whole library.
    """
    twin = await find_by_visual_hash_exact(
        session, visual_hash=visual_hash, exclude_id=exclude_id
    )
    if twin is not None:
        return twin
    if max_hamming <= 0:
        return None

    vh = visual_hash.strip().lower()
    if len(vh) != 16:
        return None

    lowered_name = (basename or "").strip().lower()
    if not lowered_name:
        return None

    rows = (
        await session.execute(
            select(DriveFile.id, DriveFile.visual_hash)
            .where(
                DriveFile.visual_hash.isnot(None),
                DriveFile.id != exclude_id,
                DriveFile.error_message.is_distinct_from("folder_marker"),
                DriveFile.status.in_(
                    (
                        DriveFileStatus.PROCESSING,
                        DriveFileStatus.PROCESSED,
                    )
                ),
                DriveFile.mime_type.like("image/%"),
                func.lower(DriveFile.name) == lowered_name,
            )
        )
    ).all()
    best_id: str | None = None
    best_dist = max_hamming + 1
    for row_id, row_hash in rows:
        if not row_hash:
            continue
        dist = hamming_hex(vh, row_hash)
        if dist <= max_hamming and dist < best_dist:
            best_id = row_id
            best_dist = dist
    if best_id is None:
        return None
    return await session.get(DriveFile, best_id)


async def apply_visual_dedupe_on_image(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    max_hamming: int = 5,
) -> str | None:
    """Mark visually identical images as PROCESSED without re-indexing."""
    if drive_file.status == DriveFileStatus.PROCESSED:
        return None
    if not (drive_file.mime_type or "").startswith("image/"):
        return None
    if not drive_file.visual_hash:
        return None

    twin = await find_visual_twin(
        session,
        visual_hash=drive_file.visual_hash,
        exclude_id=drive_file.id,
        max_hamming=max_hamming,
        basename=drive_file.name,
    )
    if twin is None:
        return None

    same_name = effective_index_name(twin).lower() == effective_index_name(drive_file).lower()
    kind = KIND_SAME_CONTENT if same_name else KIND_SAME_CONTENT_DIFF_NAME
    msg = (
        f"{DUPLICATE_CONTENT_PREFIX} visually identical to {twin.id}"
        + ("" if same_name else f" (also named {twin.display_name!r})")
    )
    drive_file.status = DriveFileStatus.PROCESSED
    drive_file.error_message = msg
    await _upsert_conflict(
        session,
        incoming=drive_file,
        existing=twin,
        kind=kind,
        status=STATUS_MERGED,
        message=msg,
    )
    logger.info(
        "Complete visual duplicate %s → matches %s (hamming≤%d)",
        drive_file.id[:12],
        twin.id[:12],
        max_hamming,
    )
    return "duplicate_content"


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


def is_duplicate_content_complete(drive_file: DriveFile | None) -> bool:
    """True when this row is a content-twin of an already-indexed file (not a real skip)."""
    if drive_file is None:
        return False
    return (drive_file.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)


async def promote_duplicate_content_skips(session: AsyncSession) -> int:
    """Migrate legacy SKIPPED duplicate_content rows → PROCESSED (already indexed)."""
    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status == DriveFileStatus.SKIPPED,
                    DriveFile.error_message.like(f"{DUPLICATE_CONTENT_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = DriveFileStatus.PROCESSED
    if rows:
        await session.flush()
        logger.info("Promoted %d duplicate_content SKIPPED → PROCESSED", len(rows))
    return len(rows)


async def requeue_circular_duplicate_canonicals(
    session: AsyncSession,
    *,
    limit: int = 250,
) -> list[str]:
    """Backfill one real index owner for duplicate-only content groups.

    Older concurrent indexing runs could mark every copy PROCESSED while making
    each row point at another duplicate. Those groups have captions but no
    canonical face/media owner. Keep every Drive row, but requeue one copy so
    normal indexing creates the shared searchable artifacts.
    """
    result = await session.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    candidate.id,
                    row_number() OVER (
                        PARTITION BY candidate.content_hash, candidate.content_hash_algo
                        ORDER BY candidate.created_at, candidate.id
                    ) AS group_rank
                FROM drive_files AS candidate
                WHERE candidate.status = 'PROCESSED'
                  AND candidate.mime_type LIKE 'image/%'
                  AND candidate.content_hash IS NOT NULL
                  AND candidate.content_hash_algo IS NOT NULL
                  AND candidate.error_message LIKE 'duplicate_content:%'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM drive_files AS canonical
                      WHERE canonical.id <> candidate.id
                        AND canonical.content_hash = candidate.content_hash
                        AND canonical.content_hash_algo = candidate.content_hash_algo
                        AND canonical.status IN ('PENDING', 'PROCESSING', 'PROCESSED')
                        AND COALESCE(canonical.error_message, '') NOT LIKE 'duplicate_content:%'
                  )
            ),
            selected AS (
                SELECT id
                FROM ranked
                WHERE group_rank = 1
                LIMIT :limit
            )
            UPDATE drive_files AS target
            SET status = 'PENDING',
                error_message = NULL,
                processing_started_at = NULL,
                completed_at = NULL
            FROM selected
            WHERE target.id = selected.id
            RETURNING target.id
            """
        ),
        {"limit": max(1, int(limit))},
    )
    ids = [str(row[0]) for row in result.fetchall()]
    # Also catch owners from an earlier repair that Qdrant recovery prematurely
    # finalized before a face job was created. A completed FaceJob is the durable
    # marker for a valid zero-face scan, so those rows are not retried forever.
    face_gap_result = await session.execute(
        text(
            """
            SELECT owner.id
            FROM drive_files AS owner
            WHERE owner.status = 'PROCESSED'
              AND owner.mime_type LIKE 'image/%'
              AND EXISTS (
                  SELECT 1 FROM media
                  WHERE media.drive_file_id = owner.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM media
                  JOIN faces ON faces.media_id = media.id
                  WHERE media.drive_file_id = owner.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM face_jobs
                  WHERE face_jobs.drive_file_id = owner.id
                    AND (
                        face_jobs.status IN ('PENDING', 'PROCESSING')
                        OR (
                            face_jobs.status = 'DONE'
                            AND face_jobs.scan_completed_at IS NOT NULL
                        )
                    )
              )
            ORDER BY owner.created_at, owner.id
            LIMIT :limit
            """
        ),
        {"limit": max(1, int(limit))},
    )
    ids = list(dict.fromkeys(ids + [str(row[0]) for row in face_gap_result.fetchall()]))
    if ids:
        media_ids = set(
            (
                await session.execute(
                    select(Media.drive_file_id).where(Media.drive_file_id.in_(ids))
                )
            ).scalars()
        )
        open_job_ids = set(
            (
                await session.execute(
                    select(FaceJob.drive_file_id).where(
                        FaceJob.drive_file_id.in_(media_ids),
                        FaceJob.status.in_(
                            (FaceJobStatus.PENDING, FaceJobStatus.PROCESSING)
                        ),
                    )
                )
            ).scalars()
        )
        direct_face_ids = media_ids - open_job_ids
        if direct_face_ids:
            await session.execute(
                update(DriveFile)
                .where(DriveFile.id.in_(direct_face_ids))
                .values(status=DriveFileStatus.PROCESSING)
            )
            session.add_all(
                [
                    FaceJob(
                        drive_file_id=drive_file_id,
                        status=FaceJobStatus.PENDING,
                        attempts=0,
                    )
                    for drive_file_id in direct_face_ids
                ]
            )
            await session.flush()
    if ids:
        logger.warning(
            "Requeued %d circular duplicate image group(s) for canonical face backfill "
            "(%d direct face job(s))",
            len(ids),
            len(direct_face_ids),
        )
    return ids


async def apply_dedupe_on_upsert(
    session: AsyncSession,
    drive_file: DriveFile,
    *,
    algo: str | None,
    digest: str | None,
) -> str | None:
    """
    Apply content / name conflict rules for a newly discovered or updated file.

    Returns a reason key when indexing should stop for this row:
    - ``duplicate_content`` → mark PROCESSED (identical bytes already indexed; not a skip)
    - ``None`` → proceed with normal indexing (name collisions are auto-disambiguated)
    """
    if algo and digest:
        drive_file.content_hash = digest.strip().lower()
        drive_file.content_hash_algo = algo

    # Never demote a finished index row — hash attach / re-sync must keep PROCESSED.
    if drive_file.status == DriveFileStatus.PROCESSED:
        return None

    # Same content → already search-ready via the twin. Mark complete, do not reindex.
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
            drive_file.status = DriveFileStatus.PROCESSED
            drive_file.error_message = msg
            await _upsert_conflict(
                session,
                incoming=drive_file,
                existing=twin,
                kind=kind,
                status=STATUS_MERGED,
                message=msg,
            )
            logger.info(
                "Complete duplicate content %s → matches %s (%s)",
                drive_file.id[:12],
                twin.id[:12],
                kind,
            )
            return "duplicate_content"

    # Same effective index name, different (or unknown) content → auto-rename like a file manager.
    incoming_name = effective_index_name(drive_file)
    name_twin = await find_by_effective_name(
        session, name=incoming_name, exclude_id=drive_file.id
    )
    if name_twin is not None:
        twin_key = content_identity_key(name_twin.content_hash_algo, name_twin.content_hash)
        ours = content_identity_key(drive_file.content_hash_algo, drive_file.content_hash)
        if twin_key and ours and twin_key == ours:
            return None
        new_name = await allocate_index_name(session, drive_file, desired=drive_file.name)
        drive_file.index_name = new_name
        logger.info(
            "Disambiguated index name %s → %s (was blocked by %s)",
            drive_file.name,
            new_name,
            name_twin.id[:12],
        )
        return None

    return None


async def reconcile_name_conflict_skips(session: AsyncSession) -> int:
    """Requeue legacy name_conflict skips with auto-disambiguated index names."""
    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.status == DriveFileStatus.SKIPPED,
                    DriveFile.error_message.like(f"{NAME_CONFLICT_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.index_name = await allocate_index_name(session, row, desired=row.name)
        row.status = DriveFileStatus.PENDING
        row.error_message = None
        row.decode_attempts = 0
    if rows:
        await session.flush()
        logger.info(
            "Requeued %d name_conflict skip(s) with disambiguated index names",
            len(rows),
        )
    return len(rows)


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
        # Align with autoskip: incoming is search-ready via the twin — no re-pipeline.
        if incoming is not None:
            twin_id = conflict.existing_file_id
            same_name = bool(
                existing
                and incoming
                and (incoming.name or "").strip().lower() == (existing.name or "").strip().lower()
            )
            incoming.status = DriveFileStatus.PROCESSED
            incoming.error_message = (
                f"{DUPLICATE_CONTENT_PREFIX} merged with {twin_id}"
                + ("" if same_name else f" (also named {existing.name!r})" if existing else "")
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
                await remove_drive_file(
                    session,
                    existing,
                    gemini=gemini,
                    reason="conflict replace (soft-archive existing)",
                )
        if incoming is not None:
            incoming.status = DriveFileStatus.PENDING
            incoming.error_message = None
            incoming.decode_attempts = 0
        conflict.status = STATUS_REPLACED
        conflict.resolved_at = now
        return conflict

    raise ValueError(f"unknown action: {action}")
