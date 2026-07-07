"""
Integration tests for the matching/clustering service against a *real* Postgres +
pgvector instance (cosine_distance is a Postgres-only operator, so this cannot be
faked with SQLite). Start one locally, e.g.:

    docker run -d --name dfi-test-postgres -p 55432:5432 \\
        -e POSTGRES_USER=drivefaceindexer -e POSTGRES_PASSWORD=drivefaceindexer \\
        -e POSTGRES_DB=drivefaceindexer_test pgvector/pgvector:pg16

and optionally point TEST_DATABASE_URL at it (defaults to the above).
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models import ClusterStatus, DriveFile, DriveFileStatus, Face, FaceCluster, FaceEmbedding, Media, MediaType
from app.matching.service import assign_face, ignore_cluster, merge_cluster_into_person, name_cluster
from tests.conftest import requires_postgres

EMBEDDING_DIM = 512


def _unit_vector(hot_index: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[hot_index] = 1.0
    return vec


async def _make_media(session) -> Media:
    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name="test.jpg",
        mime_type="image/jpeg",
        path="/test.jpg",
        status=DriveFileStatus.PROCESSING,
    )
    session.add(drive_file)
    await session.flush()
    media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
    session.add(media)
    await session.flush()
    return media


async def _make_face(session, media: Media, embedding: list[float]) -> Face:
    face = Face(
        media_id=media.id,
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_width=10.0,
        bbox_height=10.0,
        detection_confidence=0.99,
    )
    session.add(face)
    await session.flush()
    session.add(FaceEmbedding(face_id=face.id, embedding=embedding))
    await session.flush()
    return face


@requires_postgres
@pytest.mark.asyncio
async def test_first_face_creates_new_unknown_cluster(db_session):
    media = await _make_media(db_session)
    face = await _make_face(db_session, media, _unit_vector(0))

    result = await assign_face(db_session, face, _unit_vector(0))
    await db_session.commit()

    assert result.created_new_cluster is True
    assert result.person_id is None
    assert result.cluster_id is not None
    assert face.cluster_id == result.cluster_id

    cluster = await db_session.get(FaceCluster, result.cluster_id)
    assert cluster.status == ClusterStatus.UNKNOWN
    assert cluster.member_count == 1


@requires_postgres
@pytest.mark.asyncio
async def test_similar_face_joins_existing_cluster_instead_of_creating_new_one(db_session):
    media = await _make_media(db_session)
    embedding = _unit_vector(0)

    face1 = await _make_face(db_session, media, embedding)
    result1 = await assign_face(db_session, face1, embedding)
    await db_session.commit()

    face2 = await _make_face(db_session, media, embedding)
    result2 = await assign_face(db_session, face2, embedding)
    await db_session.commit()

    assert result2.created_new_cluster is False
    assert result2.cluster_id == result1.cluster_id

    cluster = await db_session.get(FaceCluster, result1.cluster_id)
    assert cluster.member_count == 2


@requires_postgres
@pytest.mark.asyncio
async def test_dissimilar_faces_create_separate_clusters(db_session):
    media = await _make_media(db_session)

    face1 = await _make_face(db_session, media, _unit_vector(0))
    result1 = await assign_face(db_session, face1, _unit_vector(0))

    face2 = await _make_face(db_session, media, _unit_vector(1))
    result2 = await assign_face(db_session, face2, _unit_vector(1))
    await db_session.commit()

    assert result1.cluster_id != result2.cluster_id


@requires_postgres
@pytest.mark.asyncio
async def test_naming_a_cluster_creates_person_and_relabels_members(db_session):
    media = await _make_media(db_session)
    embedding = _unit_vector(0)

    face1 = await _make_face(db_session, media, embedding)
    result1 = await assign_face(db_session, face1, embedding)
    face2 = await _make_face(db_session, media, embedding)
    await assign_face(db_session, face2, embedding)
    await db_session.commit()

    person = await name_cluster(db_session, result1.cluster_id, "Alice")
    await db_session.commit()

    await db_session.refresh(face1)
    await db_session.refresh(face2)
    assert face1.person_id == person.id
    assert face2.person_id == person.id
    assert face1.cluster_id is None
    assert face2.cluster_id is None

    cluster = await db_session.get(FaceCluster, result1.cluster_id)
    assert cluster.status == ClusterStatus.NAMED
    assert cluster.person_id == person.id


@requires_postgres
@pytest.mark.asyncio
async def test_future_matching_face_is_auto_tagged_to_named_person(db_session):
    media = await _make_media(db_session)
    embedding = _unit_vector(0)

    face1 = await _make_face(db_session, media, embedding)
    result1 = await assign_face(db_session, face1, embedding)
    await db_session.commit()
    person = await name_cluster(db_session, result1.cluster_id, "Bob")
    await db_session.commit()

    face2 = await _make_face(db_session, media, embedding)
    result2 = await assign_face(db_session, face2, embedding)
    await db_session.commit()

    assert result2.person_id == person.id
    assert result2.cluster_id is None
    assert face2.person_id == person.id


@requires_postgres
@pytest.mark.asyncio
async def test_merge_cluster_into_existing_person(db_session):
    media = await _make_media(db_session)

    face_a = await _make_face(db_session, media, _unit_vector(0))
    result_a = await assign_face(db_session, face_a, _unit_vector(0))
    await db_session.commit()
    person = await name_cluster(db_session, result_a.cluster_id, "Carol")
    await db_session.commit()

    face_b = await _make_face(db_session, media, _unit_vector(2))
    result_b = await assign_face(db_session, face_b, _unit_vector(2))
    await db_session.commit()
    assert result_b.created_new_cluster is True

    await merge_cluster_into_person(db_session, result_b.cluster_id, person.id)
    await db_session.commit()

    await db_session.refresh(face_b)
    assert face_b.person_id == person.id
    assert face_b.cluster_id is None


@requires_postgres
@pytest.mark.asyncio
async def test_ignored_cluster_keeps_absorbing_future_matches_quietly(db_session):
    media = await _make_media(db_session)
    embedding = _unit_vector(3)

    face1 = await _make_face(db_session, media, embedding)
    result1 = await assign_face(db_session, face1, embedding)
    await db_session.commit()

    await ignore_cluster(db_session, result1.cluster_id)
    await db_session.commit()

    face2 = await _make_face(db_session, media, embedding)
    result2 = await assign_face(db_session, face2, embedding)
    await db_session.commit()

    # Still joins the same (now-ignored) cluster rather than spawning a new one or a person.
    assert result2.cluster_id == result1.cluster_id
    assert result2.person_id is None

    cluster = await db_session.get(FaceCluster, result1.cluster_id)
    assert cluster.status == ClusterStatus.IGNORED
    assert cluster.member_count == 2
