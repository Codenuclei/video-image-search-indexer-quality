"""Qwen identify lane: isolated Postgres writes, original photos, temp cleanup."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.objects.identify_tags import (
    QWEN_IDENTIFY_MODEL_VERSION,
    parse_identify_output,
    persist_rows,
)
from app.objects.taxonomy import OBJECT_MODEL_VERSION
from app.workers.identify_queue import (
    build_identify_payload,
    encode_identify_jpeg,
    payload_contains_drive_url,
    persist_identify_labels,
    process_identify_job,
    wipe_identify_working_dir,
    write_identify_jpeg,
)
from tests.conftest import requires_postgres


def _tiny_jpeg(*, edge: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (edge, edge), (12, 64, 200)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_identify_payload_is_jpeg_data_url_not_drive() -> None:
    payload = build_identify_payload(_tiny_jpeg(), model="Qwen/Qwen3-VL-8B-Instruct")
    blob = str(payload)
    assert "data:image/jpeg;base64," in blob
    assert "drive.google.com" not in blob
    assert payload_contains_drive_url(payload) is False
    content = payload["messages"][0]["content"]
    url = content[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert "http" not in url


def test_payload_contains_drive_url_detects_http_drive() -> None:
    bad = {
        "messages": [
            {
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://drive.google.com/uc?id=abc"},
                    },
                ]
            }
        ]
    }
    assert payload_contains_drive_url(bad) is True


def test_encode_identify_jpeg_stays_in_memory() -> None:
    data = encode_identify_jpeg(
        _tiny_jpeg(edge=2000),
        file_name="photo.jpg",
        max_edge=256,
        quality=85,
        max_bytes=200_000,
    )
    assert data.startswith(b"\xff\xd8")
    assert len(data) <= 200_000
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= 256


def test_write_identify_jpeg_resizes_and_caps(tmp_path: Path) -> None:
    dest = tmp_path / "out.jpg"
    write_identify_jpeg(
        _tiny_jpeg(edge=2000),
        dest,
        file_name="photo.jpg",
        max_edge=256,
        quality=85,
        max_bytes=200_000,
    )
    assert dest.is_file()
    with Image.open(dest) as img:
        assert max(img.size) <= 256
    assert dest.stat().st_size <= 200_000


def test_wipe_identify_working_dir_removes_files(tmp_path: Path) -> None:
    leftover = tmp_path / "abc.jpg"
    leftover.write_bytes(b"jpeg")
    removed = wipe_identify_working_dir(tmp_path)
    assert removed == 1
    assert not leftover.exists()


def test_persist_rows_use_qwen_model_version_only() -> None:
    parsed = parse_identify_output(
        "OBJECTS\nceremonial cheque | check\nACTIONS\ngiving ceremonial cheque | handing cheque\n"
    )
    rows = persist_rows(parsed)
    assert rows
    assert {row["model_version"] for row in rows} == {QWEN_IDENTIFY_MODEL_VERSION}
    assert OBJECT_MODEL_VERSION not in {row["model_version"] for row in rows}


@pytest.mark.asyncio
async def test_process_skips_download_when_lane_paused() -> None:
    import asyncio

    downloaded: list[int] = []

    async def boom(*_args, **_kwargs):
        downloaded.append(1)
        raise AssertionError("must not download when paused")

    inner = AsyncMock()
    inner.rollback = AsyncMock()
    inner.__aenter__.return_value = inner
    inner.__aexit__.return_value = False
    session_factory = MagicMock(return_value=inner)

    with (
        patch("app.workers.identify_queue.release_identify_job", new_callable=AsyncMock) as release,
        patch("app.pipelines.common.download_to_memory", new=boom),
    ):
        await process_identify_job(
            session_factory,
            1,
            gpu_sem=asyncio.Semaphore(1),
            decode_sem=asyncio.Semaphore(1),
            working_dir=Path("/tmp"),
            http=AsyncMock(),
            lane_enabled=lambda: False,
        )
    assert downloaded == []
    release.assert_awaited()


@requires_postgres
@pytest.mark.asyncio
async def test_identify_persist_does_not_touch_taxonomy_tables(db_session) -> None:
    from sqlalchemy import func, select

    from app.db.models import (
        DriveFile,
        DriveFileStatus,
        Media,
        MediaIdentifyLabel,
        MediaObjectLabel,
        MediaType,
        ObjectJob,
        ObjectJobStatus,
    )

    db_session.add(
        DriveFile(
            id="identify-iso",
            name="iso.jpg",
            path="/iso.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSED,
        )
    )
    await db_session.flush()
    media = Media(drive_file_id="identify-iso", type=MediaType.IMAGE)
    db_session.add(media)
    await db_session.flush()
    db_session.add(
        MediaObjectLabel(
            media_id=media.id,
            canonical_label="trophy",
            category="object",
            confidence=0.9,
            evidence_source="caption",
            evidence_text="trophy",
            hit_count=1,
            taxonomy_version="objects-v1",
            model_version=OBJECT_MODEL_VERSION,
        )
    )
    db_session.add(
        ObjectJob(
            drive_file_id="identify-iso",
            model_version=OBJECT_MODEL_VERSION,
            status=ObjectJobStatus.DONE,
            label_count=1,
        )
    )
    await db_session.commit()

    tax_labels = await db_session.scalar(select(func.count(MediaObjectLabel.id)))
    tax_jobs = await db_session.scalar(select(func.count(ObjectJob.id)))

    parsed = parse_identify_output(
        "OBJECTS\nceremonial cheque | check\nACTIONS\ngiving ceremonial cheque\n"
    )
    written = await persist_identify_labels(db_session, media.id, parsed)
    await db_session.commit()

    assert written >= 1
    identify_count = await db_session.scalar(select(func.count(MediaIdentifyLabel.id)))
    assert identify_count == written
    assert await db_session.scalar(select(func.count(MediaObjectLabel.id))) == tax_labels
    assert await db_session.scalar(select(func.count(ObjectJob.id))) == tax_jobs
    sources = set(
        (
            await db_session.execute(select(MediaIdentifyLabel.evidence_source))
        ).scalars()
    )
    assert "qwen_identify" in sources or "qwen_action" in sources
    versions = set(
        (await db_session.execute(select(MediaIdentifyLabel.model_version))).scalars()
    )
    assert versions == {QWEN_IDENTIFY_MODEL_VERSION}


@requires_postgres
@pytest.mark.asyncio
async def test_identify_enqueue_does_not_create_object_jobs(db_session) -> None:
    from sqlalchemy import func, select

    from app.db.models import (
        DriveFile,
        DriveFileStatus,
        IdentifyJob,
        Media,
        MediaType,
        ObjectJob,
    )
    from app.workers.identify_queue import enqueue_identify_job

    db_session.add(
        DriveFile(
            id="identify-enq",
            name="enq.jpg",
            path="/enq.jpg",
            mime_type="image/jpeg",
            status=DriveFileStatus.PROCESSED,
        )
    )
    await db_session.flush()
    db_session.add(Media(drive_file_id="identify-enq", type=MediaType.IMAGE))
    await db_session.flush()

    job = await enqueue_identify_job(db_session, "identify-enq")
    await db_session.commit()
    assert job is not None
    assert await db_session.scalar(select(func.count(IdentifyJob.id))) == 1
    assert await db_session.scalar(select(func.count(ObjectJob.id))) == 0
