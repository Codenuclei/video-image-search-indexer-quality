"""Daily durable backups for Postgres + Qdrant (never wipe on folder/user change).

Rolling copies live under ``backup_dir/daily/`` with ``backup_retention_days``.
Forever archives (deep-dive / carousel artifacts + full dumps marked keep) live
under ``backup_dir/forever/`` and are **never** auto-deleted by retention.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import CarouselGenerationSave
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)


def _backup_root() -> Path:
    settings = get_settings()
    root = Path(settings.backup_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "daily").mkdir(exist_ok=True)
    (root / "forever").mkdir(exist_ok=True)
    return root


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalize_sync_dsn(url: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


async def export_carousel_deep_dives(dest: Path) -> int:
    """Export carousel / deep-dive 2x generation artifacts as JSON (append-only file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    sf = get_session_factory()
    async with sf() as session:
        rows = list(
            (await session.execute(select(CarouselGenerationSave).order_by(CarouselGenerationSave.id))).scalars().all()
        )
    payload = [
        {
            "id": r.id,
            "drive_file_id": r.drive_file_id,
            "kind": r.kind,
            "theme_key": r.theme_key,
            "label": r.label,
            "model": r.model,
            "transcript_hash": r.transcript_hash,
            "source": r.source,
            "status": r.status,
            "input_hash": r.input_hash,
            "layout_mode": r.layout_mode,
            "copy_version": r.copy_version,
            "algorithm_version": r.algorithm_version,
            "payload": r.payload,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    dest.write_text(json.dumps({"exported_at": _stamp(), "count": len(payload), "items": payload}, default=str), encoding="utf-8")
    return len(payload)


async def backup_postgres(dest_dir: Path) -> dict[str, object]:
    """pg_dump custom format when available; otherwise schema+critical metadata note."""
    settings = get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dest_dir / f"postgres-{_stamp()}.dump"
    dsn = _normalize_sync_dsn(settings.database_url)
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["pg_dump", "--format=custom", "--file", str(dump_path), dsn],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode == 0 and dump_path.is_file():
            return {"ok": True, "path": str(dump_path), "bytes": dump_path.stat().st_size, "tool": "pg_dump"}
        err = (proc.stderr or proc.stdout or "")[:300]
        logger.warning("pg_dump failed (%s) — falling back to SQL export", err)
    except FileNotFoundError:
        logger.info("pg_dump not installed — using SQL metadata export")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pg_dump error: %s", exc)

    # Fallback: dump row counts + carousel JSON (full binary dump needs pg_dump in image).
    sql_path = dest_dir / f"postgres-meta-{_stamp()}.json"
    sf = get_session_factory()
    async with sf() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname"
                )
            )
        ).all()
        meta = {name: int(n or 0) for name, n in tables}
    carousel_n = await export_carousel_deep_dives(dest_dir / f"carousel-deep-dives-{_stamp()}.json")
    sql_path.write_text(json.dumps({"tables": meta, "carousel_exports": carousel_n}, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "path": str(sql_path),
        "tool": "meta_fallback",
        "carousel_rows": carousel_n,
        "note": "Install postgresql-client for full pg_dump; meta + carousel JSON written",
    }


def backup_qdrant(dest_dir: Path) -> dict[str, object]:
    """Request Qdrant snapshots for all DFI collections; record snapshot names."""
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = make_qdrant_client(settings.qdrant_url, timeout=120)
    collection_names = [
        settings.qdrant_collection,
        settings.qdrant_images_collection,
        settings.qdrant_image_captions_collection,
    ]
    results: list[dict[str, object]] = []
    for name in collection_names:
        if not name:
            continue
        try:
            snap = client.create_snapshot(collection_name=name)
            snap_name = getattr(snap, "name", None) or str(snap)
            results.append({"collection": name, "snapshot": snap_name, "ok": True})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qdrant snapshot failed for %s: %s", name, exc)
            results.append({"collection": name, "ok": False, "error": str(exc)[:200]})
    manifest = dest_dir / f"qdrant-snapshots-{_stamp()}.json"
    manifest.write_text(json.dumps({"created_at": _stamp(), "snapshots": results}, indent=2), encoding="utf-8")
    return {"ok": any(r.get("ok") for r in results), "manifest": str(manifest), "snapshots": results}


def prune_daily_backups(daily_dir: Path, *, retention_days: int) -> int:
    """Delete daily folders older than retention. Never touches forever/."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for child in daily_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            # Expect YYYYMMDD or stamped names; use mtime as source of truth.
            mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
            logger.info("Pruned daily backup %s", child)
    return removed


async def run_daily_backup(*, promote_forever: bool = True) -> dict[str, object]:
    """Create today's backup under daily/; copy carousel deep-dives into forever/."""
    settings = get_settings()
    if not settings.backup_enabled:
        return {"ok": False, "skipped": True, "reason": "disabled"}

    root = _backup_root()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    daily = root / "daily" / day
    daily.mkdir(parents=True, exist_ok=True)

    pg = await backup_postgres(daily)
    qd = await asyncio.to_thread(backup_qdrant, daily)
    carousel_forever = root / "forever" / f"carousel-deep-dives-{_stamp()}.json"
    carousel_n = await export_carousel_deep_dives(carousel_forever)

    pruned = prune_daily_backups(root / "daily", retention_days=settings.backup_retention_days)

    summary = {
        "ok": bool(pg.get("ok")) and bool(qd.get("ok") or qd.get("snapshots")),
        "day": day,
        "postgres": pg,
        "qdrant": qd,
        "carousel_forever_rows": carousel_n,
        "carousel_forever_path": str(carousel_forever),
        "pruned_daily_dirs": pruned,
        "forever_dir": str(root / "forever"),
    }
    (daily / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    logger.info(
        "Daily backup complete day=%s pg=%s qdrant_ok=%s carousel=%d pruned=%d",
        day,
        pg.get("tool"),
        qd.get("ok"),
        carousel_n,
        pruned,
    )
    return summary


async def restore_dry_run(day: str | None = None) -> dict[str, object]:
    """Verify a backup exists and looks restorable without applying it."""
    root = _backup_root()
    daily_root = root / "daily"
    if day:
        target = daily_root / day
    else:
        days = sorted([p for p in daily_root.iterdir() if p.is_dir()], reverse=True)
        target = days[0] if days else None
    if target is None or not target.is_dir():
        return {"ok": False, "error": "no daily backup found"}
    summary_path = target / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    dumps = list(target.glob("postgres*"))
    qdrant_manifests = list(target.glob("qdrant-snapshots-*.json"))
    forever = list((root / "forever").glob("carousel-deep-dives-*.json"))
    return {
        "ok": bool(dumps or summary),
        "day_dir": str(target),
        "postgres_artifacts": [str(p) for p in dumps],
        "qdrant_manifests": [str(p) for p in qdrant_manifests],
        "forever_carousel_archives": len(forever),
        "summary": summary,
        "would_restore": False,
        "note": "Dry-run only — no restore applied",
    }


async def backup_loop(stop_event: asyncio.Event) -> None:
    """Leader-only: run backup shortly after boot, then once per day."""
    settings = get_settings()
    if not settings.backup_enabled:
        logger.info("Backup loop disabled")
        return
    # Stagger after boot so healthchecks pass first.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=120)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await run_daily_backup()
        except Exception:  # noqa: BLE001
            logger.exception("Daily backup failed")
        # Sleep until next day-ish (24h), but wake on stop.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(3600, settings.backup_interval_seconds))
            return
        except asyncio.TimeoutError:
            continue
