from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.deadlock import retry_on_deadlock
from app.db.models import (
    ClusterStatus,
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Person,
    Recognition,
)

logger = logging.getLogger(__name__)


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

    await session.execute(
        update(DriveFile)
        .where(
            DriveFile.id.in_(
                select(Media.drive_file_id)
                .join(Face, Face.media_id == Media.id)
                .where(Face.id.in_(face_ids))
                .distinct()
            )
        )
        .values(status=DriveFileStatus.PENDING, gemini_document_name=None)
    )


async def _queue_gemini_refresh_for_person(session: AsyncSession, person_id: int) -> None:
    """Re-queue Drive files linked to a person without loading every face id into memory."""
    from app.db.models import Media

    await session.execute(
        update(DriveFile)
        .where(
            DriveFile.id.in_(
                select(Media.drive_file_id)
                .join(Face, Face.media_id == Media.id)
                .where(Face.person_id == person_id)
                .distinct()
            )
        )
        .values(status=DriveFileStatus.PENDING, gemini_document_name=None)
    )


async def refresh_gemini_for_person_background(person_id: int) -> None:
    """Re-queue linked Drive files after rename without blocking the HTTP response."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            await _queue_gemini_refresh_for_person(session, person_id)
            await session.commit()
            logger.info("Background gemini refresh queued for person %s", person_id)
        except Exception:  # noqa: BLE001
            logger.exception("Background gemini refresh failed for person %s", person_id)


async def delete_person_background(person_id: int) -> None:
    """Delete a person without blocking the HTTP response."""
    import asyncio

    from app.db.session import get_session_factory

    factory = get_session_factory()
    for attempt in range(6):
        try:
            async with factory() as session:
                await delete_person(session, person_id)
                await session.commit()
            logger.info("Background delete completed for person %s", person_id)
            return
        except ValueError:
            logger.warning("Background delete skipped — person %s not found", person_id)
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            retriable = "deadlock detected" in msg or "lock timeout" in msg
            if retriable and attempt < 5:
                delay = 0.5 * (2**attempt)
                logger.warning(
                    "Background delete for person %s blocked (attempt %d/6) — retrying in %.1fs",
                    person_id,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.exception("Background delete failed for person %s", person_id)
            return


VALID_PERSON_ROLES = frozenset({"student", "non_student"})


def normalize_person_role(role: str | None) -> str | None:
    if role is None:
        return None
    value = role.strip().lower().replace("-", "_").replace(" ", "_")
    if value in ("non_student", "nonstudent", "teacher", "teachers", "faculty", "staff"):
        return "non_student"
    if value in ("student", "students"):
        return "student"
    if value in VALID_PERSON_ROLES:
        return value
    raise ValueError("role must be student, non_student, or null to clear")


async def update_person(
    session: AsyncSession,
    person_id: int,
    *,
    name: str | None = None,
    set_role: bool = False,
    role: str | None = None,
) -> Person:
    person = await session.get(Person, person_id)
    if person is None:
        raise ValueError(f"Person {person_id} not found")

    changed = False
    if name is not None:
        new_name = name.strip()
        if not new_name:
            raise ValueError("Name cannot be empty")
        if person.name != new_name:
            person.name = new_name
            changed = True

    if set_role:
        normalized = normalize_person_role(role) if role not in (None, "") else None
        if person.role != normalized:
            person.role = normalized
            changed = True

    if changed:
        await session.flush()
    return person


async def rename_person(session: AsyncSession, person_id: int, name: str) -> Person:
    return await update_person(session, person_id, name=name)


async def name_cluster(session: AsyncSession, cluster_id: int, name: str) -> Person:
    cluster = await session.get(FaceCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")
    if cluster.status == ClusterStatus.NAMED:
        raise ValueError(f"Cluster {cluster_id} is already named")

    clean = name.strip()
    if not clean:
        raise ValueError("Name cannot be empty")

    # Reuse an existing person with the same name instead of creating duplicates.
    existing = (
        await session.execute(
            select(Person).where(func.lower(Person.name) == clean.lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        return await merge_cluster_into_person(session, cluster_id, existing.id)

    person = Person(name=clean, representative_face_id=cluster.representative_face_id)
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


async def _get_cluster_for_update(session: AsyncSession, cluster_id: int) -> FaceCluster | None:
    return (
        await session.execute(
            select(FaceCluster).where(FaceCluster.id == cluster_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _find_best_cluster(
    session: AsyncSession,
    embedding: list[float],
) -> tuple[FaceCluster | None, float]:
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
    return best_cluster, best_sim


async def _assign_face_once(
    session: AsyncSession,
    face: Face,
    embedding: list[float],
    settings: Settings,
) -> MatchResult:
    """Match a detected face to an existing cluster or create a new unknown cluster."""
    threshold = settings.person_match_threshold
    candidate, best_sim = await _find_best_cluster(session, embedding)

    created_new = False
    matched = False

    if candidate is not None and best_sim >= threshold:
        cluster = await _get_cluster_for_update(session, candidate.id)
        if cluster is not None and cluster.centroid is not None:
            locked_sim = cosine_similarity(embedding, list(cluster.centroid))
            if locked_sim >= threshold:
                best_sim = locked_sim
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
                    with session.no_autoflush:
                        rep = await session.get(Face, cluster.representative_face_id)
                    if rep is None or face.detection_confidence > rep.detection_confidence:
                        cluster.representative_face_id = face.id
                matched = True

    if not matched:
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


async def assign_face(
    session: AsyncSession,
    face: Face,
    embedding: list[float],
    settings: Settings | None = None,
) -> MatchResult:
    settings = settings or get_settings()

    async def _run() -> MatchResult:
        return await _assign_face_once(session, face, embedding, settings)

    return await retry_on_deadlock(_run, label=f"assign_face face_id={face.id}")


async def merge_cluster_into_person(session: AsyncSession, cluster_id: int, person_id: int) -> Person:
    cluster = await session.get(FaceCluster, cluster_id)
    person = await session.get(Person, person_id)
    if cluster is None:
        raise ValueError(f"Cluster {cluster_id} not found")
    if person is None:
        raise ValueError(f"Person {person_id} not found")
    if cluster.status == ClusterStatus.NAMED and cluster.person_id == person_id:
        return person

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


async def delete_person(session: AsyncSession, person_id: int) -> None:
    """Remove a named person; linked clusters return to the unknown review queue."""
    person = await session.get(Person, person_id)
    if person is None:
        raise ValueError(f"Person {person_id} not found")

    # Fail fast instead of hanging when the indexer holds row locks.
    await session.execute(text("SET LOCAL lock_timeout = '10s'"))

    # Break circular FK (person.representative_face_id -> faces.id).
    person.representative_face_id = None
    await session.flush()

    # Reset clusters for review; faces/recognitions unlink via ON DELETE SET NULL.
    await session.execute(
        update(FaceCluster)
        .where(FaceCluster.person_id == person_id)
        .values(status=ClusterStatus.UNKNOWN)
    )
    await session.delete(person)
    await session.flush()
