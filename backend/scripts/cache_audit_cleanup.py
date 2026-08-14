"""Audit video-cache retention and optionally delete conservative Drive candidates.

Dry-run is the default. ``--apply`` only removes inactive, processed Drive
source files whose database row has a Media record. Upload, YouTube, unknown,
active, and incomplete rows are always refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Support `python backend/scripts/cache_audit_cleanup.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db.models import DriveFile, DriveFileStatus, Media
from app.db.session import dispose_engine, get_session_factory
from sqlalchemy import select

DELETE_POLICY = "delete_processed_drive_with_media"


@dataclass(frozen=True)
class CacheDbState:
    file_id: str
    source: str
    status: str
    carousel_status: str
    has_media: bool


@dataclass(frozen=True)
class AuditRow:
    path: Path
    size: int
    state: CacheDbState | None
    policy: str
    reason: str

    @property
    def deletable(self) -> bool:
        return self.policy == DELETE_POLICY


def classify_cache_path(path: Path, state: CacheDbState | None) -> AuditRow:
    size = path.lstat().st_size
    if path.is_symlink():
        return AuditRow(path, size, state, "keep_unknown", "symlink is never deleted")
    if ".partial" in path.name:
        return AuditRow(
            path, size, state, "keep_partial", "partial file requires manual review"
        )
    if state is None:
        return AuditRow(path, size, None, "keep_unknown", "no matching DB row")
    source = (state.source or "unknown").lower()
    if source in {"upload", "youtube"}:
        return AuditRow(
            path, size, state, f"keep_{source}", "source bytes are retained"
        )
    if source != "drive":
        return AuditRow(
            path, size, state, "keep_unknown", f"unrecognized source {source!r}"
        )
    if (
        state.status == DriveFileStatus.PROCESSING.value
        or state.carousel_status == "processing"
    ):
        return AuditRow(
            path, size, state, "keep_active", "index or carousel processing is active"
        )
    if state.status == DriveFileStatus.PROCESSED.value and state.has_media:
        return AuditRow(
            path,
            size,
            state,
            DELETE_POLICY,
            "inactive processed Drive row has durable Media",
        )
    return AuditRow(
        path,
        size,
        state,
        "keep_incomplete_drive",
        "requires quiesced retry review and remote Drive verification",
    )


def duplicate_case_groups(paths: Iterable[Path]) -> list[list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        groups[str(path.resolve()).casefold()].append(path)
    return [
        sorted(group, key=lambda p: p.name)
        for group in groups.values()
        if len(group) > 1
    ]


def _file_id(path: Path) -> str:
    return path.name.rsplit(".", 1)[0] if "." in path.name else path.name


async def _load_states() -> dict[str, CacheDbState]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(DriveFile, Media.id).outerjoin(
                    Media, Media.drive_file_id == DriveFile.id
                )
            )
        ).all()
    return {
        drive_file.id: CacheDbState(
            file_id=drive_file.id,
            source=drive_file.source,
            status=(
                drive_file.status.value
                if hasattr(drive_file.status, "value")
                else str(drive_file.status)
            ),
            carousel_status=drive_file.carousel_status or "idle",
            has_media=media_id is not None,
        )
        for drive_file, media_id in rows
    }


async def _load_state(file_id: str) -> CacheDbState | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(DriveFile, Media.id)
                .outerjoin(Media, Media.drive_file_id == DriveFile.id)
                .where(DriveFile.id == file_id)
            )
        ).first()
    if row is None:
        return None
    drive_file, media_id = row
    return CacheDbState(
        file_id=drive_file.id,
        source=drive_file.source,
        status=(
            drive_file.status.value
            if hasattr(drive_file.status, "value")
            else str(drive_file.status)
        ),
        carousel_status=drive_file.carousel_status or "idle",
        has_media=media_id is not None,
    )


async def run(root: Path, *, apply: bool) -> int:
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"cache root does not exist: {root}")
    states = await _load_states()
    paths = sorted(p for p in root.iterdir() if p.is_file() or p.is_symlink())
    rows = [classify_cache_path(path, states.get(_file_id(path))) for path in paths]

    for row in rows:
        state = row.state
        print(
            "\t".join(
                [
                    row.policy,
                    state.source if state else "unknown",
                    state.status if state else "unknown",
                    f"media={state.has_media if state else 'unknown'}",
                    f"bytes={row.size}",
                    str(row.path),
                    row.reason,
                ]
            )
        )

    duplicates = duplicate_case_groups(paths)
    for group in duplicates:
        print("DUPLICATE_CASE_PATHS\t" + "\t".join(str(p) for p in group))

    counts = Counter(row.policy for row in rows)
    bytes_by_policy = Counter()
    for row in rows:
        bytes_by_policy[row.policy] += row.size
    print(f"MODE\t{'APPLY' if apply else 'DRY_RUN'}")
    print(f"TOTAL\tfiles={len(rows)}\tbytes={sum(row.size for row in rows)}")
    for policy in sorted(counts):
        print(
            f"POLICY\t{policy}\tfiles={counts[policy]}\tbytes={bytes_by_policy[policy]}"
        )
    print(
        f"DUPLICATES\tgroups={len(duplicates)}"
        f"\tphysical_files={sum(len(g) for g in duplicates)}"
        f"\tphysical_bytes={sum(p.lstat().st_size for g in duplicates for p in g)}"
        f"\tredundant_bytes={sum(sum(p.lstat().st_size for p in g) - max(p.lstat().st_size for p in g) for g in duplicates)}"
    )

    if not apply:
        print("No files deleted. Re-run with --apply after reviewing every candidate.")
        return 0

    deleted_files = deleted_bytes = 0
    for row in rows:
        if not row.deletable:
            continue
        # Containment and policy are checked again immediately before deletion.
        resolved = row.path.resolve()
        if resolved.parent != root or row.path.is_symlink():
            print(f"REFUSED\t{row.path}\tpath escaped cache root or became symlink")
            continue
        # Re-read DB ownership immediately before unlink so a newly active row
        # cannot be deleted based on the earlier inventory snapshot.
        current = classify_cache_path(row.path, await _load_state(_file_id(row.path)))
        if not current.deletable:
            print(f"REFUSED\t{row.path}\tpolicy changed")
            continue
        row.path.unlink()
        deleted_files += 1
        deleted_bytes += row.size
        print(f"DELETED\tbytes={row.size}\t{row.path}")
    print(f"APPLIED\tfiles={deleted_files}\tbytes={deleted_bytes}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(get_settings().video_cache_dir),
        help="video cache root (default: VIDEO_CACHE_DIR)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete only conservative processed Drive+Media candidates",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.root, apply=args.apply))
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    raise SystemExit(main())
