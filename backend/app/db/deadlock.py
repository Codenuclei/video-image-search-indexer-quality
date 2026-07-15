"""Detect PostgreSQL deadlocks so indexing can retry instead of failing good files."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_deadlock_error(exc: BaseException) -> bool:
    """True when exc is a Postgres deadlock (possibly wrapped by SQLAlchemy/asyncpg)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name == "DeadlockDetectedError":
            return True
        msg = str(current).lower()
        if "deadlock detected" in msg:
            return True
        current = current.__cause__ or current.__context__
    return False


async def retry_on_deadlock(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    base_delay_seconds: float = 0.08,
    label: str = "db operation",
) -> T:
    """Run an async DB operation; retry with backoff when Postgres reports a deadlock."""
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if not is_deadlock_error(exc) or attempt >= max_attempts - 1:
                raise
            last_exc = exc
            delay = base_delay_seconds * (2**attempt)
            logger.warning("%s deadlock (attempt %d/%d) — retrying in %.2fs", label, attempt + 1, max_attempts, delay)
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_on_deadlock exhausted without result")
