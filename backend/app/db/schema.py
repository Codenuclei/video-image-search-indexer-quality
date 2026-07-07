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
