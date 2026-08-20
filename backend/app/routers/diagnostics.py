"""Secret-gated, fixed production verification suite.

Never accepts commands/test names and never passes production DB credentials to
the subprocess. Live checks are read-only SQL aggregates.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db
from app.drive.library_shell_cache import (
    compute_library_revision_sql,
    get_library_shell_cache,
)

router = APIRouter(tags=["diagnostics"])

_run_lock = asyncio.Lock()
_last_run_mono = 0.0


def _authorize(settings: Settings, token: str | None) -> None:
    expected = (settings.production_tests_token or "").strip()
    if not settings.production_tests_enabled or not expected:
        # Hide the endpoint unless explicitly enabled and configured.
        raise HTTPException(status_code=404, detail="Not found")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid tests token")


def _safe_subprocess_env() -> dict[str, str]:
    """Minimal environment with all live DB/API credentials removed."""
    keep = ("PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL", "TZ")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    # Defense in depth: diagnostics module is DB-free, and cannot discover the
    # production URL through normal Settings loading.
    env["DATABASE_URL"] = "postgresql+asyncpg://blocked:blocked@127.0.0.1:1/blocked"
    env["TEST_DATABASE_URL"] = "postgresql+asyncpg://blocked:blocked@127.0.0.1:1/blocked_test"
    return env


async def _run_fixed_suite(settings: Settings) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.diagnostics.production_suite",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_safe_subprocess_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(5, settings.production_tests_timeout_seconds),
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return {"ok": False, "suite": "production-safe-v1", "error": "timeout"}

    line = stdout.decode("utf-8", errors="replace").strip().splitlines()
    try:
        payload = json.loads(line[-1]) if line else {}
    except json.JSONDecodeError:
        payload = {"ok": False, "suite": "production-safe-v1", "error": "invalid runner output"}
    if process.returncode != 0:
        payload["ok"] = False
        if "error" not in payload:
            # Only a short final line; never return full logs/environment.
            payload["error"] = stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:][0][:300] if stderr.strip() else "checks failed"
    return payload


async def _live_read_only_checks(session: AsyncSession) -> dict[str, object]:
    t0 = time.monotonic()
    await session.execute(text("SELECT 1"))
    db_ms = round((time.monotonic() - t0) * 1000, 2)

    t1 = time.monotonic()
    revision = await compute_library_revision_sql(session)
    revision_ms = round((time.monotonic() - t1) * 1000, 2)

    status_rows = (
        await session.execute(
            select(DriveFile.status, func.count()).group_by(DriveFile.status)
        )
    ).all()
    counts = {
        (status.value if hasattr(status, "value") else str(status)): int(count)
        for status, count in status_rows
    }

    duplicate_groups = (
        select(DriveFile.content_hash, DriveFile.content_hash_algo)
        .where(
            DriveFile.content_hash.is_not(None),
            DriveFile.status == DriveFileStatus.PROCESSED,
        )
        .group_by(DriveFile.content_hash, DriveFile.content_hash_algo)
        .having(func.count(DriveFile.id) > 1)
        .subquery()
    )
    duplicate_hash_groups = int(
        (
            await session.execute(
                select(func.count()).select_from(duplicate_groups)
            )
        ).scalar_one()
        or 0
    )

    shell_cache = get_library_shell_cache()
    return {
        "ok": True,
        "database_select_ms": db_ms,
        "library_revision_ms": revision_ms,
        "library_revision": revision,
        "status_counts": counts,
        "processed_duplicate_hash_groups": duplicate_hash_groups,
        "shell_cache": {
            "warm": shell_cache.payload is not None,
            "revision_matches": shell_cache.revision == revision,
        },
    }


@router.post("/tests")
async def run_production_tests(
    x_tests_token: str | None = Header(default=None, alias="X-Tests-Token"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Run one fixed safe suite and read-only checks against the live service."""
    global _last_run_mono

    _authorize(settings, x_tests_token)
    now = time.monotonic()
    cooldown = max(0, settings.production_tests_cooldown_seconds)
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="Tests already running")
    if _last_run_mono and now - _last_run_mono < cooldown:
        retry_after = max(1, int(cooldown - (now - _last_run_mono)))
        raise HTTPException(
            status_code=429,
            detail="Tests cooldown active",
            headers={"Retry-After": str(retry_after)},
        )

    async with _run_lock:
        started = time.monotonic()
        suite, live = await asyncio.gather(
            _run_fixed_suite(settings),
            _live_read_only_checks(session),
        )
        _last_run_mono = time.monotonic()

    return {
        "ok": bool(suite.get("ok")) and bool(live.get("ok")),
        "suite": suite,
        "live": live,
        "elapsed_ms": round((_last_run_mono - started) * 1000, 2),
    }
