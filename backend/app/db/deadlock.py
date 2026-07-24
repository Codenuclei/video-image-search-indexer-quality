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


def is_aborted_transaction_error(exc: BaseException) -> bool:
    """True when Postgres rejected work because an earlier statement aborted the txn."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name == "InFailedSQLTransactionError":
            return True
        msg = str(current).lower()
        if "current transaction is aborted" in msg or "infailedsqltransaction" in msg:
            return True
        current = current.__cause__ or current.__context__
    return False


def is_transient_db_error(exc: BaseException) -> bool:
    """Deadlocks / aborted-txn fallout — safe to re-queue the file as PENDING."""
    return is_deadlock_error(exc) or is_aborted_transaction_error(exc)


async def retry_on_deadlock(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    base_delay_seconds: float = 0.08,
    label: str = "db operation",
) -> T:
    """Run an async DB operation; retry with backoff on deadlock / aborted-txn.

    Caller must open a *fresh* session inside ``fn`` — retrying on an aborted
    transaction is useless.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            retryable = is_transient_db_error(exc)
            if not retryable or attempt >= max_attempts - 1:
                raise
            last_exc = exc
            delay = base_delay_seconds * (2**attempt)
            logger.warning(
                "%s transient DB error (attempt %d/%d) — retrying in %.2fs: %s",
                label,
                attempt + 1,
                max_attempts,
                delay,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_on_deadlock exhausted without result")
