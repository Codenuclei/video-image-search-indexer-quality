"""In-memory state for the optional Go indexer canary sidecar."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock


@dataclass
class GoIndexerRunStats:
    files_ok: int = 0
    files_err: int = 0
    elapsed_ms: int = 0
    files_per_sec: float = 0.0
    download_bytes: int = 0
    reported_at: datetime | None = None


@dataclass
class GoIndexerState:
    last_heartbeat_at: datetime | None = None
    last_stats: GoIndexerRunStats = field(default_factory=GoIndexerRunStats)
    claimed_at: dict[str, float] = field(default_factory=dict)  # file_id -> monotonic-ish utc ts
    _lock: Lock = field(default_factory=Lock, repr=False)


_state = GoIndexerState()


def get_go_indexer_state() -> GoIndexerState:
    return _state


def go_heartbeat() -> None:
    with _state._lock:
        _state.last_heartbeat_at = datetime.now(timezone.utc)


def go_report_stats(
    *,
    files_ok: int,
    files_err: int,
    elapsed_ms: int,
    download_bytes: int = 0,
) -> GoIndexerRunStats:
    elapsed_s = max(elapsed_ms, 1) / 1000.0
    stats = GoIndexerRunStats(
        files_ok=files_ok,
        files_err=files_err,
        elapsed_ms=elapsed_ms,
        files_per_sec=round(files_ok / elapsed_s, 3),
        download_bytes=download_bytes,
        reported_at=datetime.now(timezone.utc),
    )
    with _state._lock:
        _state.last_stats = stats
        _state.last_heartbeat_at = stats.reported_at
    return stats


def go_track_claim(file_ids: list[str]) -> None:
    now = datetime.now(timezone.utc).timestamp()
    with _state._lock:
        for fid in file_ids:
            _state.claimed_at[fid] = now


def go_untrack(file_id: str) -> None:
    with _state._lock:
        _state.claimed_at.pop(file_id, None)


def go_claimed_ids() -> set[str]:
    with _state._lock:
        return set(_state.claimed_at.keys())


def go_is_alive(*, max_age_seconds: float = 60.0) -> bool:
    with _state._lock:
        hb = _state.last_heartbeat_at
    if hb is None:
        return False
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    return age <= max_age_seconds


def go_stale_claims(*, max_age_seconds: float = 900.0) -> list[str]:
    now = datetime.now(timezone.utc).timestamp()
    with _state._lock:
        return [fid for fid, ts in _state.claimed_at.items() if (now - ts) > max_age_seconds]
