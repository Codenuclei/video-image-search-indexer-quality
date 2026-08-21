"""Audit video/media cache retention and optionally delete conservative Drive candidates.

Dry-run is the default. ``--apply`` only removes inactive, processed Drive
source files whose database row has a Media record. Upload, YouTube, unknown,
active, and incomplete rows are always refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Support `python backend/scripts/cache_audit_cleanup.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.session import dispose_engine
from app.drive.cache_cleanup import DELETE_POLICY, run_cache_cleanup


async def _run(root: Path | None, *, apply: bool, all_roots: bool) -> int:
    if all_roots or root is None:
        result = await run_cache_cleanup(apply=apply)
    else:
        # Single-root mode for backward-compatible CLI use.
        from app.drive.cache_cleanup import (
            audit_roots,
            load_cache_states,
            load_cache_state,
            file_id_from_cache_path,
            classify_cache_path,
        )

        states = await load_cache_states()
        rows = audit_roots([("custom", root)], states)
        deletable = [r for r in rows if r.deletable]
        print(f"MODE\t{'APPLY' if apply else 'DRY_RUN'}")
        print(f"TOTAL\tfiles={len(rows)}\tbytes={sum(r.size for r in rows)}")
        print(
            f"POLICY\t{DELETE_POLICY}\tfiles={len(deletable)}\tbytes={sum(r.size for r in deletable)}"
        )
        if not apply:
            print("No files deleted. Re-run with --apply after reviewing every candidate.")
            return 0
        deleted_files = deleted_bytes = 0
        root_resolved = root.resolve()
        for row in deletable:
            if row.path.resolve().parent != root_resolved or row.path.is_symlink():
                continue
            current = classify_cache_path(
                row.path, await load_cache_state(file_id_from_cache_path(row.path))
            )
            if not current.deletable:
                continue
            row.path.unlink()
            deleted_files += 1
            deleted_bytes += row.size
        print(f"APPLIED\tfiles={deleted_files}\tbytes={deleted_bytes}")
        return 0

    print(f"MODE\t{'APPLY' if apply else 'DRY_RUN'}")
    print(f"TOTAL\tfiles={result['total_files']}\tbytes={result['total_bytes']}")
    for policy, stats in sorted(result["by_policy"].items()):
        print(f"POLICY\t{policy}\tfiles={stats['files']}\tbytes={stats['bytes']}")
    print(
        f"DELETABLE\tfiles={result['deletable_count']}\tbytes={result['deletable_bytes']}"
    )
    if apply:
        print(
            f"APPLIED\tfiles={result['deleted_count']}\tbytes={result['deleted_bytes']}"
        )
    else:
        print("No files deleted. Re-run with --apply after reviewing every candidate.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="single cache root (default: both media_cache + videos)",
    )
    parser.add_argument(
        "--all-roots",
        action="store_true",
        help="audit media_cache and videos (default when --root omitted)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete only conservative processed Drive+Media candidates",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(
            _run(args.root, apply=args.apply, all_roots=args.all_roots or args.root is None)
        )
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    raise SystemExit(main())
