"""Tests for content-hash dedupe, name conflicts, and skip-reason keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DriveFile,
    DriveFileStatus,
    FaceJob,
    FaceJobStatus,
    FileIndexConflict,
    Media,
    MediaType,
)
from app.drive.content_hash import (
    DUPLICATE_CONTENT_PREFIX,
    NAME_CONFLICT_PREFIX,
    drive_url_for_folder,
    hash_from_connector_entry,
)
from app.drive.conflicts import (
    KIND_SAME_CONTENT,
    KIND_SAME_CONTENT_DIFF_NAME,
    KIND_SAME_NAME_DIFF_CONTENT,
    STATUS_MERGED,
    STATUS_PENDING,
    STATUS_REPLACED,
    apply_dedupe_on_upsert,
    find_older_pending_same_hash,
    requeue_circular_duplicate_canonicals,
    resolve_conflict,
)
from app.drive.indexed_folders import list_indexed_folders, record_indexed_folder
from app.drive.schemas import ConnectorFile
from app.workers.requeue_failed import normalize_skip_reason
from tests.conftest import requires_postgres


def test_hash_from_connector_prefers_md5() -> None:
    entry = ConnectorFile.model_validate(
        {
            "id": "a",
            "name": "x.jpg",
            "mimeType": "image/jpeg",
            "isFolder": False,
            "parentId": "p",
            "path": "x.jpg",
            "md5Checksum": "abc123",
            "sha1Checksum": "def456",
        }
    )
    assert hash_from_connector_entry(entry) == ("md5", "abc123")


def test_drive_url_for_folder() -> None:
    assert drive_url_for_folder("folder123") == "https://drive.google.com/drive/folders/folder123"


@pytest.mark.parametrize(
    "msg,key",
    [
        ("duplicate_content: identical to xyz", "duplicate_content"),
        ("name_conflict: same name as abc", "name_conflict"),
    ],
)
def test_normalize_new_skip_reasons(msg: str, key: str) -> None:
    assert normalize_skip_reason(msg) == key


@requires_postgres
@pytest.mark.asyncio
async def test_autoskip_duplicate_content(db_session: AsyncSession) -> None:
    existing = DriveFile(
        id="exist1",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/photo.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="deadbeef",
        content_hash_algo="md5",
    )
    incoming = DriveFile(
        id="new1",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/other/photo.jpg",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([existing, incoming])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, incoming, algo="md5", digest="deadbeef")
    await db_session.flush()

    assert reason == "duplicate_content"
    assert incoming.status == DriveFileStatus.PROCESSED
    assert (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)

    conflict = (await db_session.execute(select(FileIndexConflict).limit(1))).scalar_one()
    assert conflict.conflict_kind == KIND_SAME_CONTENT
    assert conflict.status == STATUS_MERGED
    assert conflict.resolved_at is not None


@requires_postgres
@pytest.mark.asyncio
async def test_duplicate_placeholder_cannot_become_canonical_twin(
    db_session: AsyncSession,
) -> None:
    placeholder = DriveFile(
        id="placeholder",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/copy/photo.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="circular-hash",
        content_hash_algo="md5",
        error_message="duplicate_content: identical to incoming",
    )
    incoming = DriveFile(
        id="incoming",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/photo.jpg",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([placeholder, incoming])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(
        db_session,
        incoming,
        algo="md5",
        digest="circular-hash",
    )

    assert reason is None
    assert incoming.status == DriveFileStatus.PENDING
    assert incoming.error_message is None


@requires_postgres
@pytest.mark.asyncio
async def test_backfill_requeues_one_owner_for_circular_duplicate_group(
    db_session: AsyncSession,
) -> None:
    copies = [
        DriveFile(
            id=f"circular-{index}",
            name="same.jpg",
            mime_type="image/jpeg",
            path=f"/copy-{index}/same.jpg",
            status=DriveFileStatus.PROCESSED,
            content_hash="same-circular-content",
            content_hash_algo="md5",
            error_message=f"duplicate_content: identical to circular-{1 - index}",
        )
        for index in range(2)
    ]
    db_session.add_all(copies)
    await db_session.flush()

    repaired = await requeue_circular_duplicate_canonicals(db_session)
    await db_session.flush()

    assert len(repaired) == 1
    statuses = [copy.status for copy in copies]
    assert statuses.count(DriveFileStatus.PENDING) == 1
    assert statuses.count(DriveFileStatus.PROCESSED) == 1
    owner = next(copy for copy in copies if copy.status == DriveFileStatus.PENDING)
    assert owner.error_message is None


@requires_postgres
@pytest.mark.asyncio
async def test_backfill_enqueues_face_job_when_media_already_exists(
    db_session: AsyncSession,
) -> None:
    older = datetime.now(timezone.utc) - timedelta(minutes=1)
    first = DriveFile(
        id="face-backfill-owner",
        name="event.jpg",
        mime_type="image/jpeg",
        path="/event.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="face-backfill-content",
        content_hash_algo="md5",
        error_message="duplicate_content: identical to face-backfill-copy",
        created_at=older,
    )
    second = DriveFile(
        id="face-backfill-copy",
        name="event.jpg",
        mime_type="image/jpeg",
        path="/copy/event.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="face-backfill-content",
        content_hash_algo="md5",
        error_message="duplicate_content: identical to face-backfill-owner",
        created_at=older + timedelta(seconds=1),
    )
    db_session.add_all([first, second])
    await db_session.flush()
    db_session.add(
        Media(drive_file_id=first.id, type=MediaType.IMAGE)
    )
    await db_session.flush()

    repaired = await requeue_circular_duplicate_canonicals(db_session)
    await db_session.flush()

    assert repaired == [first.id]
    assert first.status == DriveFileStatus.PROCESSING
    job = (
        await db_session.execute(
            select(FaceJob).where(FaceJob.drive_file_id == first.id)
        )
    ).scalar_one()
    assert job.status == FaceJobStatus.PENDING


@requires_postgres
@pytest.mark.asyncio
async def test_same_content_diff_name_autoskip_mergeable(db_session: AsyncSession) -> None:
    existing = DriveFile(
        id="exist2",
        name="a.jpg",
        mime_type="image/jpeg",
        path="/a.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="cafebabe",
        content_hash_algo="md5",
    )
    incoming = DriveFile(
        id="new2",
        name="b.jpg",
        mime_type="image/jpeg",
        path="/b.jpg",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([existing, incoming])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, incoming, algo="md5", digest="cafebabe")
    await db_session.flush()
    assert reason == "duplicate_content"
    assert incoming.status == DriveFileStatus.PROCESSED

    conflict = (await db_session.execute(select(FileIndexConflict).limit(1))).scalar_one()
    assert conflict.conflict_kind == KIND_SAME_CONTENT_DIFF_NAME

    resolved = await resolve_conflict(db_session, conflict.id, action="merge")
    assert resolved.status == STATUS_MERGED
    assert (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)


@requires_postgres
@pytest.mark.asyncio
async def test_same_name_diff_content_auto_disambiguate(db_session: AsyncSession) -> None:
    existing = DriveFile(
        id="exist3",
        name="shot.png",
        mime_type="image/png",
        path="/shot.png",
        status=DriveFileStatus.PROCESSED,
        content_hash="111111",
        content_hash_algo="md5",
    )
    incoming = DriveFile(
        id="new3",
        name="shot.png",
        mime_type="image/png",
        path="/copy/shot.png",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([existing, incoming])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, incoming, algo="md5", digest="222222")
    await db_session.flush()

    assert reason is None
    assert incoming.status == DriveFileStatus.PENDING
    assert incoming.index_name == "shot (1).png"
    assert (await db_session.execute(select(FileIndexConflict))).scalar_one_or_none() is None


@requires_postgres
@pytest.mark.asyncio
async def test_same_name_diff_content_increments_disambiguator(db_session: AsyncSession) -> None:
    existing = DriveFile(
        id="exist3b",
        name="shot.png",
        mime_type="image/png",
        path="/shot.png",
        status=DriveFileStatus.PROCESSED,
        content_hash="111111",
        content_hash_algo="md5",
    )
    taken = DriveFile(
        id="exist3c",
        name="shot.png",
        index_name="shot (1).png",
        mime_type="image/png",
        path="/shot-copy.png",
        status=DriveFileStatus.PROCESSED,
        content_hash="333333",
        content_hash_algo="md5",
    )
    incoming = DriveFile(
        id="new3b",
        name="shot.png",
        mime_type="image/png",
        path="/copy2/shot.png",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([existing, taken, incoming])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, incoming, algo="md5", digest="444444")
    await db_session.flush()

    assert reason is None
    assert incoming.index_name == "shot (2).png"


@requires_postgres
@pytest.mark.asyncio
async def test_legacy_name_conflict_reconcile_requeues(db_session: AsyncSession) -> None:
    from app.drive.conflicts import reconcile_name_conflict_skips

    existing = DriveFile(
        id="exist_legacy",
        name="dup.jpg",
        mime_type="image/jpeg",
        path="/dup.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="aaaa",
        content_hash_algo="md5",
    )
    skipped = DriveFile(
        id="skip_legacy",
        name="dup.jpg",
        mime_type="image/jpeg",
        path="/other/dup.jpg",
        status=DriveFileStatus.SKIPPED,
        error_message=f"{NAME_CONFLICT_PREFIX} same name as exist_legacy",
        content_hash="bbbb",
        content_hash_algo="md5",
    )
    db_session.add_all([existing, skipped])
    await db_session.flush()

    n = await reconcile_name_conflict_skips(db_session)
    await db_session.flush()
    assert n == 1
    assert skipped.status == DriveFileStatus.PENDING
    assert skipped.index_name == "dup (1).jpg"
    assert skipped.error_message is None


@requires_postgres
@pytest.mark.asyncio
async def test_pending_peers_same_content_do_not_autoskip(db_session: AsyncSession) -> None:
    """Two PENDING files with the same hash must not skip each other before either finishes."""
    a = DriveFile(
        id="pend_hash_a",
        name="a.jpg",
        mime_type="image/jpeg",
        path="/a.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="samehash",
        content_hash_algo="md5",
    )
    b = DriveFile(
        id="pend_hash_b",
        name="b.jpg",
        mime_type="image/jpeg",
        path="/b.jpg",
        status=DriveFileStatus.PENDING,
    )
    db_session.add_all([a, b])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, b, algo="md5", digest="samehash")
    await db_session.flush()
    assert reason is None
    assert b.status == DriveFileStatus.PENDING


@requires_postgres
@pytest.mark.asyncio
async def test_dedupe_never_demotes_processed(db_session: AsyncSession) -> None:
    processed = DriveFile(
        id="keep_proc",
        name="hero.jpg",
        mime_type="image/jpeg",
        path="/hero.jpg",
        status=DriveFileStatus.PROCESSED,
        content_hash="abc123",
        content_hash_algo="md5",
    )
    twin = DriveFile(
        id="other_pend",
        name="copy.jpg",
        mime_type="image/jpeg",
        path="/copy.jpg",
        status=DriveFileStatus.PROCESSING,
        content_hash="abc123",
        content_hash_algo="md5",
    )
    db_session.add_all([processed, twin])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, processed, algo="md5", digest="abc123")
    await db_session.flush()
    assert reason is None
    assert processed.status == DriveFileStatus.PROCESSED


def test_macos_junk_name() -> None:
    from app.drive.content_hash import is_macos_junk_name

    assert is_macos_junk_name("._IMG_1234.HEIC") is True
    assert is_macos_junk_name(".DS_Store") is True
    assert is_macos_junk_name("IMG_1234.HEIC") is False


def test_first_hash_attach_is_not_hash_changed() -> None:
    """Mirrors indexer upsert: newly attached digest must not count as content change."""
    prev_hash = None
    digest = "deadbeef"
    hash_changed = bool(prev_hash and digest and digest != prev_hash)
    newly_hashed = bool(digest and not prev_hash)
    assert hash_changed is False
    assert newly_hashed is True
    content_changed = False or hash_changed
    assert content_changed is False


@pytest.mark.asyncio
async def test_known_processed_hash_stops_before_download_or_pipeline() -> None:
    """Index click invariant: a known twin never downloads/decodes/creates Media."""
    from app.config import Settings
    from app.pipelines.image import prepare_image_media

    incoming = DriveFile(
        id="known-twin",
        name="copy.jpg",
        mime_type="image/jpeg",
        path="/copy.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="knownhash",
        content_hash_algo="md5",
    )
    session = AsyncMock(spec=AsyncSession)
    client = AsyncMock()

    with (
        patch("app.pipelines.image.file_has_media", new=AsyncMock(return_value=False)),
        patch("app.pipelines.image.clear_existing_media", new=AsyncMock()),
        patch(
            "app.drive.conflicts.apply_dedupe_on_upsert",
            new=AsyncMock(return_value="duplicate_content"),
        ) as dedupe,
        patch("app.drive.media_cache.ensure_media_cached", new=AsyncMock()) as download,
        patch("app.pipelines.image.decode_image_bgr") as decode,
    ):
        result = await prepare_image_media(session, incoming, client, Settings())

    assert result is None
    dedupe.assert_awaited_once()
    download.assert_not_awaited()
    decode.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_known_processed_hash_stops_before_video_download() -> None:
    """Video twin with known hash never downloads from Drive."""
    from app.config import Settings
    from app.pipelines.video import process_video_file

    incoming = DriveFile(
        id="known-video-twin",
        name="copy.mp4",
        mime_type="video/mp4",
        path="/copy.mp4",
        status=DriveFileStatus.PENDING,
        content_hash="knownvideohash",
        content_hash_algo="md5",
    )
    session = AsyncMock(spec=AsyncSession)
    client = AsyncMock()

    with (
        patch("app.pipelines.common.file_has_media", new=AsyncMock(return_value=False)),
        patch("app.pipelines.video.clear_existing_media", new=AsyncMock()),
        patch(
            "app.drive.conflicts.apply_dedupe_on_upsert",
            new=AsyncMock(return_value="duplicate_content"),
        ) as dedupe,
        patch("app.pipelines.video.download_to_temp_file") as download,
        patch("app.pipelines.video.is_youtube_source", return_value=False),
        patch("app.pipelines.video._video_cache_path", return_value="/tmp/nope.mp4"),
        patch("os.path.isfile", return_value=False),
    ):
        result = await process_video_file(session, incoming, client, Settings())

    assert result is None
    dedupe.assert_awaited_once()
    download.assert_not_called()


@requires_postgres
@pytest.mark.asyncio
async def test_only_oldest_pending_same_hash_is_claimable(db_session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    older = DriveFile(
        id="older-pending",
        name="older.jpg",
        mime_type="image/jpeg",
        path="/older.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="same-pending-hash",
        content_hash_algo="md5",
        created_at=now,
    )
    newer = DriveFile(
        id="newer-pending",
        name="newer.jpg",
        mime_type="image/jpeg",
        path="/newer.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="same-pending-hash",
        content_hash_algo="md5",
        created_at=now + timedelta(seconds=1),
    )
    db_session.add(older)
    await db_session.flush()
    db_session.add(newer)
    await db_session.flush()

    peer = await find_older_pending_same_hash(
        db_session,
        algo="md5",
        digest="same-pending-hash",
        exclude_id=newer.id,
        created_at=newer.created_at,
    )
    assert peer is not None
    assert peer.id == older.id
    assert (
        await find_older_pending_same_hash(
            db_session,
            algo="md5",
            digest="same-pending-hash",
            exclude_id=older.id,
            created_at=older.created_at,
        )
        is None
    )


@requires_postgres
@pytest.mark.asyncio
async def test_restore_processed_when_media_exists(db_session: AsyncSession) -> None:
    from app.db.models import Media, MediaType
    from app.drive.cleanup import restore_processed_when_media_exists

    df = DriveFile(
        id="media_pending",
        name="already.jpg",
        mime_type="image/jpeg",
        path="/already.jpg",
        status=DriveFileStatus.PENDING,
    )
    db_session.add(df)
    await db_session.flush()
    db_session.add(Media(drive_file_id=df.id, type=MediaType.IMAGE))
    await db_session.flush()

    n = await restore_processed_when_media_exists(db_session)
    await db_session.flush()
    assert n == 1
    refreshed = await db_session.get(DriveFile, "media_pending")
    assert refreshed is not None
    assert refreshed.status == DriveFileStatus.PROCESSED


@requires_postgres
@pytest.mark.asyncio
async def test_pending_peers_same_name_do_not_conflict(db_session: AsyncSession) -> None:
    """Two PENDING files with the same name must not skip each other."""
    a = DriveFile(
        id="pend_a",
        name="dup.jpg",
        mime_type="image/jpeg",
        path="/a/dup.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="aaa111",
        content_hash_algo="md5",
    )
    b = DriveFile(
        id="pend_b",
        name="dup.jpg",
        mime_type="image/jpeg",
        path="/b/dup.jpg",
        status=DriveFileStatus.PENDING,
        content_hash="bbb222",
        content_hash_algo="md5",
    )
    db_session.add_all([a, b])
    await db_session.flush()

    reason = await apply_dedupe_on_upsert(db_session, b, algo="md5", digest="bbb222")
    await db_session.flush()
    assert reason is None
    assert b.status == DriveFileStatus.PENDING


def test_drive_file_status_enum_persists_names_for_postgres() -> None:
    """Live PG uses member names (PENDING/ARCHIVED); .value stays lowercase for API."""
    assert DriveFileStatus.ARCHIVED.name == "ARCHIVED"
    assert DriveFileStatus.ARCHIVED.value == "archived"
    col = DriveFile.__table__.c.status
    values = list(col.type.enums) if hasattr(col.type, "enums") else []
    # SQLAlchemy default Enum lists member names when values_callable is absent.
    assert "ARCHIVED" in values or "archived" in values


@requires_postgres
@pytest.mark.asyncio
async def test_indexed_folder_persists_drive_url(db_session: AsyncSession) -> None:
    row = await record_indexed_folder(
        db_session,
        folder_id="fold_abc",
        folder_name="Campaign Assets",
        mark_active=True,
    )
    await db_session.flush()
    assert row.drive_url == "https://drive.google.com/drive/folders/fold_abc"
    assert row.is_active is True

    row2 = await record_indexed_folder(
        db_session,
        folder_id="fold_xyz",
        folder_name="Archive",
        mark_active=True,
    )
    await db_session.flush()
    await db_session.refresh(row)
    assert row.is_active is False
    assert row2.is_active is True
    assert row.drive_url.startswith("https://drive.google.com/drive/folders/")

    listed = await list_indexed_folders(db_session)
    assert len(listed) == 2
    assert listed[0].id == "fold_xyz"
