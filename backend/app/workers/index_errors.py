"""Map raw indexing exceptions to short, user-facing error strings."""
from __future__ import annotations


def friendly_index_error_message(exc: BaseException, *, max_len: int = 500) -> str:
    """
    Prefer a short readable message over SQLAlchemy/asyncpg stack walls.
    Full technical text is still logged by the caller.
    """
    raw = str(exc).strip() or type(exc).__name__
    lower = raw.lower()
    name = type(exc).__name__

    if (
        "infailedsqltransaction" in lower
        or "current transaction is aborted" in lower
        or name == "InFailedSQLTransactionError"
    ):
        return "Database transaction aborted during face clustering. Retry this file."

    if "deadlock detected" in lower or name == "DeadlockDetectedError":
        return "Database deadlock while updating face clusters. Retry this file."

    if (
        "uniqueviolation" in lower
        or "unique constraint" in lower
        or "duplicate key" in lower
        or name == "UniqueViolationError"
    ):
        return "Database conflict while saving face data. Retry this file."

    if any(
        token in lower
        for token in (
            "connection refused",
            "connection reset",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
    ):
        return "Temporary network or service timeout. Retry this file."

    technical = any(
        token in lower
        for token in ("sqlalchemy", "asyncpg", "psycopg", "greenlet", "traceback")
    ) or name.endswith("Error") and any(
        token in name for token in ("SQL", "DBAPI", "Integrity", "Operational", "Interface")
    )

    if technical or len(raw) > max_len or "\n" in raw:
        # Keep a short head for uncommon DB errors; avoid dumping multi-line stacks.
        head = next((ln.strip() for ln in raw.splitlines() if ln.strip()), name)
        if any(
            token in head.lower()
            for token in ("sqlalchemy", "asyncpg", "psycopg", "infailedsql")
        ):
            return "Database error during indexing. Retry this file."
        if len(head) > max_len:
            return head[: max_len - 1] + "…"
        return head[:max_len]

    return raw[:max_len]
