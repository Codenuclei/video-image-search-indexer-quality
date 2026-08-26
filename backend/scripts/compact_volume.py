"""Compact thumbnail JPEGs and enforce short on-volume backup retention."""
from __future__ import annotations

import argparse
import fcntl
import gzip
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

TARGET_JPEG_BYTES = 160 * 1024
JPEG_ATTEMPTS = ((960, 55),)
COMPACT_WORKERS = 8


def _older_than(path: Path, cutoff: datetime) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff


def prune_backups(root: Path, retention_days: int, *, apply: bool) -> tuple[int, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
    removed = 0
    reclaimed = 0
    daily = root / "daily"
    if daily.is_dir():
        for child in daily.iterdir():
            if child.is_dir() and _older_than(child, cutoff):
                size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
                if apply:
                    shutil.rmtree(child)
                removed += 1
                reclaimed += size
    forever = root / "forever"
    if forever.is_dir():
        for child in forever.iterdir():
            if child.is_file() and _older_than(child, cutoff):
                size = child.stat().st_size
                if apply:
                    child.unlink(missing_ok=True)
                removed += 1
                reclaimed += size
    return removed, reclaimed


def gzip_retained_exports(root: Path, *, apply: bool) -> tuple[int, int]:
    compressed = 0
    reclaimed = 0
    for source in root.rglob("carousel-deep-dives-*.json"):
        dest = source.with_suffix(source.suffix + ".gz")
        if dest.exists():
            continue
        before = source.stat().st_size
        if apply:
            partial = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.partial")
            with source.open("rb") as src, gzip.open(partial, "wb", compresslevel=6) as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            if source.exists():
                partial.replace(dest)
            else:
                partial.unlink(missing_ok=True)
                continue
            after = dest.stat().st_size
            source.unlink()
        else:
            after = before
        compressed += 1
        reclaimed += max(0, before - after)
    return compressed, reclaimed


def _encode_compact(source: Path, dest: Path, max_edge: int, quality: int) -> int:
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode != "RGB":
            image = image.convert("RGB")
        else:
            image = image.copy()
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    image.save(dest, "JPEG", quality=quality)
    return dest.stat().st_size


def compact_thumbnails(root: Path, *, apply: bool) -> tuple[int, int, int]:
    sources = [
        source
        for source in root.rglob("*")
        if source.is_file() and source.suffix.lower() in {".jpg", ".jpeg"}
    ]
    if not apply:
        return len(sources), 0, 0

    def _compact_one(source: Path) -> tuple[int, int]:
        before = source.stat().st_size
        if before <= TARGET_JPEG_BYTES:
            return 0, 0
        best: Path | None = None
        best_size = before
        try:
            for index, (max_edge, quality) in enumerate(JPEG_ATTEMPTS):
                candidate = source.with_name(f"{source.name}.compact-{index}.partial")
                size = _encode_compact(source, candidate, max_edge, quality)
                if size < best_size:
                    if best is not None:
                        best.unlink(missing_ok=True)
                    best = candidate
                    best_size = size
                else:
                    candidate.unlink(missing_ok=True)
                if best_size <= TARGET_JPEG_BYTES:
                    break
            if best is not None and best_size < before:
                best.replace(source)
                return 1, before - best_size
        except Exception:
            if best is not None:
                best.unlink(missing_ok=True)
        return 0, 0

    compacted = 0
    reclaimed = 0
    with ThreadPoolExecutor(max_workers=COMPACT_WORKERS) as executor:
        for did_compact, saved in executor.map(_compact_one, sources):
            compacted += did_compact
            reclaimed += saved
    return len(sources), compacted, reclaimed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", "/app/data"))
    parser.add_argument("--retention-days", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    data = Path(args.data_dir)
    lock_path = data / "tmp" / "compact-volume.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print({"applied": False, "skipped": "already_running"}, flush=True)
        return

    removed, backup_reclaimed = prune_backups(
        data / "backups",
        args.retention_days,
        apply=args.apply,
    )
    gzipped, gzip_reclaimed = gzip_retained_exports(
        data / "backups",
        apply=args.apply,
    )
    scanned, compacted, thumb_reclaimed = compact_thumbnails(
        data / "thumbnails",
        apply=args.apply,
    )
    print(
        {
            "applied": args.apply,
            "retention_days": args.retention_days,
            "backup_items_removed": removed,
            "backup_exports_gzipped": gzipped,
            "thumbnails_scanned": scanned,
            "thumbnails_compacted": compacted,
            "reclaimed_gb": round(
                (backup_reclaimed + gzip_reclaimed + thumb_reclaimed) / 1024**3,
                2,
            ),
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
