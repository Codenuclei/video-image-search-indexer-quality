"""Postgres session-level advisory locks for multi-worker (Gunicorn) safety.

In-memory asyncio.Lock only protects one process. With WEB_CONCURRENCY>1 we need
cross-process serialization for:
  - background-worker leader election (indexer / push channel / maintenance)
  - carousel extract + cold theme generation storms
  - Drive cache refresh + DB sync (webhook can hit any worker)
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.session import get_engine

logger = logging.getLogger(__name__)

# Fixed int keys (pg advisory locks are int64). Stable across deploys.
LOCK_BACKGROUND_LEADER = 0xDF100001
LOCK_CAROUSEL_EXTRACT = 0xDF100002
LOCK_CAROUSEL_THEMES = 0xDF100003
LOCK_DRIVE_CACHE_REFRESH = 0xDF100004
LOCK_IMAGE_INDEX_CLAIM = 0xDF100005
LOCK_VIDEO_INDEX_CLAIM = 0xDF100006


def video_index_lock_key(file_id: str) -> int:
    """Stable per-video int64 key used to exclude duplicate execution."""
    digest = hashlib.blake2b(
        file_id.encode("utf-8"), digest_size=8, person=b"videoidx"
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


class AdvisoryLockHandle:
    """Holds an open connection that owns a session-level advisory lock."""

    __slots__ = ("conn", "key", "name")

    def __init__(self, conn: AsyncConnection, key: int, name: str) -> None:
        self.conn = conn
        self.key = key
        self.name = name

    async def release(self) -> None:
        try:
            await self.conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": self.key}
            )
            await self.conn.commit()
        except Exception:  # noqa: BLE001
            logger.debug("advisory unlock failed key=%s name=%s", self.key, self.name, exc_info=True)
        try:
            await self.conn.close()
        except Exception:  # noqa: BLE001
            logger.debug("advisory conn close failed name=%s", self.name, exc_info=True)


async def try_acquire_advisory_lock(key: int, *, name: str) -> AdvisoryLockHandle | None:
    """Non-blocking acquire. Caller must hold the handle until work finishes."""
    engine = get_engine()
    conn = await engine.connect()
    try:
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        ).scalar()
        await conn.commit()
        if not got:
            await conn.close()
            return None
        logger.info("Acquired advisory lock name=%s key=%s", name, key)
        return AdvisoryLockHandle(conn, key, name)
    except Exception:
        await conn.close()
        raise


@asynccontextmanager
async def advisory_lock(
    key: int,
    *,
    name: str,
    blocking: bool = False,
) -> AsyncIterator[bool]:
    """
    Context manager around an advisory lock.

    Yields True if the lock was acquired. When not acquired and blocking=False,
    yields False without holding a connection.
    """
    engine = get_engine()
    conn = await engine.connect()
    acquired = False
    try:
        if blocking:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            await conn.commit()
            acquired = True
        else:
            got = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar()
            await conn.commit()
            acquired = bool(got)
        if not acquired:
            yield False
            return
        logger.debug("advisory lock held name=%s", name)
        yield True
    finally:
        if acquired:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()
            except Exception:  # noqa: BLE001
                logger.debug("advisory unlock failed name=%s", name, exc_info=True)
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            pass


# Process-local leader handle (kept open for the worker lifetime).
_leader_handle: AdvisoryLockHandle | None = None


async def try_become_background_leader() -> bool:
    """Elect exactly one Gunicorn worker to run indexer / push / maintenance."""
    global _leader_handle
    if _leader_handle is not None:
        return True
    handle = await try_acquire_advisory_lock(
        LOCK_BACKGROUND_LEADER, name="background_leader"
    )
    if handle is None:
        logger.info("Not background leader — API-only worker")
        return False
    _leader_handle = handle
    return True


async def release_background_leader() -> None:
    global _leader_handle
    if _leader_handle is None:
        return
    await _leader_handle.release()
    _leader_handle = None
    logger.info("Released background leader lock")


def is_background_leader() -> bool:
    return _leader_handle is not None
