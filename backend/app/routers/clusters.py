from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    ClusterStatus,
    DriveFile,
    Face,
    FaceCluster,
    Media,
    Startup,
    StartupFaceCluster,
    StartupFounder,
)
from app.db.session import get_db
from app.matching.service import ignore_cluster, merge_cluster_into_person, name_cluster
from app.routers.persons import _serialize_person
from app.schemas import (
    ClusterListResponse,
    ClusterOut,
    MediaOccurrence,
    MergeClusterRequest,
    NameClusterRequest,
    PersonOut,
    StartupClusterContext,
)

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
            Media.duration_seconds,
        )
        .join(Face, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(Face.cluster_id == cluster.id)
        .group_by(
            Media.id,
            DriveFile.id,
            DriveFile.name,
            DriveFile.path,
            Media.type,
            Media.duration_seconds,
        )
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
            duration_seconds=duration_seconds,
        )
        for media_id, drive_id, name, path, media_type, frame_timestamp, duration_seconds in rows
    ]

    representative_confidence = None
    if cluster.representative_face_id is not None:
        rep_face = await session.get(Face, cluster.representative_face_id)
        representative_confidence = rep_face.detection_confidence if rep_face else None

    startup_ctx = await _startup_contexts(session, [cluster.id])
    return ClusterOut(
        id=cluster.id,
        status=cluster.status.value,
        member_count=cluster.member_count,
        representative_face_id=cluster.representative_face_id,
        representative_confidence=representative_confidence,
        appears_in=appears_in,
        created_at=cluster.created_at,
        startup=startup_ctx.get(cluster.id),
    )


async def _build_cluster_outs(
    session: AsyncSession, clusters: list[FaceCluster]
) -> list[ClusterOut]:
    """Serialize a page in batches instead of two queries per cluster."""
    if not clusters:
        return []

    cluster_ids = [cluster.id for cluster in clusters]
    occurrence_rows = (
        await session.execute(
            select(
                Face.cluster_id,
                Media.id,
                DriveFile.id,
                DriveFile.name,
                DriveFile.path,
                Media.type,
                func.min(Face.frame_timestamp),
                Media.duration_seconds,
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
                Media.duration_seconds,
            )
        )
    ).all()
    occurrences: dict[int, list[MediaOccurrence]] = {cluster_id: [] for cluster_id in cluster_ids}
    for (
        cluster_id,
        media_id,
        drive_id,
        name,
        path,
        media_type,
        frame_timestamp,
        duration_seconds,
    ) in occurrence_rows:
        if cluster_id is None:
            continue
        occurrences[int(cluster_id)].append(
            MediaOccurrence(
                media_id=media_id,
                drive_file_id=drive_id,
                name=name,
                path=path,
                media_type=media_type.value,
                frame_timestamp=frame_timestamp,
                duration_seconds=duration_seconds,
            )
        )

    representative_ids = [
        cluster.representative_face_id
        for cluster in clusters
        if cluster.representative_face_id is not None
    ]
    confidences: dict[int, float] = {}
    if representative_ids:
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

    startup_ctx = await _startup_contexts(session, cluster_ids)
    return [
        ClusterOut(
            id=cluster.id,
            status=cluster.status.value,
            member_count=cluster.member_count,
            representative_face_id=cluster.representative_face_id,
            representative_confidence=confidences.get(cluster.representative_face_id),
            appears_in=occurrences.get(cluster.id, []),
            created_at=cluster.created_at,
            startup=startup_ctx.get(cluster.id),
        )
        for cluster in clusters
    ]


async def _startup_contexts(
    session: AsyncSession,
    cluster_ids: list[int],
) -> dict[int, StartupClusterContext]:
    if not cluster_ids:
        return {}
    rows = (
        await session.execute(
            select(StartupFaceCluster, Startup)
            .join(Startup, Startup.id == StartupFaceCluster.startup_id)
            .where(StartupFaceCluster.cluster_id.in_(cluster_ids))
        )
    ).all()
    if not rows:
        return {}
    startup_ids = list({startup.id for _link, startup in rows})
    founders = (
        await session.execute(
            select(StartupFounder).where(StartupFounder.startup_id.in_(startup_ids))
        )
    ).scalars().all()
    by_startup: dict[int, list[str]] = {}
    for founder in founders:
        by_startup.setdefault(founder.startup_id, []).append(founder.founder_name)

    out: dict[int, StartupClusterContext] = {}
    for link, startup in rows:
        if link.cluster_id in out:
            continue
        candidates = [
            name
            for name in by_startup.get(startup.id, [])
        ]
        # Prefer non-candidate names first while keeping order stable.
        primary = [
            f.founder_name
            for f in founders
            if f.startup_id == startup.id and not f.is_candidate
        ]
        secondary = [
            f.founder_name
            for f in founders
            if f.startup_id == startup.id and f.is_candidate
        ]
        out[int(link.cluster_id)] = StartupClusterContext(
            startup_id=startup.id,
            startup_slug=startup.slug,
            startup_name=startup.name,
            source_asset=link.source_asset,
            founder_candidates=primary or secondary or candidates,
        )
    return out


@router.get("", response_model=ClusterListResponse)
async def list_clusters(
    include_ignored: bool = False,
    limit: int = 100,
    offset: int = 0,
    session: AsyncSession = Depends(get_db),
) -> ClusterListResponse:
    """Unknown-faces review queue: clusters awaiting a name (or ignored, if requested)."""
    settings = get_settings()
    min_conf = settings.review_queue_min_confidence
    statuses = [ClusterStatus.UNKNOWN]
    if include_ignored:
        statuses.append(ClusterStatus.IGNORED)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    filters = (
        FaceCluster.status.in_(statuses),
        Face.detection_confidence >= min_conf,
    )
    count_stmt = (
        select(func.count(FaceCluster.id))
        .join(Face, FaceCluster.representative_face_id == Face.id)
        .where(*filters)
    )
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(FaceCluster)
        .join(Face, FaceCluster.representative_face_id == Face.id)
        .where(*filters)
        .order_by(FaceCluster.member_count.desc())
        .offset(offset)
        .limit(limit)
    )
    clusters = (await session.execute(stmt)).scalars().all()
    items = await _build_cluster_outs(session, list(clusters))
    return ClusterListResponse(items=items, total=total, offset=offset, limit=limit)


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

    return await _serialize_person(session, person)


@router.post("/{cluster_id}/ignore", status_code=204)
async def ignore_cluster_endpoint(cluster_id: int, session: AsyncSession = Depends(get_db)) -> None:
    try:
        await ignore_cluster(session, cluster_id)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{cluster_id}/merge", response_model=PersonOut)
async def merge_cluster_endpoint(
    cluster_id: int,
    body: MergeClusterRequest,
    session: AsyncSession = Depends(get_db),
) -> PersonOut:
    try:
        person = await merge_cluster_into_person(session, cluster_id, body.person_id)
        await session.commit()
        return await _serialize_person(session, person)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
