"""Map raw indexing exceptions to short, user-facing error strings."""
from __future__ import annotations


def is_transient_network_error(exc: BaseException) -> bool:
    """True for Drive/httpx transport failures that should requeue, not hard-ERROR.

    ``httpx.ReadError`` (often str()'d as just ``ReadError``) is a mid-stream
    disconnect while downloading — especially large videos. Same class of
    flake as ConnectError / TimeoutException.
    """
    try:
        import httpx

        if isinstance(
            exc,
            (
                httpx.TransportError,
                httpx.TimeoutException,
                httpx.ReadError,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.WriteError,
                httpx.PoolTimeout,
            ),
        ):
            return True
    except ImportError:
        pass

    name = type(exc).__name__.lower()
    raw = str(exc).strip().lower()
    if name in {
        "readerror",
        "connecterror",
        "timeoutexception",
        "connecttimeoutexception",
        "readtimeoutexception",
        "remoteprotocolerror",
        "writeerror",
        "networkerror",
    }:
        return True
    return any(
        token in raw
        for token in (
            "connection reset",
            "connection refused",
            "connection aborted",
            "broken pipe",
            "server disconnected",
            "remoteprotocolerror",
        )
    )


def friendly_index_error_message(exc: BaseException, *, max_len: int = 500) -> str:
    """
    Prefer a short readable message over SQLAlchemy/asyncpg stack walls.
    Full technical text is still logged by the caller.
    """
    raw = str(exc).strip() or type(exc).__name__
    lower = raw.lower()
    name = type(exc).__name__

    if is_transient_network_error(exc) or name in {"ReadError", "ConnectError", "RemoteProtocolError"}:
        return "Temporary download interruption (network ReadError). Retry this file."

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
