"""Per-Drive / per-folder indexing outcome stats for search readiness."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus
from app.workers.requeue_failed import normalize_error_bucket, normalize_skip_reason

_FOLDER_MARKER = "folder_marker"
_TOP_N = 8


def _folder_of(path: str | None) -> str:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def _root_label(path: str | None, root_folder_id: str | None) -> str:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if parts:
        return parts[0]
    return root_folder_id or "unknown"


def _is_folder_marker(df: DriveFile) -> bool:
    return (df.error_message or "") == _FOLDER_MARKER or (
        (df.mime_type or "") == "application/vnd.google-apps.folder"
    )


def _status_value(df: DriveFile) -> str:
    st = df.status
    return st.value if hasattr(st, "value") else str(st)


def _rank_counter(counter: Counter[str], *, limit: int = _TOP_N) -> list[dict[str, Any]]:
    return [{"reason": k, "count": int(v)} for k, v in counter.most_common(limit)]


async def build_drive_index_stats(session: AsyncSession) -> dict[str, Any]:
    """Aggregate indexing outcomes for the connected Drive library.

    Returns totals plus per-root-folder and per-parent-folder breakdowns with
    top skip reasons and top error buckets (for search-readiness dashboards).
    """
    rows = list(
        (
            await session.execute(
                select(DriveFile).where(
                    DriveFile.source == "drive",
                )
            )
        )
        .scalars()
        .all()
    )

    overall = Counter()
    overall_skips: Counter[str] = Counter()
    overall_errors: Counter[str] = Counter()

    by_root: dict[str, dict[str, Any]] = {}
    by_folder: dict[str, dict[str, Any]] = {}

    def _bucket(store: dict[str, dict[str, Any]], key: str, *, name: str) -> dict[str, Any]:
        if key not in store:
            store[key] = {
                "id": key,
                "name": name,
                "path": key if key.startswith("/") else f"/{name}",
                "total": 0,
                "processed": 0,
                "processing": 0,
                "pending": 0,
                "error": 0,
                "skipped": 0,
                "archived": 0,
                "top_skip_reasons": Counter(),
                "top_error_reasons": Counter(),
            }
        return store[key]

    for df in rows:
        if _is_folder_marker(df):
            continue
        from app.drive.library_filters import is_apple_junk_row

        if is_apple_junk_row(name=df.name, error_message=df.error_message):
            continue
        status = _status_value(df)
        overall[status] += 1
        overall["total"] += 1

        skip_key = normalize_skip_reason(df.error_message) if status == "skipped" else None
        err_key = normalize_error_bucket(df.error_message) if status == "error" else None
        if skip_key:
            overall_skips[skip_key] += 1
        if err_key:
            overall_errors[err_key] += 1

        root_id = df.root_folder_id or "unknown"
        root = _bucket(by_root, root_id, name=_root_label(df.path, df.root_folder_id))
        folder_path = _folder_of(df.path)
        folder = _bucket(by_folder, folder_path, name=folder_path.rstrip("/").split("/")[-1] or "/")

        for bucket in (root, folder):
            bucket["total"] += 1
            if status in bucket:
                bucket[status] += 1
            else:
                bucket[status] = bucket.get(status, 0) + 1
            if skip_key:
                bucket["top_skip_reasons"][skip_key] += 1
            if err_key:
                bucket["top_error_reasons"][err_key] += 1

    def _finalize(store: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in store.values():
            skips = row.pop("top_skip_reasons")
            errors = row.pop("top_error_reasons")
            processed = int(row.get("processed") or 0)
            total = int(row.get("total") or 0)
            row["top_skip_reasons"] = _rank_counter(skips)
            row["top_error_reasons"] = _rank_counter(errors)
            row["success_rate_pct"] = round(100.0 * processed / total, 1) if total else 0.0
            # Search-ready ≈ processed (embeddings may still backfill via maintenance).
            row["search_ready"] = processed
            out.append(row)
        out.sort(key=lambda r: (-int(r["total"]), str(r.get("name") or "")))
        return out

    total = int(overall.get("total") or 0)
    processed = int(overall.get("processed") or 0)
    return {
        "totals": {
            "total": total,
            "processed": processed,
            "processing": int(overall.get("processing") or 0),
            "pending": int(overall.get("pending") or 0),
            "error": int(overall.get("error") or 0),
            "skipped": int(overall.get("skipped") or 0),
            "archived": int(overall.get("archived") or 0),
            "success_rate_pct": round(100.0 * processed / total, 1) if total else 0.0,
            "search_ready": processed,
        },
        "top_skip_reasons": _rank_counter(overall_skips),
        "top_error_reasons": _rank_counter(overall_errors),
        "by_root_folder": _finalize(by_root),
        "by_folder": _finalize(by_folder)[:200],  # cap payload; deepest folders still in tree API
    }
