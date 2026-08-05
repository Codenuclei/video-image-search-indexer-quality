"""Unit tests for append-only Qdrant → Postgres recovery (no network)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import DriveFileStatus, MediaType
from app.qdrant.recover import (
    CollectionInventory,
    RecoverFromQdrantResult,
    _infer_media_type,
    recover_from_qdrant,
)


def test_infer_media_type_prefers_mime() -> None:
    row = SimpleNamespace(mime_type="image/jpeg")
    assert _infer_media_type(row, from_images=True, from_frames=False) == MediaType.IMAGE
    row = SimpleNamespace(mime_type="video/mp4")
    assert _infer_media_type(row, from_images=False, from_frames=True) == MediaType.VIDEO


def test_result_to_dict_bounds_orphans() -> None:
    r = RecoverFromQdrantResult(
        dry_run=True,
        images={},
        video_frames={},
        captions={},
        orphaned_image_ids=[f"i{i}" for i in range(60)],
        orphaned_video_ids=["v1"],
    )
    d = r.to_dict()
    assert d["orphaned_image_ids"]["count"] == 60
    assert len(d["orphaned_image_ids"]["sample"]) == 50
    assert d["orphaned_video_ids"]["count"] == 1


@pytest.mark.asyncio
async def test_recover_marks_skipped_with_vectors_dry_run() -> None:
    img = CollectionInventory(
        collection="dfi_images",
        points=2,
        unique_drive_file_ids=2,
        drive_file_ids={"img1", "orphan_img"},
    )
    frames = CollectionInventory(
        collection="dfi_video_frames",
        points=3,
        unique_drive_file_ids=1,
        drive_file_ids={"vid1"},
        max_timestamp={"vid1": 12.5},
        frame_counts={"vid1": 3},
    )
    caps = CollectionInventory(collection="dfi_image_captions", points=0)

    skipped_img = SimpleNamespace(
        id="img1",
        mime_type="image/jpeg",
        status=DriveFileStatus.SKIPPED,
        error_message="indexing paused",
    )
    skipped_vid = SimpleNamespace(
        id="vid1",
        mime_type="video/mp4",
        status=DriveFileStatus.SKIPPED,
        error_message="indexing paused",
    )
    folder = SimpleNamespace(
        id="folder1",
        mime_type="application/vnd.google-apps.folder",
        status=DriveFileStatus.SKIPPED,
        error_message="folder_marker",
    )

    session = AsyncMock()
    # First execute → drive_files, second → media
    drive_result = MagicMock()
    drive_result.scalars.return_value.all.return_value = [skipped_img, skipped_vid, folder]
    media_result = MagicMock()
    media_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[drive_result, media_result])
    session.commit = AsyncMock()
    session.add = MagicMock()

    result = await recover_from_qdrant(
        session,
        dry_run=True,
        create_orphaned_stubs=False,
        image_inv=img,
        frame_inv=frames,
        caption_inv=caps,
    )
    assert result.linked_images == 1
    assert result.linked_videos == 1
    assert result.status_marked_processed == 2
    assert result.media_created == 2
    assert result.orphaned_image_ids == ["orphan_img"]
    assert result.stubs_created == 0
    session.commit.assert_not_called()
    assert any("captions cannot be recovered" in n.lower() for n in result.notes)


@pytest.mark.asyncio
async def test_recover_apply_updates_status_and_media() -> None:
    img = CollectionInventory(
        collection="dfi_images",
        points=1,
        unique_drive_file_ids=1,
        drive_file_ids={"img1"},
    )
    frames = CollectionInventory(
        collection="dfi_video_frames",
        points=0,
        unique_drive_file_ids=0,
        drive_file_ids=set(),
    )
    caps = CollectionInventory(collection="dfi_image_captions", points=0)

    row = SimpleNamespace(
        id="img1",
        mime_type="image/png",
        status=DriveFileStatus.SKIPPED,
        error_message="was skipped",
        last_synced_at=None,
    )
    session = AsyncMock()
    drive_result = MagicMock()
    drive_result.scalars.return_value.all.return_value = [row]
    media_result = MagicMock()
    media_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=[drive_result, media_result])
    session.commit = AsyncMock()
    session.add = MagicMock()

    result = await recover_from_qdrant(
        session,
        dry_run=False,
        image_inv=img,
        frame_inv=frames,
        caption_inv=caps,
    )
    assert result.status_marked_processed == 1
    assert result.media_created == 1
    assert row.status == DriveFileStatus.PROCESSED
    assert row.error_message.startswith("recovered_from_qdrant")
    session.add.assert_called()
    session.commit.assert_awaited()
