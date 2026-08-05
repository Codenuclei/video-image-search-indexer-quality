"""Append-only recovery: re-link Postgres status/media from existing Qdrant points.

Does NOT delete Qdrant points, wipe collections, truncate Postgres, or call Gemini.
Captions cannot be recovered when ``dfi_image_captions`` is empty.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, Media, MediaType

logger = logging.getLogger(__name__)

_SCROLL_LIMIT = 256
_RECOVERED_NOTE = "recovered_from_qdrant"


@dataclass
class CollectionInventory:
    collection: str
    points: int = 0
    unique_drive_file_ids: int = 0
    drive_file_ids: set[str] = field(default_factory=set)
    # video frames: drive_file_id → max timestamp seen
    max_timestamp: dict[str, float] = field(default_factory=dict)
    # video frames: drive_file_id → frame point count
    frame_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RecoverFromQdrantResult:
    dry_run: bool
    images: dict[str, Any]
    video_frames: dict[str, Any]
    captions: dict[str, Any]
    linked_images: int = 0
    linked_videos: int = 0
    status_marked_processed: int = 0
    media_created: int = 0
    already_processed: int = 0
    skipped_folder_markers: int = 0
    orphaned_image_ids: list[str] = field(default_factory=list)
    orphaned_video_ids: list[str] = field(default_factory=list)
    stubs_created: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Keep orphan lists bounded in API responses.
        for key in ("orphaned_image_ids", "orphaned_video_ids"):
            ids = d[key]
            d[key] = {
                "count": len(ids),
                "sample": ids[:50],
            }
        return d


def _scroll_payloads(client: Any, collection: str) -> list[dict[str, Any]]:
    """Scroll all points; return payloads only (no vectors)."""
    payloads: list[dict[str, Any]] = []
    offset = None
    while True:
        records, next_offset = client.scroll(
            collection_name=collection,
            limit=_SCROLL_LIMIT,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for rec in records:
            payload = rec.payload or {}
            if isinstance(payload, dict):
                payloads.append(payload)
        if next_offset is None:
            break
        offset = next_offset
    return payloads


def inventory_images_sync(client: Any | None = None) -> CollectionInventory:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    name = settings.qdrant_images_collection
    q = client or make_qdrant_client(settings.qdrant_url, timeout=120)
    inv = CollectionInventory(collection=name)
    try:
        info = q.get_collection(name)
        inv.points = int(info.points_count or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not get collection info for %s: %s", name, exc)
        return inv
    for payload in _scroll_payloads(q, name):
        fid = payload.get("drive_file_id")
        if isinstance(fid, str) and fid:
            inv.drive_file_ids.add(fid)
    inv.unique_drive_file_ids = len(inv.drive_file_ids)
    return inv


def inventory_video_frames_sync(client: Any | None = None) -> CollectionInventory:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    name = settings.qdrant_collection
    q = client or make_qdrant_client(settings.qdrant_url, timeout=120)
    inv = CollectionInventory(collection=name)
    try:
        info = q.get_collection(name)
        inv.points = int(info.points_count or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not get collection info for %s: %s", name, exc)
        return inv
    for payload in _scroll_payloads(q, name):
        fid = payload.get("drive_file_id")
        if not isinstance(fid, str) or not fid:
            continue
        inv.drive_file_ids.add(fid)
        inv.frame_counts[fid] = inv.frame_counts.get(fid, 0) + 1
        ts = payload.get("timestamp")
        if isinstance(ts, (int, float)):
            prev = inv.max_timestamp.get(fid)
            if prev is None or float(ts) > prev:
                inv.max_timestamp[fid] = float(ts)
    inv.unique_drive_file_ids = len(inv.drive_file_ids)
    return inv


def inventory_captions_sync(client: Any | None = None) -> CollectionInventory:
    from app.config import get_settings
    from app.qdrant.client import make_qdrant_client

    settings = get_settings()
    name = settings.qdrant_image_captions_collection
    q = client or make_qdrant_client(settings.qdrant_url, timeout=120)
    inv = CollectionInventory(collection=name)
    try:
        info = q.get_collection(name)
        inv.points = int(info.points_count or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not get collection info for %s: %s", name, exc)
        return inv
    for payload in _scroll_payloads(q, name):
        fid = payload.get("drive_file_id")
        if isinstance(fid, str) and fid:
            inv.drive_file_ids.add(fid)
    inv.unique_drive_file_ids = len(inv.drive_file_ids)
    return inv


def _is_folder_marker(row: DriveFile) -> bool:
    return (row.error_message or "") == "folder_marker" or row.mime_type == "application/vnd.google-apps.folder"


def _infer_media_type(row: DriveFile, *, from_images: bool, from_frames: bool) -> MediaType:
    mime = (row.mime_type or "").lower()
    if mime.startswith("video/") or from_frames and not from_images:
        return MediaType.VIDEO
    if mime.startswith("image/") or from_images:
        return MediaType.IMAGE
    if from_frames:
        return MediaType.VIDEO
    return MediaType.IMAGE


async def recover_from_qdrant(
    session: AsyncSession,
    *,
    dry_run: bool = True,
    create_orphaned_stubs: bool = False,
    image_inv: CollectionInventory | None = None,
    frame_inv: CollectionInventory | None = None,
    caption_inv: CollectionInventory | None = None,
) -> RecoverFromQdrantResult:
    """Re-link Postgres ``drive_files`` / ``media`` to existing Qdrant embeddings.

    Append-only: never deletes Qdrant or Postgres rows. Does not regenerate
    embeddings or captions. Orphaned Qdrant points (no PG row) are reported;
    stubs are only created when ``create_orphaned_stubs`` is True (unsafe —
    payloads lack name/path/mime).
    """
    if image_inv is None:
        image_inv = await _to_thread(inventory_images_sync)
    if frame_inv is None:
        frame_inv = await _to_thread(inventory_video_frames_sync)
    if caption_inv is None:
        caption_inv = await _to_thread(inventory_captions_sync)

    result = RecoverFromQdrantResult(
        dry_run=dry_run,
        images={
            "collection": image_inv.collection,
            "points": image_inv.points,
            "unique_drive_file_ids": image_inv.unique_drive_file_ids,
        },
        video_frames={
            "collection": frame_inv.collection,
            "points": frame_inv.points,
            "unique_drive_file_ids": frame_inv.unique_drive_file_ids,
        },
        captions={
            "collection": caption_inv.collection,
            "points": caption_inv.points,
            "unique_drive_file_ids": caption_inv.unique_drive_file_ids,
            "recoverable": False,
            "note": (
                "Caption text/embeddings live only in dfi_image_captions. "
                "With 0 points, captions cannot be recovered from Qdrant — "
                "do not fake captions; re-run caption backfill if needed."
            ),
        },
    )
    if caption_inv.points == 0:
        result.notes.append(
            "dfi_image_captions is empty — captions cannot be recovered from Qdrant."
        )

    qdrant_ids = image_inv.drive_file_ids | frame_inv.drive_file_ids
    if not qdrant_ids:
        result.notes.append("No drive_file_id payloads found in image/frame collections.")
        return result

    rows = list((await session.execute(select(DriveFile))).scalars().all())
    by_id = {r.id: r for r in rows}
    media_rows = list((await session.execute(select(Media))).scalars().all())
    media_by_file = {m.drive_file_id: m for m in media_rows}

    now = datetime.now(timezone.utc)

    for fid in sorted(qdrant_ids):
        from_images = fid in image_inv.drive_file_ids
        from_frames = fid in frame_inv.drive_file_ids
        row = by_id.get(fid)

        if row is None:
            if from_images:
                result.orphaned_image_ids.append(fid)
            if from_frames:
                result.orphaned_video_ids.append(fid)
            if create_orphaned_stubs:
                # Payloads lack name/path/mime — only create when explicitly requested.
                media_type = MediaType.IMAGE if from_images and not from_frames else MediaType.VIDEO
                if from_images and from_frames:
                    media_type = MediaType.VIDEO
                stub_name = f"recovered-{fid}"
                stub_mime = "image/jpeg" if media_type == MediaType.IMAGE else "video/mp4"
                stub_path = f"/recovered/{fid}"
                if not dry_run:
                    stub = DriveFile(
                        id=fid,
                        name=stub_name,
                        mime_type=stub_mime,
                        path=stub_path,
                        status=DriveFileStatus.PROCESSED,
                        error_message=_RECOVERED_NOTE,
                        source="recovered_qdrant",
                        last_synced_at=now,
                    )
                    session.add(stub)
                    session.add(
                        Media(
                            drive_file_id=fid,
                            type=media_type,
                            duration_seconds=frame_inv.max_timestamp.get(fid),
                        )
                    )
                result.stubs_created += 1
                if from_images:
                    result.linked_images += 1
                if from_frames:
                    result.linked_videos += 1
                result.media_created += 1
                result.status_marked_processed += 1
            continue

        if _is_folder_marker(row):
            result.skipped_folder_markers += 1
            continue

        media = media_by_file.get(fid)
        media_type = _infer_media_type(row, from_images=from_images, from_frames=from_frames)
        needs_media = media is None
        needs_status = row.status != DriveFileStatus.PROCESSED

        if from_images:
            result.linked_images += 1
        if from_frames:
            result.linked_videos += 1

        if not needs_media and not needs_status:
            result.already_processed += 1
            continue

        if needs_status:
            result.status_marked_processed += 1
        if needs_media:
            result.media_created += 1

        if dry_run:
            continue

        if needs_status:
            row.status = DriveFileStatus.PROCESSED
            # Clear skip/error so Library treats the file as indexed; keep a trace.
            if row.error_message and row.error_message != _RECOVERED_NOTE:
                row.error_message = f"{_RECOVERED_NOTE}; was: {row.error_message[:400]}"
            else:
                row.error_message = _RECOVERED_NOTE
            row.last_synced_at = now

        if needs_media:
            new_media = Media(
                drive_file_id=fid,
                type=media_type,
                duration_seconds=frame_inv.max_timestamp.get(fid) if media_type == MediaType.VIDEO else None,
            )
            session.add(new_media)
            media_by_file[fid] = new_media
        elif media is not None and media.type == MediaType.VIDEO and media.duration_seconds is None:
            ts = frame_inv.max_timestamp.get(fid)
            if ts is not None:
                media.duration_seconds = ts

    if not dry_run and (
        result.status_marked_processed
        or result.media_created
        or result.stubs_created
    ):
        await session.commit()

    result.notes.append(
        "Orphaned Qdrant points lack name/path/mime in payload — stubs not created "
        "unless create_orphaned_stubs=true."
        if not create_orphaned_stubs
        else "Orphaned stubs use placeholder name/path/mime (source=recovered_qdrant)."
    )
    result.notes.append("Face embeddings in Postgres were left untouched.")
    return result


async def _to_thread(fn):
    import asyncio

    return await asyncio.to_thread(fn)
