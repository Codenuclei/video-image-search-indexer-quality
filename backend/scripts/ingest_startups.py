#!/usr/bin/env python3
"""CLI: ingest startups catalog into the database.

Usage (from backend/):
  python -m scripts.ingest_startups
  python -m scripts.ingest_startups --no-logos
  python -m scripts.ingest_startups --no-faces
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow `python scripts/ingest_startups.py` from backend/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Ingest startups catalog")
    parser.add_argument("--no-logos", action="store_true", help="Skip logo embeddings")
    parser.add_argument("--no-faces", action="store_true", help="Skip founder face linking")
    parser.add_argument(
        "--refresh-logos", action="store_true", help="Delete and re-embed all logo refs"
    )
    args = parser.parse_args()

    from app.db.session import get_session_factory
    from app.startups.ingest import ingest_catalog
    from app.startups.paths import catalog_path

    print(f"catalog={catalog_path()} exists={catalog_path().is_file()}")
    factory = get_session_factory()
    async with factory() as session:
        stats = await ingest_catalog(
            session,
            embed_logos=not args.no_logos,
            link_faces=not args.no_faces,
            refresh_logos=args.refresh_logos,
        )
    print(json.dumps(stats.as_dict(), indent=2))
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
