from __future__ import annotations

import logging

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db import models  # noqa: F401
from app.db.models import DriveFile, DriveFileStatus

logger = logging.getLogger(__name__)


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
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema verified")


async def recover_stuck_processing_files(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        result = await session.execute(
            update(DriveFile)
            .where(DriveFile.status == DriveFileStatus.PROCESSING)
            .values(status=DriveFileStatus.PENDING)
        )
        await session.commit()
        count = result.rowcount or 0
        if count:
            logger.warning("Reset %d file(s) stuck in processing state", count)
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
