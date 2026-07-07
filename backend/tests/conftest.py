from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401 - registers all tables on Base.metadata
from app.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://drivefaceindexer:drivefaceindexer@localhost:55432/drivefaceindexer_test",
)


def _postgres_available() -> bool:
    normalized = TEST_DATABASE_URL.replace("+asyncpg", "").replace("+psycopg", "")
    parsed = urlparse(normalized)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason=(
        "No reachable Postgres at TEST_DATABASE_URL "
        f"({TEST_DATABASE_URL}) — start pgvector/pgvector:pg16 to run integration tests."
    ),
)


@pytest_asyncio.fixture
async def db_session():
    """
    Fresh schema per test against a *real* Postgres+pgvector instance: creates every
    table, yields an AsyncSession, then drops everything so tests stay isolated.
    """
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        await session.close()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
