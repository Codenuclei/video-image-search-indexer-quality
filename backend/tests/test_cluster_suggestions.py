"""Exact suggestion-ranking shared by People detail and reverse-search batch."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

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
from app.matching.suggestions import ranked_cluster_suggestions
from tests.conftest import requires_postgres


def _vec(pairs: dict[int, float]) -> list[float]:
    vec = [0.0] * 512
    for idx, value in pairs.items():
        vec[idx] = value
    return vec


async def _named_cluster(session, person: Person, centroid: list[float]) -> FaceCluster:
    cluster = FaceCluster(
        status=ClusterStatus.NAMED,
        person_id=person.id,
        member_count=1,
        centroid=centroid,
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _unknown_cluster(
    session, centroid: list[float], *, with_file: bool = False
) -> FaceCluster:
    cluster = FaceCluster(
        status=ClusterStatus.UNKNOWN,
        person_id=None,
        member_count=2,
        centroid=centroid,
    )
    session.add(cluster)
    await session.flush()
    if with_file:
        drive_file = DriveFile(
            id=f"drive-{uuid.uuid4().hex}",
            name="shot.jpg",
            mime_type="image/jpeg",
            path="/shot.jpg",
            status=DriveFileStatus.PROCESSED,
        )
        session.add(drive_file)
        await session.flush()
        media = Media(drive_file_id=drive_file.id, type=MediaType.IMAGE)
        session.add(media)
        await session.flush()
        session.add(
            Face(
                media_id=media.id,
                bbox_x=0.0,
                bbox_y=0.0,
                bbox_width=10.0,
                bbox_height=10.0,
                detection_confidence=0.99,
                cluster_id=cluster.id,
            )
        )
        await session.flush()
    return cluster


@pytest.mark.asyncio
async def test_empty_person_ids_short_circuits() -> None:
    # Empty ids must return before any query runs, so a dummy session suffices.
    items, total = await ranked_cluster_suggestions(
        None,  # type: ignore[arg-type]
        person_ids=[],
    )
    assert items == []
    assert total == 0


@requires_postgres
@pytest.mark.asyncio
async def test_candidate_assigned_to_closest_person_only(db_session) -> None:
    person_a = Person(name=f"A-{uuid.uuid4().hex[:8]}")
    person_b = Person(name=f"B-{uuid.uuid4().hex[:8]}")
    db_session.add_all([person_a, person_b])
    await db_session.flush()
    await _named_cluster(db_session, person_a, _vec({0: 1.0}))
    await _named_cluster(db_session, person_b, _vec({1: 1.0}))
    candidate = await _unknown_cluster(db_session, _vec({0: 1.0, 1: 0.2}), with_file=True)
    await db_session.commit()

    items, total = await ranked_cluster_suggestions(
        db_session, person_ids=[person_a.id, person_b.id], min_similarity=0.5, limit=24
    )

    # Exactly one row for the candidate, attributed to the closer person (A),
    # even though both people were requested — batch dedup by assignment.
    candidate_rows = [item for item in items if item["cluster_id"] == candidate.id]
    assert len(candidate_rows) == 1
    assert candidate_rows[0]["person_id"] == person_a.id
    assert candidate_rows[0]["similarity"] == pytest.approx(0.9806, abs=1e-3)
    assert candidate_rows[0]["member_count"] == 2
    assert candidate_rows[0]["file_count"] == 1
    assert len(candidate_rows[0]["sample_files"]) == 1
    assert total >= 1


@requires_postgres
@pytest.mark.asyncio
async def test_rejected_pair_is_excluded(db_session) -> None:
    person_a = Person(name=f"A-{uuid.uuid4().hex[:8]}")
    person_b = Person(name=f"B-{uuid.uuid4().hex[:8]}")
    db_session.add_all([person_a, person_b])
    await db_session.flush()
    await _named_cluster(db_session, person_a, _vec({0: 1.0}))
    await _named_cluster(db_session, person_b, _vec({1: 1.0}))
    candidate = await _unknown_cluster(db_session, _vec({0: 1.0, 1: 0.2}))
    await db_session.execute(
        text(
            """
            INSERT INTO person_cluster_decisions (person_id, cluster_id, decision, similarity)
            VALUES (:person_id, :cluster_id, 'rejected', 0.98)
            """
        ),
        {"person_id": person_a.id, "cluster_id": candidate.id},
    )
    await db_session.commit()

    items, _ = await ranked_cluster_suggestions(
        db_session, person_ids=[person_a.id, person_b.id], min_similarity=0.5, limit=24
    )

    # A rejected the candidate; the only other eligible identity (B) is below
    # the floor, so nothing is suggested for it.
    assert all(item["cluster_id"] != candidate.id for item in items)


@requires_postgres
@pytest.mark.asyncio
async def test_min_similarity_floor_is_enforced(db_session) -> None:
    person_a = Person(name=f"A-{uuid.uuid4().hex[:8]}")
    db_session.add(person_a)
    await db_session.flush()
    await _named_cluster(db_session, person_a, _vec({0: 1.0}))
    # cos ≈ 0.4 to A — above a (too-low) requested 0.3 but below the 0.5 floor.
    weak = await _unknown_cluster(db_session, _vec({0: 0.4, 1: 0.9165}))
    # cos ≈ 0.6 to A — legitimately above the floor.
    strong = await _unknown_cluster(db_session, _vec({0: 0.6, 1: 0.8}))
    await db_session.commit()

    items, _ = await ranked_cluster_suggestions(
        db_session, person_ids=[person_a.id], min_similarity=0.3, limit=24
    )

    cluster_ids = {item["cluster_id"] for item in items}
    assert strong.id in cluster_ids
    assert weak.id not in cluster_ids
