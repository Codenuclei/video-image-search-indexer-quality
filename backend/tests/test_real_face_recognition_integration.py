"""
The heaviest but most convincing test in the suite: runs the *real* InsightFace
model (RetinaFace + ArcFace, CPU) against real photographs of real people
(a handful of images from the LFW dataset, see tests/fixtures/faces/), through
the real image pipeline, into a real Postgres+pgvector database — with nothing
faked except the Drive Connector HTTP call (we just hand it local file bytes).

This proves the whole chain end-to-end: detection -> embedding -> pgvector
matching -> online clustering actually recognizes the same person across
different photos and tells different people apart.

Skipped automatically if either Postgres or the `insightface` package (and its
downloaded model weights) aren't available, since both are heavy optional
dependencies not needed for the rest of the suite.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.config import Settings
from app.db.models import DriveFile, DriveFileStatus, Face
from app.pipelines.image import process_image_file
from tests.conftest import requires_postgres

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "faces"

try:
    from app.faces.engine import FaceEngine

    _engine = FaceEngine()
    _engine._ensure_loaded()  # noqa: SLF001 - force model load now so we can skip cleanly if unavailable
    _INSIGHTFACE_READY = True
except Exception:  # noqa: BLE001 - insightface not installed or model weights unavailable
    _engine = None
    _INSIGHTFACE_READY = False

requires_insightface = pytest.mark.skipif(
    not _INSIGHTFACE_READY,
    reason="insightface (and its downloaded model weights) are not available in this environment",
)

_REQUIRED_FIXTURES = ["person_a_1.jpg", "person_a_2.jpg", "person_a_3.jpg", "person_b_1.jpg"]
requires_fixtures = pytest.mark.skipif(
    not all((FIXTURES_DIR / name).exists() for name in _REQUIRED_FIXTURES),
    reason=(
        f"Sample face photos not found in {FIXTURES_DIR} — this fixture set is gitignored "
        "(copyrighted dataset images); drop a few real face photos there to run this test "
        "(see this file's docstring for the expected filenames)."
    ),
)


class _LocalFileDriveClient:
    """Fakes only the Drive Connector HTTP hop — hands back real bytes from disk."""

    def __init__(self, path_by_id: dict[str, Path]) -> None:
        self._path_by_id = path_by_id

    @asynccontextmanager
    async def stream_file_content(self, file_id: str):
        yield _FileResponse(self._path_by_id[file_id].read_bytes())


class _FileResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def aiter_bytes(self, chunk_size: int = 1024 * 256):
        yield self._content


async def _index_fixture(session, settings, filename: str) -> tuple[DriveFile, list]:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name=filename,
        mime_type="image/jpeg",
        path=f"/{filename}",
        status=DriveFileStatus.PROCESSING,
    )
    session.add(drive_file)
    await session.flush()

    client = _LocalFileDriveClient({drive_file.id: FIXTURES_DIR / filename})
    media = await process_image_file(session, drive_file, client, settings, _engine)
    await session.commit()

    faces = (await session.execute(Face.__table__.select().where(Face.media_id == media.id))).all()
    return drive_file, faces


@requires_postgres
@requires_insightface
@requires_fixtures
@pytest.mark.asyncio
async def test_same_person_across_different_photos_clusters_together(db_session, tmp_path):
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbnails"))

    _, faces_a1 = await _index_fixture(db_session, settings, "person_a_1.jpg")
    _, faces_a2 = await _index_fixture(db_session, settings, "person_a_2.jpg")

    assert len(faces_a1) >= 1, "Expected InsightFace to detect a face in person_a_1.jpg"
    assert len(faces_a2) >= 1, "Expected InsightFace to detect a face in person_a_2.jpg"

    face_a1 = await db_session.get(Face, faces_a1[0].id)
    face_a2 = await db_session.get(Face, faces_a2[0].id)

    assert face_a1.cluster_id is not None
    assert face_a1.cluster_id == face_a2.cluster_id, (
        "Two different photos of the same real person should land in the same unknown cluster"
    )


@requires_postgres
@requires_insightface
@requires_fixtures
@pytest.mark.asyncio
async def test_different_people_do_not_share_a_cluster(db_session, tmp_path):
    settings = Settings(thumbnail_dir=str(tmp_path / "thumbnails"))

    _, faces_a = await _index_fixture(db_session, settings, "person_a_1.jpg")
    _, faces_b = await _index_fixture(db_session, settings, "person_b_1.jpg")

    assert len(faces_a) >= 1
    assert len(faces_b) >= 1

    face_a = await db_session.get(Face, faces_a[0].id)
    face_b = await db_session.get(Face, faces_b[0].id)

    assert face_a.cluster_id != face_b.cluster_id, "Two different real people should not end up in the same cluster"


@requires_postgres
@requires_insightface
@requires_fixtures
@pytest.mark.asyncio
async def test_naming_then_third_photo_of_same_person_is_auto_tagged(db_session, tmp_path):
    from app.matching.service import name_cluster

    settings = Settings(thumbnail_dir=str(tmp_path / "thumbnails"))

    _, faces_a1 = await _index_fixture(db_session, settings, "person_a_1.jpg")
    face_a1 = await db_session.get(Face, faces_a1[0].id)
    assert face_a1.cluster_id is not None

    person = await name_cluster(db_session, face_a1.cluster_id, "George")
    await db_session.commit()

    _, faces_a3 = await _index_fixture(db_session, settings, "person_a_3.jpg")
    face_a3 = await db_session.get(Face, faces_a3[0].id)

    assert face_a3.person_id == person.id, "A third photo of the same person should auto-tag to the named person"
    assert face_a3.cluster_id is None
