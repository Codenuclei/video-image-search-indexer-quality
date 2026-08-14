"""Disk-space guards for durable cache and upload writes."""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

DEFAULT_DISK_RESERVE_BYTES = 64 * 1024 * 1024
# Claim gate: stop admitting new index downloads when free space falls below this.
DEFAULT_INDEX_DISK_HIGH_WATER_BYTES = 2 * 1024 * 1024 * 1024


class RetryableDiskSpaceError(OSError):
    """A write cannot safely start or continue until disk space is freed."""

    def __init__(self, path: str | os.PathLike[str], required_bytes: int, free_bytes: int):
        self.path = str(path)
        self.required_bytes = max(0, int(required_bytes))
        self.free_bytes = max(0, int(free_bytes))
        super().__init__(
            errno.ENOSPC,
            (
                "retryable_disk_full: insufficient free space for write "
                f"(need {self.required_bytes} bytes including reserve, "
                f"have {self.free_bytes} bytes) at {self.path}; "
                "free disk space and retry"
            ),
            self.path,
        )


def _existing_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def disk_usage_report(path: str | os.PathLike[str]) -> dict[str, int | float | str | bool]:
    """Return free/used/total bytes for ops and /health/detail disk readiness."""
    target = _existing_path(path)
    usage = shutil.disk_usage(target)
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    pct_used = round((used / total) * 100.0, 1) if total else 0.0
    return {
        "path": str(target),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "pct_used": pct_used,
        "ok": free >= DEFAULT_DISK_RESERVE_BYTES,
    }


def indexing_disk_ready(
    path: str | os.PathLike[str],
    *,
    high_water_bytes: int = DEFAULT_INDEX_DISK_HIGH_WATER_BYTES,
) -> bool:
    """True when the volume has enough free space to admit new index downloads."""
    free = shutil.disk_usage(_existing_path(path)).free
    return free >= max(0, int(high_water_bytes))


def ensure_disk_space(
    path: str | os.PathLike[str],
    payload_bytes: int = 0,
    *,
    reserve_bytes: int = DEFAULT_DISK_RESERVE_BYTES,
) -> None:
    """Raise before a write when payload plus the safety reserve will not fit."""
    target = _existing_path(path)
    free = shutil.disk_usage(target).free
    required = max(0, int(payload_bytes)) + max(0, int(reserve_bytes))
    if free < required:
        raise RetryableDiskSpaceError(path, required, free)
