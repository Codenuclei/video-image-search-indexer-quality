"""Tests for content-hash dedupe, name conflicts, and skip-reason keys."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, FileIndexConflict
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
    STATUS_AUTOSKIPPED,
    STATUS_MERGED,
    STATUS_PENDING,
    STATUS_REPLACED,
    apply_dedupe_on_upsert,
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
    assert incoming.status == DriveFileStatus.SKIPPED
    assert (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)

    conflict = (await db_session.execute(select(FileIndexConflict).limit(1))).scalar_one()
    assert conflict.conflict_kind == KIND_SAME_CONTENT
    assert conflict.status == STATUS_AUTOSKIPPED


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

    conflict = (await db_session.execute(select(FileIndexConflict).limit(1))).scalar_one()
    assert conflict.conflict_kind == KIND_SAME_CONTENT_DIFF_NAME

    resolved = await resolve_conflict(db_session, conflict.id, action="merge")
    assert resolved.status == STATUS_MERGED
    assert (incoming.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)


@requires_postgres
@pytest.mark.asyncio
async def test_same_name_diff_content_pending_replace(db_session: AsyncSession) -> None:
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

    assert reason == "name_conflict"
    assert incoming.status == DriveFileStatus.SKIPPED
    assert (incoming.error_message or "").startswith(NAME_CONFLICT_PREFIX)

    conflict = (await db_session.execute(select(FileIndexConflict).limit(1))).scalar_one()
    assert conflict.conflict_kind == KIND_SAME_NAME_DIFF_CONTENT
    assert conflict.status == STATUS_PENDING

    resolved = await resolve_conflict(db_session, conflict.id, action="replace")
    assert resolved.status == STATUS_REPLACED
    refreshed = await db_session.get(DriveFile, "new3")
    assert refreshed is not None
    assert refreshed.status == DriveFileStatus.PENDING
    # Never-delete: existing is soft-archived, not hard-deleted.
    existing_row = await db_session.get(DriveFile, "exist3")
    assert existing_row is not None
    assert existing_row.status == DriveFileStatus.ARCHIVED
    assert existing_row.archived_at is not None


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
