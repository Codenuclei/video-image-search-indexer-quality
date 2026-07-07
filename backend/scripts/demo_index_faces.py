"""Quick local demo: index a few LFW face photos through the real pipeline into Postgres."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from contextlib import asynccontextmanager

from app.config import Settings
from app.db.models import DriveFile, DriveFileStatus, Face, FaceCluster, Person
from app.db.session import get_session_factory
from app.faces.engine import get_face_engine
from app.matching.service import name_cluster
from app.pipelines.image import process_image_file

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "faces"


class _FileResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def aiter_bytes(self, chunk_size: int = 1024 * 256):
        yield self._content


class _LocalFileDriveClient:
    def __init__(self, path_by_id: dict[str, Path]) -> None:
        self._path_by_id = path_by_id

    @asynccontextmanager
    async def stream_file_content(self, file_id: str):
        yield _FileResponse(self._path_by_id[file_id].read_bytes())


async def index_one(session, filename: str) -> None:
    drive_file = DriveFile(
        id=f"demo-{uuid.uuid4().hex}",
        name=filename,
        mime_type="image/jpeg",
        path=f"/demo/{filename}",
        status=DriveFileStatus.PROCESSING,
    )
    session.add(drive_file)
    await session.flush()
    client = _LocalFileDriveClient({drive_file.id: FIXTURES / filename})
    settings = Settings(thumbnail_dir="./data/thumbnails", temp_dir="./data/tmp")
    await process_image_file(session, drive_file, client, settings, get_face_engine())
    drive_file.status = DriveFileStatus.PROCESSED
    await session.commit()
    print(f"indexed {filename}")


async def main() -> None:
    factory = get_session_factory()
    async with factory() as session:
        for name in ["person_a_1.jpg", "person_a_2.jpg", "person_b_1.jpg"]:
            await index_one(session, name)

        # Name the first unknown cluster as a demo person
        cluster = (await session.execute(FaceCluster.__table__.select().limit(1))).first()
        if cluster:
            person = await name_cluster(session, cluster.id, "Person A")
            await session.commit()
            print(f"named cluster -> {person.name}")

        faces = (await session.execute(Face.__table__.select())).all()
        persons = (await session.execute(Person.__table__.select())).all()
        clusters = (await session.execute(FaceCluster.__table__.select())).all()
        print(f"faces={len(faces)} persons={len(persons)} clusters={len(clusters)}")


if __name__ == "__main__":
    asyncio.run(main())
