#!/usr/bin/env python3
"""Load probe: concurrent Drive downloads (files/sec).

Uses live Drive tokens from Postgres + DriveDirectClient. Does NOT delete
indexed data, upsert Qdrant, or change folder selection.

Usage (from backend/ with venv + .env):
  .venv/bin/python scripts/test_drive_download_throughput.py
  .venv/bin/python scripts/test_drive_download_throughput.py --n 40 --parallel 16
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)


async def _pick_file_ids(n: int) -> list[str]:
    from sqlalchemy import select

    from app.db.models import DriveFile, DriveFileStatus
    from app.db.session import get_session_factory
    from app.pipelines.common import is_image_mime

    sf = get_session_factory()
    async with sf() as session:
        rows = list(
            (
                await session.execute(
                    select(DriveFile)
                    .where(
                        DriveFile.source == "drive",
                        DriveFile.status.in_(
                            [DriveFileStatus.PROCESSED, DriveFileStatus.PENDING, DriveFileStatus.ARCHIVED]
                        ),
                    )
                    .order_by(DriveFile.last_synced_at.desc().nullslast())
                    .limit(n * 4)
                )
            ).scalars().all()
        )
    ids = [
        r.id
        for r in rows
        if is_image_mime(r.mime_type, r.name) and (r.size or 0) < 8_000_000
    ]
    return ids[:n]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Drive download throughput probe")
    parser.add_argument("--n", type=int, default=24, help="Files to download")
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="Override DRIVE_DOWNLOAD_MAX_CONCURRENT for this run (0 = settings)",
    )
    args = parser.parse_args()

    from app.config import get_settings
    from app.dependencies import get_drive_client
    from app.pipelines.common import download_to_memory
    import app.drive.rate_limit as rl

    settings = get_settings()
    parallel = args.parallel or settings.drive_download_max_concurrent
    if args.parallel:
        # Reset cached semaphore to the probe override.
        rl._download_sem = None
        rl._download_sem_n = None
        with_settings = settings.model_copy(update={"drive_download_max_concurrent": parallel})
        # Monkeypatch get_settings for this process.
        from app import config as cfg

        cfg.get_settings.cache_clear()
        os.environ["DRIVE_DOWNLOAD_MAX_CONCURRENT"] = str(parallel)
        cfg.get_settings.cache_clear()

    ids = await _pick_file_ids(args.n)
    if not ids:
        print("No Drive image file ids found in Postgres — connect + sync first.")
        return 2

    client = get_drive_client()
    ok = 0
    err = 0
    bytes_total = 0
    t0 = time.perf_counter()

    async def one(fid: str) -> None:
        nonlocal ok, err, bytes_total
        try:
            data = await download_to_memory(client, fid)
            ok += 1
            bytes_total += len(data)
        except Exception as exc:  # noqa: BLE001
            err += 1
            print(f"FAIL {fid}: {exc}")

    await asyncio.gather(*[one(fid) for fid in ids])
    elapsed = time.perf_counter() - t0
    fps = ok / elapsed if elapsed else 0.0
    mibs = (bytes_total / (1024 * 1024)) / elapsed if elapsed else 0.0
    print(
        f"downloaded_ok={ok} errors={err} parallel≈{parallel} "
        f"elapsed_s={elapsed:.2f} files_per_sec={fps:.2f} MiB_per_sec={mibs:.2f}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
