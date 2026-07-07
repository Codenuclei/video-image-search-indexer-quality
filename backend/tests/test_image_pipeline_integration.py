"""
Integration test for the image pipeline end-to-end (download -> detect -> embed ->
match -> persist), run against a real Postgres+pgvector instance. The Drive
Connector HTTP call and the InsightFace model are faked at their boundaries (no
network access to a real connector and no multi-hundred-MB model download are
needed to prove the pipeline's own wiring and DB persistence are correct); the
matching/clustering logic they call into is real and exercised against Postgres.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.config import Settings
from app.db.models import DriveFile, DriveFileStatus, Face, FaceCluster, FaceEmbedding
from app.faces.engine import DetectedFace
from app.pipelines.image import process_image_file
from tests.conftest import requires_postgres

EMBEDDING_DIM = 512


class _FakeFaceEngine:
    def __init__(self, detections: list[DetectedFace]) -> None:
        self._detections = detections

    def detect_and_embed(self, image_bgr) -> list[DetectedFace]:
        return self._detections


class _FakeDriveClient:
    def __init__(self, content: bytes) -> None:
        self._content = content

    @asynccontextmanager
    async def stream_file_content(self, file_id: str):
        yield _FakeStreamResponse(self._content)


class _FakeStreamResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def aiter_bytes(self, chunk_size: int = 1024 * 256):
        yield self._content


def _one_hot_embedding(index: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[index] = 1.0
    return vec


def _make_detection(index: int) -> DetectedFace:
    return DetectedFace(
        bbox_x=1.0,
        bbox_y=2.0,
        bbox_width=30.0,
        bbox_height=40.0,
        confidence=0.95,
        embedding=_one_hot_embedding(index),
        thumbnail_jpeg=b"",  # skip real thumbnail I/O in this test
    )


async def _make_drive_file(session) -> DriveFile:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name="photo.jpg",
        mime_type="image/jpeg",
        path="/photo.jpg",
        status=DriveFileStatus.PROCESSING,
    )
    session.add(drive_file)
    await session.flush()
    return drive_file


@pytest.fixture
def fake_settings(tmp_path) -> Settings:
    return Settings(thumbnail_dir=str(tmp_path / "thumbnails"))


@requires_postgres
@pytest.mark.asyncio
async def test_process_image_file_persists_media_face_and_embedding(db_session, fake_settings, monkeypatch):
    import cv2
    import numpy as np

    fake_jpeg = cv2.imencode(".jpg", np.zeros((10, 10, 3), dtype=np.uint8))[1].tobytes()
    drive_file = await _make_drive_file(db_session)
    engine = _FakeFaceEngine([_make_detection(0)])
    client = _FakeDriveClient(fake_jpeg)

    media = await process_image_file(db_session, drive_file, client, fake_settings, engine)
    await db_session.commit()

    faces = (await db_session.execute(Face.__table__.select().where(Face.media_id == media.id))).all()
    assert len(faces) == 1

    embeddings = (
        await db_session.execute(FaceEmbedding.__table__.select().where(FaceEmbedding.face_id == faces[0].id))
    ).all()
    assert len(embeddings) == 1

    clusters = (await db_session.execute(FaceCluster.__table__.select())).all()
    assert len(clusters) == 1


@requires_postgres
@pytest.mark.asyncio
async def test_process_image_file_with_no_faces_still_creates_media(db_session, fake_settings):
    import cv2
    import numpy as np

    fake_jpeg = cv2.imencode(".jpg", np.zeros((10, 10, 3), dtype=np.uint8))[1].tobytes()
    drive_file = await _make_drive_file(db_session)
    engine = _FakeFaceEngine([])
    client = _FakeDriveClient(fake_jpeg)

    media = await process_image_file(db_session, drive_file, client, fake_settings, engine)
    await db_session.commit()

    faces = (await db_session.execute(Face.__table__.select().where(Face.media_id == media.id))).all()
    assert len(faces) == 0
    assert media.drive_file_id == drive_file.id


@requires_postgres
@pytest.mark.asyncio
async def test_process_image_file_raises_on_undecodable_image(db_session, fake_settings):
    drive_file = await _make_drive_file(db_session)
    engine = _FakeFaceEngine([])
    client = _FakeDriveClient(b"not a real image")

    with pytest.raises(ValueError):
        await process_image_file(db_session, drive_file, client, fake_settings, engine)
