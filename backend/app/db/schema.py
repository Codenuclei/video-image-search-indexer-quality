from __future__ import annotations

import logging

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db import models  # noqa: F401
from app.db.models import DriveFile, DriveFileStatus

logger = logging.getLogger(__name__)


async def _ensure_enum_label(conn, type_name: str, label: str) -> None:
    """Add a Postgres enum label if missing (safe, non-destructive).

    Checks ``pg_enum`` first so we never rely solely on IF NOT EXISTS support,
    then runs ``ALTER TYPE … ADD VALUE IF NOT EXISTS``.
    """
    if not type_name.replace("_", "").isalnum() or not label.replace("_", "").isalnum():
        raise ValueError(f"refusing unsafe enum identifiers: {type_name!r} {label!r}")
    exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM pg_type t
            JOIN pg_enum e ON t.oid = e.enumtypid
            WHERE t.typname = :type_name AND e.enumlabel = :label
            """
        ),
        {"type_name": type_name, "label": label},
    )
    if exists:
        return
    await conn.execute(
        text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")
    )
    logger.info("Added Postgres enum label %s.%s", type_name, label)


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create pgvector extension and tables if missing (idempotent)."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS gemini_document_name VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'drive'")
        )
        await conn.execute(
            text(
                "ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_status "
                "VARCHAR(24) NOT NULL DEFAULT 'idle'"
            )
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_lock_token VARCHAR(64)")
        )
        await conn.execute(
            text(
                "ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_lock_input_hash VARCHAR(64)"
            )
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_locked_at TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_error TEXT")
        )
        await conn.execute(
            text(
                "ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS carousel_attempts "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_drive_files_carousel_status "
                "ON drive_files (carousel_status)"
            )
        )
        await conn.execute(
            text("ALTER TABLE persons ADD COLUMN IF NOT EXISTS role VARCHAR(32)")
        )
        await conn.execute(
            text(
                "ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS decode_attempts "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS follow_shortcut_folders "
                "BOOLEAN NOT NULL DEFAULT true"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS experimental_manual_face_tag "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS reindex_errored_files "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS reindex_skipped_files "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS go_indexer_enabled "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS carousel_llm_provider "
                "VARCHAR NOT NULL DEFAULT 'auto'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS openrouter_model "
                "VARCHAR NOT NULL DEFAULT 'anthropic/claude-sonnet-4'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS claude_model "
                "VARCHAR NOT NULL DEFAULT 'claude-sonnet-4-5-20250929'"
            )
        )
        # Carousel generation saves: themes vs topics/hooks + cache keys
        await conn.execute(
            text(
                "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS kind "
                "VARCHAR(32) NOT NULL DEFAULT 'topics_hooks'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS model "
                "VARCHAR(128)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS transcript_hash "
                "VARCHAR(64)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS source "
                "VARCHAR(32)"
            )
        )
        await conn.execute(text(
            "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS status "
            "VARCHAR(24) NOT NULL DEFAULT 'ready'"
        ))
        await conn.execute(text(
            "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS input_hash "
            "VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS layout_mode "
            "VARCHAR(32) NOT NULL DEFAULT 'single_1'"
        ))
        await conn.execute(text(
            "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS copy_version "
            "INTEGER NOT NULL DEFAULT 1"
        ))
        await conn.execute(text(
            "ALTER TABLE carousel_generation_saves ADD COLUMN IF NOT EXISTS algorithm_version "
            "VARCHAR(32) NOT NULL DEFAULT 'p0'"
        ))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_carousel_generation_saves_kind "
                "ON carousel_generation_saves (kind)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_carousel_generation_saves_transcript_hash "
                "ON carousel_generation_saves (transcript_hash)"
            )
        )
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_carousel_generation_saves_input_hash "
            "ON carousel_generation_saves (input_hash)"
        ))
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_carousel_gen_saves_drive_kind_created "
                "ON carousel_generation_saves (drive_file_id, kind, created_at DESC)"
            )
        )
        # Content-hash dedupe + durable media cache paths (additive).
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)")
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS content_hash_algo VARCHAR(16)")
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS cache_rel_path VARCHAR")
        )
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS root_folder_id VARCHAR")
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_drive_files_content_hash "
                "ON drive_files (content_hash)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_drive_files_root_folder_id "
                "ON drive_files (root_folder_id)"
            )
        )
        # Transcript language for English-ensure / non-English purge.
        await conn.execute(
            text("ALTER TABLE video_segments ADD COLUMN IF NOT EXISTS language VARCHAR(16)")
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_video_segments_language "
                "ON video_segments (language)"
            )
        )
        # Soft-archive: never-delete policy for indexed artifacts.
        # Live PG enums use SQLAlchemy *member names* (PENDING, SKIPPED, …).
        # ADD 'ARCHIVED' (uppercase). A prior mistaken 'archived' label may also
        # exist — leave it; code binds ARCHIVED via the Python enum member name.
        await _ensure_enum_label(conn, "drive_file_status", "ARCHIVED")
        await conn.execute(
            text("ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_drive_files_archived_at "
                "ON drive_files (archived_at)"
            )
        )
        # Durable folder history (survives disconnect / folder switch).
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS indexed_folders (
                    id VARCHAR NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    drive_url TEXT NOT NULL,
                    drive_user_id VARCHAR,
                    drive_user_email VARCHAR,
                    is_active BOOLEAN NOT NULL DEFAULT false,
                    first_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_file_count INTEGER
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_indexed_folders_drive_user_id "
                "ON indexed_folders (drive_user_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_indexed_folders_is_active "
                "ON indexed_folders (is_active)"
            )
        )
        # Durable InsightFace job queue (FOR UPDATE SKIP LOCKED claim).
        await conn.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE face_job_status AS ENUM (
                        'PENDING', 'PROCESSING', 'DONE', 'ERROR'
                    );
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS face_jobs (
                    id SERIAL PRIMARY KEY,
                    drive_file_id VARCHAR NOT NULL REFERENCES drive_files(id) ON DELETE CASCADE,
                    status face_job_status NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lock_token VARCHAR(64),
                    locked_at TIMESTAMPTZ,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_face_jobs_status_created "
                "ON face_jobs (status, created_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_face_jobs_drive_file_id "
                "ON face_jobs (drive_file_id)"
            )
        )
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema verified")


async def recover_stuck_processing_files(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Drain PROCESSING orphans without deleting media.

    Rows that already have Media → PROCESSED (add-if-missing resume),
    unless an open face_job is still pending/processing (multi-index path).
    Rows without Media → PENDING so face/embed can run.
    """
    from sqlalchemy import select

    from app.db.models import FaceJob, FaceJobStatus, Media

    async with session_factory() as session:
        stuck = (
            await session.execute(
                select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
            )
        ).scalars().all()
        if not stuck:
            return 0
        restored = 0
        requeued = 0
        face_waiting = 0
        for row in stuck:
            has_media = (
                await session.execute(
                    select(Media.id).where(Media.drive_file_id == row.id).limit(1)
                )
            ).scalar_one_or_none() is not None
            if has_media:
                open_face = (
                    await session.execute(
                        select(FaceJob.id)
                        .where(
                            FaceJob.drive_file_id == row.id,
                            FaceJob.status.in_(
                                (FaceJobStatus.PENDING, FaceJobStatus.PROCESSING)
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if open_face is not None:
                    face_waiting += 1
                    continue
                row.status = DriveFileStatus.PROCESSED
                row.error_message = None
                restored += 1
            else:
                row.status = DriveFileStatus.PENDING
                requeued += 1
        await session.commit()
        count = restored + requeued
        if count or face_waiting:
            logger.warning(
                "Drain stuck PROCESSING: restored=%d requeued=%d face_jobs_open=%d",
                restored,
                requeued,
                face_waiting,
            )
        return count


async def recover_aborted_transaction_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Re-queue ERROR files left by InFailedSQLTransactionError face-cluster fallout."""
    async with session_factory() as session:
        result = await session.execute(
            update(DriveFile)
            .where(
                DriveFile.status == DriveFileStatus.ERROR,
                DriveFile.error_message.ilike("%transaction aborted%"),
            )
            .values(status=DriveFileStatus.PENDING, error_message=None)
        )
        await session.commit()
        count = result.rowcount or 0
        if count:
            logger.warning(
                "Re-queued %d file(s) stuck on aborted face-cluster transactions",
                count,
            )
        return count
