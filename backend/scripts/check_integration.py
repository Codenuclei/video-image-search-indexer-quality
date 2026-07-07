"""Quick integration status checks (DB + HTTP)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://drivefaceindexer:drivefaceindexer@localhost:55432/drivefaceindexer_test"
API = "http://127.0.0.1:8000"
FENNEC = "http://127.0.0.1:8002"
CACHE = Path(__file__).resolve().parents[2] / "data" / "fennec-media"


async def main() -> int:
    print("=== Stage 3: DFI health ===")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{API}/health")
            print(r.status_code, r.text)
            r2 = await client.get(f"{API}/fennec/status")
            print("fennec/status", r2.status_code, r2.text)
    except Exception as exc:  # noqa: BLE001
        print("DFI API unreachable:", exc)

    print("\n=== Stage 4: DB + cache ===")
    engine = create_async_engine(DB_URL)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("select id, name, mime_type, status from drive_files order by name"))).fetchall()
        print(f"drive_files: {len(rows)}")
        for row in rows:
            print(" ", row)
        videos = [r for r in rows if str(r[2]).startswith("video/")]
        print(f"videos in DB: {len(videos)}")

    cache_files = list(CACHE.glob("*")) if CACHE.exists() else []
    print(f"fennec-media cache: {CACHE} ({len(cache_files)} files)")
    for f in cache_files:
        print(" ", f.name, f.stat().st_size)

    print("\n=== Stage 2: Fennec API ===")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{FENNEC}/api/ready")
            print("fennec ready", r.status_code, r.text)
    except Exception as exc:  # noqa: BLE001
        print("Fennec API unreachable:", exc)

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
