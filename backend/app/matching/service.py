from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import (
    ClusterStatus,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Person,
    Recognition,
)


@dataclass(frozen=True)
class MatchResult:
    person_id: int | None
    cluster_id: int | None
    similarity: float | None
    created_new_cluster: bool = False


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb)) or 1e-8
    return float(np.dot(va, vb) / denom)


async def _queue_gemini_refresh_for_faces(session: AsyncSession, face_ids: list[int]) -> None:
    """After manual naming, re-upload affected Drive files so Gemini gets person_name metadata."""
    if not face_ids:
        return
    from app.db.models import Media

    drive_ids = (
        await session.execute(
            select(Media.drive_file_id)
            .join(Face, Face.media_id == Media.id)
            .where(Face.id.in_(face_ids))
            .distinct()
        )
    ).scalars().all()
    if not drive_ids:
        return
    await session.execute(
        update(DriveFile)
        .where(DriveFile.id.in_(drive_ids))
        .values(status=DriveFileStatus.PENDING, gemini_document_name=None)
    )


async def rename_person(session: AsyncSession, person_id: int, name: str) -> Person:
    person = await session.get(Person, person_id)
    if person is None:
        raise ValueError(f"Person {person_id} not found")

    new_name = name.strip()
    if not new_name:
        raise ValueError("Name cannot be empty")
    if person.name == new_name:
        return person

    person.name = new_name
    face_ids = list(
        (await session.execute(select(Face.id).where(Face.person_id == person_id))).scalars().all()
    )
    await _queue_gemini_refresh_for_faces(session, face_ids)
    await session.flush()
    return person


async def name_cluster(session: AsyncSession, cluster_id: int, name: str) -> Person:
    cluster = await session.get(FaceCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")

    person = Person(name=name.strip(), representative_face_id=cluster.representative_face_id)
    session.add(person)
    await session.flush()

    faces = (await session.execute(select(Face).where(Face.cluster_id == cluster_id))).scalars().all()
    face_ids = []
    for face in faces:
        face.person_id = person.id
        face_ids.append(face.id)

    cluster.person_id = person.id
    cluster.status = ClusterStatus.NAMED
    person.representative_face_id = cluster.representative_face_id

    await _queue_gemini_refresh_for_faces(session, face_ids)
    await session.flush()
    return person


async def ignore_cluster(session: AsyncSession, cluster_id: int) -> None:
    cluster = await session.get(FaceCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")
    cluster.status = ClusterStatus.IGNORED
    await session.flush()


async def assign_face(
    session: AsyncSession,
    face: Face,
    embedding: list[float],
    settings: Settings | None = None,
) -> MatchResult:
    """Match a detected face to an existing cluster or create a new unknown cluster."""
    settings = settings or get_settings()
    threshold = settings.person_match_threshold

    clusters = (
        await session.execute(select(FaceCluster).where(FaceCluster.centroid.isnot(None)))
    ).scalars().all()

    best_cluster: FaceCluster | None = None
    best_sim = -1.0
    for cluster in clusters:
        if cluster.centroid is None:
            continue
        sim = cosine_similarity(embedding, list(cluster.centroid))
        if sim > best_sim:
            best_sim = sim
            best_cluster = cluster

    created_new = False
    if best_cluster is not None and best_sim >= threshold:
        cluster = best_cluster
        n = cluster.member_count
        old = np.asarray(cluster.centroid, dtype=np.float32)
        new = np.asarray(embedding, dtype=np.float32)
        cluster.centroid = ((old * n + new) / (n + 1)).tolist()
        cluster.member_count = n + 1
        if cluster.person_id is not None:
            face.person_id = cluster.person_id
            face.cluster_id = None
        else:
            face.cluster_id = cluster.id
        if cluster.representative_face_id is None:
            cluster.representative_face_id = face.id
        else:
            rep = await session.get(Face, cluster.representative_face_id)
            if rep is None or face.detection_confidence > rep.detection_confidence:
                cluster.representative_face_id = face.id
    else:
        cluster = FaceCluster(
            representative_face_id=face.id,
            status=ClusterStatus.UNKNOWN,
            centroid=embedding,
            member_count=1,
        )
        session.add(cluster)
        await session.flush()
        face.cluster_id = cluster.id
        created_new = True
        best_sim = None

    session.add(
        Recognition(
            media_id=face.media_id,
            face_id=face.id,
            person_id=face.person_id,
            confidence=float(best_sim) if best_sim is not None and best_sim >= 0 else 1.0,
        )
    )

    if face.person_id is not None:
        await _queue_gemini_refresh_for_faces(session, [face.id])

    await session.flush()
    return MatchResult(
        person_id=face.person_id,
        cluster_id=face.cluster_id,
        similarity=best_sim if not created_new else None,
        created_new_cluster=created_new,
    )


async def merge_cluster_into_person(session: AsyncSession, cluster_id: int, person_id: int) -> Person:
    cluster = await session.get(FaceCluster, cluster_id)
    person = await session.get(Person, person_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")
    if person is None:
        raise ValueError(f"Person {person_id} not found")

    faces = (await session.execute(select(Face).where(Face.cluster_id == cluster_id))).scalars().all()
    face_ids = []
    for face in faces:
        face.person_id = person.id
        face_ids.append(face.id)

    cluster.person_id = person.id
    cluster.status = ClusterStatus.NAMED
    await _queue_gemini_refresh_for_faces(session, face_ids)
    await session.flush()
    return person
