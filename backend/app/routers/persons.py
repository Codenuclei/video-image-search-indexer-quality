from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClusterStatus, DriveFile, Face, FaceCluster, Media, Person
from app.db.session import get_db
from app.matching.service import (
    delete_person_background,
    merge_cluster_into_person,
    refresh_gemini_for_person_background,
    update_person,
)
from app.schemas import (
    MediaOccurrence,
    PersonClusterSuggestion,
    PersonClusterSuggestionList,
    PersonOut,
    RenamePersonRequest,
    UpdatePersonRequest,
)

router = APIRouter(prefix="/persons", tags=["persons"])


async def _occurrence_count(session: AsyncSession, person_id: int) -> int:
    stmt = select(func.count()).select_from(Face).where(
        or_(
            Face.person_id == person_id,
            Face.cluster_id.in_(
                select(FaceCluster.id).where(FaceCluster.person_id == person_id)
            ),
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def _file_count(session: AsyncSession, person_id: int) -> int:
    """Unique Drive files linked to this person's faces."""
    stmt = (
        select(func.count(func.distinct(Media.drive_file_id)))
        .select_from(Face)
        .join(Media, Face.media_id == Media.id)
        .where(
            or_(
                Face.person_id == person_id,
                Face.cluster_id.in_(
                    select(FaceCluster.id).where(FaceCluster.person_id == person_id)
                ),
            )
        )
        .where(Media.drive_file_id.is_not(None))
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _best_face_id(session: AsyncSession, person: Person) -> int | None:
    """Return the representative face ID, preferring one with a valid on-disk thumbnail."""
    from app.reid.face_thumbs import ensure_face_thumbnail_jpeg, thumb_exists_on_disk

    if person.representative_face_id is not None:
        rep = await session.get(Face, person.representative_face_id)
        if rep is not None:
            if thumb_exists_on_disk(rep):
                return rep.id
            try:
                await ensure_face_thumbnail_jpeg(session, rep.id, allow_fallback=True)
                return rep.id
            except ValueError:
                pass

    faces = (
        await session.execute(
            select(Face)
            .where(
                or_(
                    Face.person_id == person.id,
                    Face.cluster_id.in_(
                        select(FaceCluster.id).where(
                            FaceCluster.person_id == person.id
                        )
                    ),
                )
            )
            .order_by(Face.detection_confidence.desc())
            .limit(10)
        )
    ).scalars().all()
    for face in faces:
        if thumb_exists_on_disk(face):
            return face.id
        try:
            await ensure_face_thumbnail_jpeg(session, face.id, allow_fallback=True)
            return face.id
        except ValueError:
            continue
    return None


def _person_out(
    session: AsyncSession,
    person: Person,
    occurrence_count: int,
    face_id: int | None,
    file_count: int = 0,
) -> PersonOut:
    return PersonOut(
        id=person.id,
        name=person.name,
        role=person.role,
        representative_face_id=face_id,
        occurrence_count=occurrence_count,
        file_count=file_count,
        created_at=person.created_at,
    )


async def _serialize_person(session: AsyncSession, person: Person) -> PersonOut:
    return _person_out(
        session,
        person,
        await _occurrence_count(session, person.id),
        await _best_face_id(session, person),
        await _file_count(session, person.id),
    )


async def _serialize_persons(
    session: AsyncSession,
    persons: list[Person],
) -> list[PersonOut]:
    if not persons:
        return []
    person_ids = [person.id for person in persons]
    identity_person_id = func.coalesce(Face.person_id, FaceCluster.person_id)
    rows = (
        await session.execute(
            select(
                identity_person_id.label("person_id"),
                func.count(Face.id),
                func.count(func.distinct(Media.drive_file_id)),
            )
            .outerjoin(FaceCluster, FaceCluster.id == Face.cluster_id)
            .join(Media, Media.id == Face.media_id)
            .where(identity_person_id.in_(person_ids))
            .group_by(identity_person_id)
        )
    ).all()
    counts = {
        int(person_id): (int(face_count), int(file_count))
        for person_id, face_count, file_count in rows
        if person_id is not None
    }
    face_ids = [await _best_face_id(session, person) for person in persons]
    return [
        _person_out(
            session,
            person,
            counts.get(person.id, (0, 0))[0],
            face_id,
            counts.get(person.id, (0, 0))[1],
        )
        for person, face_id in zip(persons, face_ids, strict=True)
    ]


@router.get("/revision")
async def persons_revision(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Lightweight freshness token for client cache (no full person payloads)."""
    from app.reid.face_thumbs import count_persons_with_valid_rep_thumb

    count = (await session.execute(select(func.count()).select_from(Person))).scalar_one()
    max_id = (await session.execute(select(func.max(Person.id)))).scalar_one()
    occ_sum = (
        await session.execute(
            select(func.count())
            .select_from(Face)
            .outerjoin(FaceCluster, FaceCluster.id == Face.cluster_id)
            .where(
                or_(
                    Face.person_id.isnot(None),
                    FaceCluster.person_id.isnot(None),
                )
            )
        )
    ).scalar_one()
    valid_reps = await count_persons_with_valid_rep_thumb(session)
    revision = f"{int(count or 0)}:{int(max_id or 0)}:{int(occ_sum or 0)}:{int(valid_reps or 0)}"
    return {"revision": revision, "count": int(count or 0)}


@router.get("", response_model=list[PersonOut])
async def list_persons(session: AsyncSession = Depends(get_db)) -> list[PersonOut]:
    persons = (await session.execute(select(Person).order_by(Person.name))).scalars().all()
    return await _serialize_persons(session, list(persons))


@router.get("/search", response_model=list[PersonOut])
async def search_persons(
    q: str = "",
    limit: int = 20,
    session: AsyncSession = Depends(get_db),
) -> list[PersonOut]:
    """Search named people by substring (for merge picker in review queue)."""
    query = q.strip()
    if not query:
        return []
    limit = max(1, min(limit, 50))
    persons = (
        await session.execute(
            select(Person)
            .where(Person.name.ilike(f"%{query}%"))
            .order_by(Person.name)
            .limit(limit)
        )
    ).scalars().all()
    return await _serialize_persons(session, list(persons))


@router.get("/suggestion-counts")
async def person_suggestion_counts(
    session: AsyncSession = Depends(get_db),
) -> dict[int, int]:
    """Counts of over-50% unknown cluster suggestions by best named identity."""
    rows = (
        await session.execute(
            text(
                """
                WITH ranked AS (
                    SELECT
                        candidate.id AS cluster_id,
                        reference.person_id,
                        candidate.centroid <=> reference.centroid AS distance,
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
                ),
                suggestions AS (
                    SELECT person_id
                    FROM ranked
                    WHERE person_rank = 1
                      AND distance <= 0.5
                )
                SELECT person_id, count(*)
                FROM suggestions
                GROUP BY person_id
                """
            )
        )
    ).all()
    return {int(person_id): int(count) for person_id, count in rows}


@router.get("/{person_id}", response_model=PersonOut)
async def get_person(person_id: int, session: AsyncSession = Depends(get_db)) -> PersonOut:
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return await _serialize_person(session, person)


async def _cluster_similarity_to_person(
    session: AsyncSession,
    *,
    person_id: int,
    cluster_id: int,
) -> float | None:
    value = await session.scalar(
        text(
            """
            SELECT max(1 - (candidate.centroid <=> reference.centroid))
            FROM face_clusters AS candidate
            JOIN face_clusters AS reference
              ON reference.person_id = :person_id
             AND reference.centroid IS NOT NULL
            WHERE candidate.id = :cluster_id
              AND candidate.centroid IS NOT NULL
            """
        ),
        {"person_id": person_id, "cluster_id": cluster_id},
    )
    return float(value) if value is not None else None


@router.get(
    "/{person_id}/suggested-clusters",
    response_model=PersonClusterSuggestionList,
)
async def suggested_clusters_for_person(
    person_id: int,
    limit: int = 12,
    offset: int = 0,
    min_similarity: float = 0.5,
    session: AsyncSession = Depends(get_db),
) -> PersonClusterSuggestionList:
    """Unknown clusters whose closest eligible named identity is this person."""
    if await session.get(Person, person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    min_similarity = max(0.5, min(float(min_similarity), 1.0))

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
                SELECT cluster_id, similarity, count(*) OVER () AS total
                FROM suggestions
                WHERE person_id = :person_id
                ORDER BY similarity DESC, cluster_id
                OFFSET :offset
                LIMIT :limit
                """
            ),
            {
                "person_id": person_id,
                "min_similarity": min_similarity,
                "offset": offset,
                "limit": limit,
            },
        )
    ).all()
    if not rows:
        return PersonClusterSuggestionList(items=[], total=0, offset=offset, limit=limit)

    similarities = {int(row.cluster_id): float(row.similarity) for row in rows}
    total = int(rows[0].total)
    cluster_ids = list(similarities)
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
    return PersonClusterSuggestionList(
        items=[
            PersonClusterSuggestion(
                cluster_id=cluster_id,
                similarity=similarities[cluster_id],
                member_count=clusters[cluster_id].member_count,
                representative_face_id=clusters[cluster_id].representative_face_id,
                representative_confidence=confidences.get(
                    clusters[cluster_id].representative_face_id
                ),
                file_count=file_counts.get(cluster_id, 0),
                sample_files=samples.get(cluster_id, []),
            )
            for cluster_id in cluster_ids
            if cluster_id in clusters
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


async def _record_cluster_decision(
    session: AsyncSession,
    *,
    person_id: int,
    cluster_id: int,
    decision: str,
    similarity: float | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO person_cluster_decisions
                (person_id, cluster_id, decision, similarity)
            VALUES
                (:person_id, :cluster_id, :decision, :similarity)
            ON CONFLICT (person_id, cluster_id, decision) DO NOTHING
            """
        ),
        {
            "person_id": person_id,
            "cluster_id": cluster_id,
            "decision": decision,
            "similarity": similarity,
        },
    )


@router.post(
    "/{person_id}/suggested-clusters/{cluster_id}/accept",
    response_model=PersonOut,
)
async def accept_suggested_cluster(
    person_id: int,
    cluster_id: int,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    cluster = await session.get(FaceCluster, cluster_id)
    if cluster is None or cluster.status != ClusterStatus.UNKNOWN:
        raise HTTPException(status_code=409, detail="Cluster is no longer available")
    similarity = await _cluster_similarity_to_person(
        session, person_id=person_id, cluster_id=cluster_id
    )
    try:
        person = await merge_cluster_into_person(session, cluster_id, person_id)
        await _record_cluster_decision(
            session,
            person_id=person_id,
            cluster_id=cluster_id,
            decision="accepted",
            similarity=similarity,
        )
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await _serialize_person(session, person)


@router.post(
    "/{person_id}/suggested-clusters/{cluster_id}/reject",
    status_code=204,
)
async def reject_suggested_cluster(
    person_id: int,
    cluster_id: int,
    session: AsyncSession = Depends(get_db),
) -> None:
    person = await session.get(Person, person_id)
    cluster = await session.get(FaceCluster, cluster_id)
    if person is None or cluster is None:
        raise HTTPException(status_code=404, detail="Person or cluster not found")
    if cluster.status != ClusterStatus.UNKNOWN:
        raise HTTPException(status_code=409, detail="Cluster is no longer available")
    similarity = await _cluster_similarity_to_person(
        session, person_id=person_id, cluster_id=cluster_id
    )
    await _record_cluster_decision(
        session,
        person_id=person_id,
        cluster_id=cluster_id,
        decision="rejected",
        similarity=similarity,
    )
    await session.commit()


@router.patch("/{person_id}", response_model=PersonOut)
async def update_person_endpoint(
    person_id: int,
    body: UpdatePersonRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Rename and/or set student / non-student role on a tagged person."""
    if body.name is None and "role" not in body.model_fields_set:
        raise HTTPException(status_code=400, detail="Provide name and/or role to update")

    try:
        person = await update_person(
            session,
            person_id,
            name=body.name,
            set_role="role" in body.model_fields_set,
            role=body.role,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.name is not None:
        background_tasks.add_task(refresh_gemini_for_person_background, person_id)

    return await _serialize_person(session, person)


@router.put("/{person_id}/name", response_model=PersonOut, include_in_schema=False)
async def update_person_name_legacy(
    person_id: int,
    body: RenamePersonRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Backward-compatible rename endpoint."""
    try:
        person = await update_person(session, person_id, name=body.name)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(refresh_gemini_for_person_background, person_id)
    return await _serialize_person(session, person)


@router.delete("/{person_id}", status_code=204)
async def delete_person_endpoint(
    person_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
) -> None:
    """Delete a person name; faces unlink and clusters return to the review queue."""
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    background_tasks.add_task(delete_person_background, person_id)


@router.get("/{person_id}/media", response_model=list[MediaOccurrence])
async def get_person_media(person_id: int, session: AsyncSession = Depends(get_db)) -> list[MediaOccurrence]:
    """Every piece of media this person appears in."""
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    stmt = (
        select(
            Media.id,
            DriveFile.id,
            DriveFile.name,
            DriveFile.path,
            Media.type,
            func.min(Face.frame_timestamp),
        )
        .join(Face, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(
            or_(
                Face.person_id == person_id,
                Face.cluster_id.in_(
                    select(FaceCluster.id).where(FaceCluster.person_id == person_id)
                ),
            )
        )
        .group_by(Media.id, DriveFile.id, DriveFile.name, DriveFile.path, Media.type)
    )
    rows = (await session.execute(stmt)).all()
    return [
        MediaOccurrence(
            media_id=media_id,
            drive_file_id=drive_id,
            name=name,
            path=path,
            media_type=media_type.value,
            frame_timestamp=frame_timestamp,
        )
        for media_id, drive_id, name, path, media_type, frame_timestamp in rows
    ]
