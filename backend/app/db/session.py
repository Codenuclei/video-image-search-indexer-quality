import logging
import os
import time
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
logger = logging.getLogger(__name__)


def _normalize_db_url(url: str) -> str:
    """
    Railway's managed Postgres gives a plain postgresql:// URL.
    SQLAlchemy async requires postgresql+asyncpg://.
    Also handle postgres:// alias from some providers.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        db_url = _normalize_db_url(settings.database_url)
        service_name = os.getenv("RAILWAY_SERVICE_NAME", "dfi-local")
        application_name = "".join(
            char if char.isalnum() or char in "._-" else "-"
            for char in service_name
        )[:63]
        _engine = create_async_engine(
            db_url,
            pool_pre_ping=True,
            future=True,
            pool_size=max(1, settings.db_pool_size),
            max_overflow=max(0, settings.db_max_overflow),
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=300,
            pool_use_lifo=True,
            connect_args={
                "timeout": 15,
                "server_settings": {
                    "application_name": application_name,
                    # Prevent abandoned/cancelled requests from retaining a
                    # transaction and exhausting the production connection pool.
                    "idle_in_transaction_session_timeout": "120000",
                },
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    session_factory = get_session_factory()
    started = time.monotonic()
    async with session_factory() as session:
        try:
            yield session
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 2.0:
                pool = get_engine().sync_engine.pool
                logger.warning(
                    "slow_db_session duration_ms=%d checked_out=%s pool_size=%s",
                    round(elapsed * 1000),
                    pool.checkedout(),
                    pool.size(),
                )


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
