"""Upload-an-image reverse face search against pgvector FaceEmbedding index."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

import cv2
import httpx
import numpy as np
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    DriveFile,
    Face,
    FaceCluster,
    FaceEmbedding,
    Media,
    MediaType,
    Person,
)
from app.faces.engine import DetectedFace, get_face_engine
from app.pipelines.async_cpu import run_cpu_bound
from app.reid.face_thumbs import resolve_face_thumbnail_id
from app.reid.reverse_search import linkedin_map

logger = logging.getLogger(__name__)


class FaceSearchBusyError(Exception):
    """Interactive face search timed out or failed on both GPU and CPU paths."""

_MAX_APPEARANCES_PER_MATCH = 24

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; DriveFaceIndexer/1.0; +https://mastersunion.org)"
    ),
    "Accept": "image/*,*/*;q=0.8",
}

# In-memory LRU+TTL cache for leadership portrait downloads (search-by-url).
_PORTRAIT_CACHE_MAX = 100
_PORTRAIT_CACHE_TTL_SEC = 600.0
_portrait_bytes_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()


class ImageUrlFetchError(Exception):
    """Failed to download a public portrait URL for reverse face search."""


def _portrait_cache_get(url: str) -> bytes | None:
    entry = _portrait_bytes_cache.get(url)
    if entry is None:
        return None
    ts, data = entry
    if time.monotonic() - ts > _PORTRAIT_CACHE_TTL_SEC:
        _portrait_bytes_cache.pop(url, None)
        return None
    _portrait_bytes_cache.move_to_end(url)
    return data


def _portrait_cache_put(url: str, data: bytes) -> None:
    _portrait_bytes_cache[url] = (time.monotonic(), data)
    _portrait_bytes_cache.move_to_end(url)
    while len(_portrait_bytes_cache) > _PORTRAIT_CACHE_MAX:
        _portrait_bytes_cache.popitem(last=False)


async def _appears_in_for_face(
    session: AsyncSession,
    *,
    person_id: int | None,
    cluster_id: int | None,
    limit: int = 24,
    offset: int = 0,
) -> list[dict[str, object]]:
    """Files where this matched face's person or unknown cluster appears."""
    if person_id is not None:
        face_filter = or_(
            Face.person_id == person_id,
            Face.cluster_id.in_(
                select(FaceCluster.id).where(FaceCluster.person_id == person_id)
            ),
        )
    elif cluster_id is not None:
        face_filter = Face.cluster_id == cluster_id
    else:
        return []

    # One row per DriveFile (same basis as _file_count_for_face) so pagination
    # totals and list lengths cannot disagree.
    type_rank = case(
        (Media.type == MediaType.IMAGE, 0),
        (Media.type == MediaType.VIDEO, 1),
        else_=2,
    )
    _type_by_rank = {
        0: MediaType.IMAGE.value,
        1: MediaType.VIDEO.value,
        2: MediaType.PDF.value,
    }
    stmt = (
        select(
            func.min(Media.id).label("media_id"),
            DriveFile.id,
            DriveFile.name,
            DriveFile.path,
            func.min(type_rank).label("type_rank"),
            func.min(Face.frame_timestamp).label("frame_timestamp"),
            func.max(Media.duration_seconds).label("duration_seconds"),
        )
        .join(Face, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(face_filter)
        .group_by(DriveFile.id, DriveFile.name, DriveFile.path)
        # Images before videos so gallery pages stay visually consistent.
        .order_by(
            func.min(type_rank),
            DriveFile.name,
            DriveFile.id,
        )
        .offset(max(0, offset))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "media_id": int(media_id),
            "drive_file_id": str(drive_id),
            "name": name,
            "path": path or "",
            "media_type": _type_by_rank.get(int(type_rank_val or 2), MediaType.IMAGE.value),
            "frame_timestamp": float(frame_timestamp) if frame_timestamp is not None else None,
            "duration_seconds": float(duration_seconds) if duration_seconds is not None else None,
        }
        for media_id, drive_id, name, path, type_rank_val, frame_timestamp, duration_seconds in rows
    ]


async def _file_count_for_face(
    session: AsyncSession,
    *,
    person_id: int | None,
    cluster_id: int | None,
) -> int:
    """Unique Drive files linked to this person (all merged clusters) or one cluster."""
    if person_id is not None:
        face_filter = or_(
            Face.person_id == person_id,
            Face.cluster_id.in_(
                select(FaceCluster.id).where(FaceCluster.person_id == person_id)
            ),
        )
    elif cluster_id is not None:
        face_filter = Face.cluster_id == cluster_id
    else:
        return 0

    stmt = (
        select(func.count(func.distinct(DriveFile.id)))
        .select_from(Face)
        .join(Media, Face.media_id == Media.id)
        .join(DriveFile, DriveFile.id == Media.drive_file_id)
        .where(face_filter)
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _face_count_for_person(session: AsyncSession, person_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Face)
        .where(
            or_(
                Face.person_id == person_id,
                Face.cluster_id.in_(
                    select(FaceCluster.id).where(FaceCluster.person_id == person_id)
                ),
            )
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _clusters_for_person(
    session: AsyncSession, person_id: int, limit: int = 8
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(FaceCluster.id, FaceCluster.status, FaceCluster.member_count, FaceCluster.representative_face_id)
            .where(FaceCluster.person_id == person_id)
            .order_by(FaceCluster.member_count.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "cluster_id": int(cid),
            "cluster_status": status.value if hasattr(status, "value") else str(status),
            "cluster_member_count": int(members),
            "representative_face_id": int(rep) if rep is not None else None,
        }
        for cid, status, members, rep in rows
    ]


def _identity_key(
    face_id: int, person_id: int | None, cluster_id: int | None
) -> tuple[str, int]:
    """Dedupe key for reverse-search matches.

    Clusters stay distinct even when several are linked to the same person, so
    the UI can show each face cluster instead of collapsing a person into one
    identity result. Person-linked faces without a cluster still group per
    person; unlinked faces fall back to their own face id.
    """
    if cluster_id is not None:
        return ("cluster", int(cluster_id))
    if person_id is not None:
        return ("person", int(person_id))
    return ("face", int(face_id))


def downscale_bgr_for_search(image_bgr: np.ndarray, max_edge: int) -> np.ndarray:
    """Cap long edge for interactive reverse face search; no-op when max_edge <= 0."""
    if max_edge <= 0:
        return image_bgr
    h, w = image_bgr.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_edge:
        return image_bgr
    scale = max_edge / float(long_edge)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)


async def _detect_query_faces(
    image_bgr: np.ndarray, *, settings: Settings
) -> tuple[list[DetectedFace], str]:
    """GPU detect when configured; CPU only if GPU was never configured. Raises FaceSearchBusyError."""
    from app.faces.runpod_gpu import RunPodFaceError, detect_faces_runpod, runpod_face_configured

    t0 = time.monotonic()

    if runpod_face_configured(settings):
        try:
            detections = await asyncio.wait_for(
                detect_faces_runpod(image_bgr, settings=settings),
                timeout=settings.face_search_gpu_timeout_seconds,
            )
            return detections, "gpu"
        except asyncio.TimeoutError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.warning(
                "face_search cause=gpu_timeout elapsed_ms=%.0f",
                elapsed_ms,
            )
            raise FaceSearchBusyError("Face search is busy, please try again") from exc
        except RunPodFaceError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.warning(
                "face_search cause=gpu_error elapsed_ms=%.0f",
                elapsed_ms,
                exc_info=True,
            )
            raise FaceSearchBusyError("Face search is busy, please try again") from exc

    try:
        engine = get_face_engine()
        detections = await asyncio.wait_for(
            run_cpu_bound(engine.detect_faces, image_bgr),
            timeout=settings.face_search_cpu_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise FaceSearchBusyError("Face search is busy, please try again") from exc
    except Exception as exc:  # noqa: BLE001
        raise FaceSearchBusyError("Face search is busy, please try again") from exc

    return detections, "cpu"


async def search_faces_by_image_bytes(
    session: AsyncSession,
    image_bytes: bytes,
    *,
    limit: int = 20,
    max_distance: float = 0.55,
) -> dict[str, object]:
    """Detect the largest face in an upload and ANN-match against indexed embeddings."""
    t_start = time.monotonic()
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise ValueError("Could not decode image")

    from app.config import get_settings

    settings = get_settings()
    image_bgr = downscale_bgr_for_search(image_bgr, settings.face_search_max_edge)

    t_detect = time.monotonic()
    detections, engine_name = await _detect_query_faces(image_bgr, settings=settings)
    detect_ms = (time.monotonic() - t_detect) * 1000.0

    if not detections:
        logger.info(
            "face_search detect_ms=%.0f match_ms=0 total_ms=%.0f engine=%s faces=0 matches=0",
            detect_ms,
            (time.monotonic() - t_start) * 1000.0,
            engine_name,
        )
        return {"faces_detected": 0, "matches": [], "message": "No face detected in upload"}

    # Prefer largest face by bbox area
    detections = sorted(
        detections,
        key=lambda d: float(d.bbox_width) * float(d.bbox_height),
        reverse=True,
    )
    query = detections[0]
    t_match = time.monotonic()
    dist = FaceEmbedding.embedding.cosine_distance(query.embedding).label("dist")
    identity_person_id = func.coalesce(Face.person_id, FaceCluster.person_id).label(
        "identity_person_id"
    )
    rows = (
        await session.execute(
            select(
                FaceEmbedding.face_id,
                dist,
                identity_person_id,
                Person.name,
                Face.cluster_id,
                FaceCluster.status,
                FaceCluster.member_count,
            )
            .join(Face, Face.id == FaceEmbedding.face_id)
            .outerjoin(FaceCluster, FaceCluster.id == Face.cluster_id)
            .outerjoin(Person, Person.id == identity_person_id)
            .order_by(dist)
            # Extra headroom: one person can legitimately surface several
            # clusters, so rows are consumed faster than one-per-identity.
            .limit(max(limit * 4, 60))
        )
    ).all()

    li_map = await linkedin_map(session)
    matches: list[dict[str, object]] = []
    seen_identities: set[tuple[str, int]] = set()
    person_clusters_cache: dict[int, list[dict[str, object]]] = {}
    person_face_count_cache: dict[int, int] = {}
    for face_id, distance, person_id, person_name, cluster_id, cluster_status, member_count in rows:
        d = float(distance)
        if d > max_distance:
            continue
        score = max(0.0, 1.0 - d)
        key = _identity_key(int(face_id), person_id, cluster_id)
        if key in seen_identities:
            continue
        seen_identities.add(key)

        name = person_name or "Unknown"
        pid = int(person_id) if person_id is not None else None
        cid = int(cluster_id) if cluster_id is not None else None
        # Cluster-keyed matches report cluster-scoped counts so each cluster
        # row stands on its own; person-keyed matches keep person-wide counts.
        count_person_id = pid if cid is None else None
        file_count = await _file_count_for_face(
            session, person_id=count_person_id, cluster_id=cid
        )
        # Keep reverse search bounded. Returning hundreds of files for every
        # candidate holds a transaction open and can exhaust the API DB pool.
        # file_count still reports the complete total to the UI.
        appears_limit = min(max(file_count, 1), _MAX_APPEARANCES_PER_MATCH)
        appears_in = await _appears_in_for_face(
            session, person_id=count_person_id, cluster_id=cid, limit=appears_limit
        )
        if pid is not None:
            if pid not in person_clusters_cache:
                person_clusters_cache[pid] = await _clusters_for_person(session, pid)
            person_clusters = person_clusters_cache[pid]
        else:
            person_clusters = []

        # If face lost cluster_id after naming, surface the person's top cluster for UI links
        if cid is None and person_clusters:
            top = person_clusters[0]
            cid = int(top["cluster_id"])  # type: ignore[arg-type]
            cluster_status_val = top["cluster_status"]
        else:
            cluster_status_val = (
                cluster_status.value
                if cluster_status is not None and hasattr(cluster_status, "value")
                else None
            )

        if cid is not None and cluster_id is not None:
            display_member_count = (
                int(member_count) if member_count is not None else None
            )
        elif pid is not None:
            # Person-wide match (no distinct cluster): badge spans all faces.
            if pid not in person_face_count_cache:
                person_face_count_cache[pid] = await _face_count_for_person(session, pid)
            display_member_count = person_face_count_cache[pid]
        else:
            display_member_count = int(member_count) if member_count is not None else None

        thumb_face_id, thumb_source = await resolve_face_thumbnail_id(session, int(face_id))

        matches.append(
            {
                "face_id": int(face_id),
                "thumb_face_id": thumb_face_id,
                "thumb_source": thumb_source,
                "match_scope": key[0],
                "person_id": pid,
                "person_name": name,
                "score": round(score, 4),
                "distance": round(d, 4),
                "linkedin_url": li_map.get(name) if person_name else None,
                "cluster_id": cid,
                "cluster_status": cluster_status_val,
                "cluster_member_count": display_member_count,
                "file_count": file_count,
                "appears_in": appears_in,
                "person_clusters": person_clusters,
            }
        )
        if len(matches) >= limit:
            break

    match_ms = (time.monotonic() - t_match) * 1000.0
    total_ms = (time.monotonic() - t_start) * 1000.0
    logger.info(
        "face_search detect_ms=%.0f match_ms=%.0f total_ms=%.0f engine=%s faces=%s matches=%s",
        detect_ms,
        match_ms,
        total_ms,
        engine_name,
        len(detections),
        len(matches),
    )
    return {
        "faces_detected": len(detections),
        "query_confidence": float(query.confidence),
        "matches": matches,
    }


async def search_faces_by_image_url(
    session: AsyncSession,
    image_url: str,
    *,
    limit: int = 20,
    max_distance: float = 0.55,
) -> dict[str, object]:
    """Download a public portrait URL and reverse-match faces (for leadership mini-cards)."""
    url = (image_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("image_url must be an http(s) URL")

    content = _portrait_cache_get(url)
    if content is None:
        try:
            async with httpx.AsyncClient(
                timeout=45.0, follow_redirects=True, headers=_FETCH_HEADERS
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and "image" not in content_type and "octet-stream" not in content_type:
                    # Some CDNs omit/mislabel; still try decode if body looks like an image
                    if len(resp.content) < 64:
                        raise ImageUrlFetchError("Couldn't load this person's photo")
                if len(resp.content) > 12 * 1024 * 1024:
                    raise ValueError("Image too large (max 12MB)")
                content = resp.content
        except ImageUrlFetchError:
            raise
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("portrait download failed url=%s: %s", url[:120], exc)
            raise ImageUrlFetchError("Couldn't load this person's photo") from None
        _portrait_cache_put(url, content)

    return await search_faces_by_image_bytes(
        session, content, limit=limit, max_distance=max_distance
    )
