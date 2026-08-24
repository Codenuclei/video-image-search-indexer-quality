"""Person-wide unique file / face counts for reverse face search badges."""

from __future__ import annotations

import uuid

import pytest

from app.db.models import (
    ClusterStatus,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Media,
    MediaType,
    Person,
)
from app.reid.face_search import _face_count_for_person, _file_count_for_face
from tests.conftest import requires_postgres


async def _media(session, *, name: str, media_type: MediaType) -> Media:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name=name,
        mime_type="image/jpeg" if media_type == MediaType.IMAGE else "video/mp4",
        path=f"/{name}",
        status=DriveFileStatus.PROCESSED,
    )
    session.add(drive_file)
    await session.flush()
    media = Media(drive_file_id=drive_file.id, type=media_type)
    session.add(media)
    await session.flush()
    return media


async def _face(session, media: Media, *, person_id: int | None = None, cluster_id: int | None = None) -> Face:
    face = Face(
        media_id=media.id,
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_width=10.0,
        bbox_height=10.0,
        detection_confidence=0.99,
        person_id=person_id,
        cluster_id=cluster_id,
    )
    session.add(face)
    await session.flush()
    return face


@requires_postgres
@pytest.mark.asyncio
async def test_file_count_and_face_count_span_merged_person_clusters(db_session):
    person = Person(name=f"Merged-{uuid.uuid4().hex[:8]}")
    db_session.add(person)
    await db_session.flush()

    cluster_a = FaceCluster(
        status=ClusterStatus.NAMED,
        person_id=person.id,
        member_count=2,
    )
    cluster_b = FaceCluster(
        status=ClusterStatus.NAMED,
        person_id=person.id,
        member_count=1,
    )
    db_session.add_all([cluster_a, cluster_b])
    await db_session.flush()

    img_a = await _media(db_session, name="a.jpg", media_type=MediaType.IMAGE)
    img_b = await _media(db_session, name="b.jpg", media_type=MediaType.IMAGE)
    vid = await _media(db_session, name="c.mp4", media_type=MediaType.VIDEO)

    # Two faces on different images in cluster A; one video face in cluster B.
    await _face(db_session, img_a, person_id=person.id, cluster_id=cluster_a.id)
    await _face(db_session, img_b, person_id=person.id, cluster_id=cluster_a.id)
    await _face(db_session, vid, person_id=person.id, cluster_id=cluster_b.id)
    # Named-match style: person-linked face with cluster_id cleared (still counts).
    await _face(db_session, img_a, person_id=person.id, cluster_id=None)

    await db_session.commit()

    file_count = await _file_count_for_face(db_session, person_id=person.id, cluster_id=None)
    face_count = await _face_count_for_person(db_session, person.id)

    assert file_count == 3  # a.jpg, b.jpg, c.mp4 (unique drive files)
    assert face_count == 4

    # Single-cluster count understates the merged person.
    cluster_only = await _file_count_for_face(
        db_session, person_id=None, cluster_id=cluster_a.id
    )
    assert cluster_only == 2
