#!/usr/bin/env python3
"""CLI: recover Postgres linkage from existing Qdrant embeddings (append-only).

Usage (from backend/):
  python -m scripts.recover_from_qdrant --dry-run
  python -m scripts.recover_from_qdrant --apply
  python -m scripts.recover_from_qdrant --apply --create-orphaned-stubs

Reads DATABASE_URL / QDRANT_* from backend/.env (or environment).
Never wipes Qdrant or Postgres. Never calls Gemini.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Allow `python scripts/recover_from_qdrant.py` from backend/
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


async def _main(dry_run: bool, create_orphaned_stubs: bool) -> int:
    from app.db.session import get_session_factory
    from app.qdrant.recover import recover_from_qdrant

    factory = get_session_factory()
    async with factory() as session:
        result = await recover_from_qdrant(
            session,
            dry_run=dry_run,
            create_orphaned_stubs=create_orphaned_stubs,
        )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Inventory + plan only (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Commit status/media repairs (append-only)",
    )
    parser.add_argument(
        "--create-orphaned-stubs",
        action="store_true",
        default=False,
        help=(
            "Create minimal drive_files/media for Qdrant ids missing in Postgres. "
            "Unsafe: payloads lack name/path/mime."
        ),
    )
    args = parser.parse_args()
    dry_run = not args.apply
    raise SystemExit(
        asyncio.run(_main(dry_run=dry_run, create_orphaned_stubs=args.create_orphaned_stubs))
    )


if __name__ == "__main__":
    main()
