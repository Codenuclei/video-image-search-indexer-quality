from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClusterStatus, DriveFile, Face, FaceCluster, Media
from app.db.session import get_db
from app.matching.service import ignore_cluster, merge_cluster_into_person, name_cluster
from app.schemas import ClusterOut, MediaOccurrence, MergeClusterRequest, NameClusterRequest, PersonOut

router = APIRouter(prefix="/clusters", tags=["clusters"])


async def _build_cluster_out(session: AsyncSession, cluster: FaceCluster) -> ClusterOut:
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
        .where(Face.cluster_id == cluster.id)
        .group_by(Media.id, DriveFile.id, DriveFile.name, DriveFile.path, Media.type)
    )
    rows = (await session.execute(stmt)).all()
    appears_in = [
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

    representative_confidence = None
    if cluster.representative_face_id is not None:
        rep_face = await session.get(Face, cluster.representative_face_id)
        representative_confidence = rep_face.detection_confidence if rep_face else None

    return ClusterOut(
        id=cluster.id,
        status=cluster.status.value,
        member_count=cluster.member_count,
        representative_face_id=cluster.representative_face_id,
        representative_confidence=representative_confidence,
        appears_in=appears_in,
        created_at=cluster.created_at,
    )


@router.get("", response_model=list[ClusterOut])
async def list_clusters(
    include_ignored: bool = False,
    session: AsyncSession = Depends(get_db),
) -> list[ClusterOut]:
    """Unknown-faces review queue: clusters awaiting a name (or ignored, if requested)."""
    statuses = [ClusterStatus.UNKNOWN]
    if include_ignored:
        statuses.append(ClusterStatus.IGNORED)
    stmt = select(FaceCluster).where(FaceCluster.status.in_(statuses)).order_by(FaceCluster.member_count.desc())
    clusters = (await session.execute(stmt)).scalars().all()
    return [await _build_cluster_out(session, c) for c in clusters]


@router.get("/{cluster_id}", response_model=ClusterOut)
async def get_cluster(cluster_id: int, session: AsyncSession = Depends(get_db)) -> ClusterOut:
    cluster = await session.get(FaceCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return await _build_cluster_out(session, cluster)


@router.post("/{cluster_id}/name", response_model=PersonOut)
async def name_cluster_endpoint(
    cluster_id: int,
    body: NameClusterRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    """Manually name a face cluster. Re-queues Gemini upload with person_name metadata."""
    from sqlalchemy import func

    try:
        person = await name_cluster(session, cluster_id, body.name)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    count_stmt = select(func.count()).select_from(Face).where(Face.person_id == person.id)
    occurrence_count = (await session.execute(count_stmt)).scalar_one()
    return PersonOut(
        id=person.id,
        name=person.name,
        representative_face_id=person.representative_face_id,
        occurrence_count=occurrence_count,
        created_at=person.created_at,
    )


@router.post("/{cluster_id}/ignore", status_code=204)
async def ignore_cluster_endpoint(cluster_id: int, session: AsyncSession = Depends(get_db)) -> None:
    try:
        await ignore_cluster(session, cluster_id)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{cluster_id}/merge", status_code=204)
async def merge_cluster_endpoint(
    cluster_id: int,
    body: MergeClusterRequest,
    session: AsyncSession = Depends(get_db),
) -> None:
    try:
        await merge_cluster_into_person(session, cluster_id, body.person_id)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
