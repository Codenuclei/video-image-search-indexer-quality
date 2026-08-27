"""Exact centroid-ranking suggestions: unknown clusters → closest named person.

Shared by the People detail endpoint (single person) and the reverse image
search batch endpoint (all matched people at once), so both rank candidates
with identical rules: a candidate is only suggested for its single closest
eligible named identity (person_rank = 1), never as an ANN-style loose match.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, Face, FaceCluster, Media
from app.schemas import MediaOccurrence

# Policy floor: suggestions below 50% similarity are noise, never allow them.
MIN_SUGGESTION_SIMILARITY = 0.5


async def ranked_cluster_suggestions(
    session: AsyncSession,
    *,
    person_ids: Sequence[int],
    min_similarity: float = MIN_SUGGESTION_SIMILARITY,
    limit: int = 12,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Unknown clusters whose closest eligible named identity is in person_ids.

    Returns (items, total). Each item carries cluster_id, person_id,
    similarity, member_count, representative_face_id, representative_confidence,
    file_count, and up to 4 sample_files. Because every candidate cluster is
    assigned to exactly one best person, items are inherently deduplicated
    across the requested people.
    """
    ids = sorted({int(pid) for pid in person_ids})
    if not ids:
        return [], 0
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    min_similarity = max(MIN_SUGGESTION_SIMILARITY, min(float(min_similarity), 1.0))

    rows = (
        await session.execute(
            text(
                """
                WITH suggestions AS (
                    SELECT cluster_id, person_id, similarity
                    FROM (
                        SELECT
                            candidate.id AS cluster_id,
                            reference.person_id,
                            1 - (candidate.centroid <=> reference.centroid) AS similarity,
                            row_number() OVER (
                                PARTITION BY candidate.id
                                ORDER BY candidate.centroid <=> reference.centroid,
                                         reference.person_id
                            ) AS person_rank
                        FROM face_clusters AS candidate
                        JOIN face_clusters AS reference
                          ON reference.person_id IS NOT NULL
                         AND reference.centroid IS NOT NULL
                        WHERE candidate.status = 'UNKNOWN'
                          AND candidate.person_id IS NULL
                          AND candidate.centroid IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1
                              FROM person_cluster_decisions AS decision
                              WHERE decision.cluster_id = candidate.id
                                AND decision.person_id = reference.person_id
                                AND decision.decision = 'rejected'
                          )
                    ) AS ranked
                    WHERE person_rank = 1
                      AND similarity >= :min_similarity
                )
                SELECT cluster_id, person_id, similarity, count(*) OVER () AS total
                FROM suggestions
                WHERE person_id = ANY(CAST(:person_ids AS integer[]))
                ORDER BY similarity DESC, cluster_id
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {
                "person_ids": ids,
                "min_similarity": min_similarity,
                "offset": offset,
                "limit": limit,
            },
        )
    ).all()
    if not rows:
        return [], 0

    best_by_cluster: dict[int, tuple[int, float]] = {
        int(row.cluster_id): (int(row.person_id), float(row.similarity)) for row in rows
    }
    total = int(rows[0].total)
    cluster_ids = list(best_by_cluster)
    clusters = {
        cluster.id: cluster
        for cluster in (
            await session.execute(
                select(FaceCluster).where(FaceCluster.id.in_(cluster_ids))
            )
        ).scalars()
    }

    file_counts = {
        int(cluster_id): int(file_count)
        for cluster_id, file_count in (
            await session.execute(
                select(
                    Face.cluster_id,
                    func.count(func.distinct(Media.drive_file_id)),
                )
                .join(Media, Face.media_id == Media.id)
                .where(
                    Face.cluster_id.in_(cluster_ids),
                    Media.drive_file_id.is_not(None),
                )
                .group_by(Face.cluster_id)
            )
        ).all()
        if cluster_id is not None
    }

    ranked_occurrences = (
        select(
            Face.cluster_id.label("cluster_id"),
            Media.id.label("media_id"),
            DriveFile.id.label("drive_file_id"),
            DriveFile.name.label("name"),
            DriveFile.path.label("path"),
            Media.type.label("media_type"),
            func.min(Face.frame_timestamp).label("frame_timestamp"),
            func.row_number()
            .over(partition_by=Face.cluster_id, order_by=Media.id)
            .label("sample_rank"),
        )
        .join(Media, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(Face.cluster_id.in_(cluster_ids))
        .group_by(
            Face.cluster_id,
            Media.id,
            DriveFile.id,
            DriveFile.name,
            DriveFile.path,
            Media.type,
        )
        .subquery()
    )
    occurrence_rows = (
        await session.execute(
            select(
                ranked_occurrences.c.cluster_id,
                ranked_occurrences.c.media_id,
                ranked_occurrences.c.drive_file_id,
                ranked_occurrences.c.name,
                ranked_occurrences.c.path,
                ranked_occurrences.c.media_type,
                ranked_occurrences.c.frame_timestamp,
            )
            .where(ranked_occurrences.c.sample_rank <= 4)
            .order_by(
                ranked_occurrences.c.cluster_id,
                ranked_occurrences.c.sample_rank,
            )
        )
    ).all()
    samples: dict[int, list[MediaOccurrence]] = {cluster_id: [] for cluster_id in cluster_ids}
    for cluster_id, media_id, drive_id, name, path, media_type, frame_timestamp in occurrence_rows:
        if cluster_id is None:
            continue
        samples[int(cluster_id)].append(
            MediaOccurrence(
                media_id=media_id,
                drive_file_id=drive_id,
                name=name,
                path=path,
                media_type=media_type.value,
                frame_timestamp=frame_timestamp,
            )
        )

    representative_ids = [
        cluster.representative_face_id
        for cluster in clusters.values()
        if cluster.representative_face_id is not None
    ]
    confidences = {
        int(face_id): float(confidence)
        for face_id, confidence in (
            await session.execute(
                select(Face.id, Face.detection_confidence).where(
                    Face.id.in_(representative_ids)
                )
            )
        ).all()
    }

    items: list[dict[str, Any]] = []
    for cluster_id in cluster_ids:
        if cluster_id not in clusters:
            continue
        person_id, similarity = best_by_cluster[cluster_id]
        items.append(
            {
                "cluster_id": cluster_id,
                "person_id": person_id,
                "similarity": similarity,
                "member_count": clusters[cluster_id].member_count,
                "representative_face_id": clusters[cluster_id].representative_face_id,
                "representative_confidence": confidences.get(
                    clusters[cluster_id].representative_face_id
                ),
                "file_count": file_counts.get(cluster_id, 0),
                "sample_files": samples.get(cluster_id, []),
            }
        )
    return items, total
