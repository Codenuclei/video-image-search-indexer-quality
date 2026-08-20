from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pydantic import BaseModel

from app.db.models import DriveFile, DriveFileStatus
from app.db.session import get_db
from app.dependencies import get_indexing_worker
from app.drive.client import DriveConnectorError
from app.drive.google_client import DriveDirectClient, DriveDirectError
from app.drive.cleanup import remove_drive_file
from app.drive.library_tree import (
    build_library_shell,
    build_library_tree,
    compute_library_revision,
    folder_node_to_dict,
    folder_node_to_shell_dict,
    is_direct_child_of_folder,
    thin_file_dict,
)
from app.drive.library_shell_cache import compute_library_revision_sql, get_library_shell_cache
from app.drive.library_reader_runtime import get_library_reader_runtime
from app.drive.indexing_pause import (
    load_paused_folder_paths,
    normalize_folder_path,
    pause_folder_indexing,
    resume_folder_indexing,
    skip_corrupt_files,
)
from app.gemini.service import get_gemini_service
from app.pipelines.common import download_to_memory
from app.schemas import DriveFileOut
from app.workers.indexer import IndexingWorker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["drive"])


class FolderIndexingAction(BaseModel):
    folder_path: str


def _parse_drive_file_status(status: str | None) -> DriveFileStatus | None:
    """Coerce queue/UI status query params to DriveFileStatus (e.g. Active → processing)."""
    if not status:
        return None
    raw = status.strip().lower()
    # UI label aliases used by the Folders indexing queue tabs.
    aliases = {"active": "processing", "completed": "processed", "failed": "error"}
    raw = aliases.get(raw, raw)
    try:
        return DriveFileStatus(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status {status!r}. Expected one of: "
            + ", ".join(s.value for s in DriveFileStatus),
        ) from exc


def _live_drive_http_status(exc: BaseException) -> int:
    """Map live-Drive auth failures to 401; other upstream failures stay 502."""
    msg = str(exc).lower()
    if (
        "not connected" in msg
        or "reconnect google drive" in msg
        or "no google drive account" in msg
        or "no refresh token" in msg
        or "no drive folder selected" in msg
    ):
        return 401
    return 502


@router.get("/files", response_model=list[DriveFileOut])
async def list_drive_files(
    status: str | None = None,
    source: str | None = None,
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_db),
) -> list[DriveFile]:
    """Lists files as currently tracked from the connected Drive folder."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    stmt = select(DriveFile).order_by(DriveFile.path).offset(offset).limit(limit)
    status_enum = _parse_drive_file_status(status)
    if status_enum is not None:
        stmt = stmt.where(DriveFile.status == status_enum)
    if source:
        stmt = stmt.where(DriveFile.source == source)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/files/page")
async def list_drive_files_page(
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Paginated file list with total count for queue modals."""
    from sqlalchemy import func as sa_func

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    filters = []
    status_enum = _parse_drive_file_status(status)
    if status_enum is not None:
        filters.append(DriveFile.status == status_enum)
    if source:
        filters.append(DriveFile.source == source)
    count_stmt = select(sa_func.count()).select_from(DriveFile)
    list_stmt = select(DriveFile).order_by(DriveFile.path).offset(offset).limit(limit)
    for f in filters:
        count_stmt = count_stmt.where(f)
        list_stmt = list_stmt.where(f)
    total = int((await session.execute(count_stmt)).scalar_one())
    items = list((await session.execute(list_stmt)).scalars().all())
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [DriveFileOut.model_validate(i) for i in items],
    }

@router.get("/files/lookup-faces")
async def lookup_file_faces(
    name: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Diagnostic: resolve a Drive file by name substring, list its faces/clusters,
    and report cosine similarity to named people + nearest other clusters.
    """
    from app.config import get_settings
    from app.db.models import Face, FaceCluster, FaceEmbedding, Media, Person
    from app.matching.service import cosine_similarity
    import numpy as np

    needle = (name or "").strip()
    if len(needle) < 3:
        raise HTTPException(status_code=400, detail="name must be at least 3 characters")

    df = (
        await session.execute(
            select(DriveFile).where(DriveFile.name.ilike(f"%{needle}%")).limit(1)
        )
    ).scalar_one_or_none()
    if df is None:
        raise HTTPException(status_code=404, detail=f"No drive file matching {needle!r}")

    media = (
        await session.execute(select(Media).where(Media.drive_file_id == df.id))
    ).scalar_one_or_none()
    faces = (
        list((await session.execute(select(Face).where(Face.media_id == media.id))).scalars().all())
        if media
        else []
    )

    persons = list((await session.execute(select(Person))).scalars().all())
    person_cents: list[tuple[Person, list[float], int]] = []
    for person in persons:
        fems = (
            await session.execute(
                select(FaceEmbedding)
                .join(Face, Face.id == FaceEmbedding.face_id)
                .where(Face.person_id == person.id)
                .limit(40)
            )
        ).scalars().all()
        if not fems:
            continue
        cent = (
            sum(np.asarray(x.embedding, dtype=np.float32) for x in fems) / len(fems)
        ).tolist()
        person_cents.append((person, cent, len(fems)))

    threshold = get_settings().person_match_threshold
    face_payloads: list[dict[str, object]] = []
    for face in faces:
        cluster = await session.get(FaceCluster, face.cluster_id) if face.cluster_id else None
        person = await session.get(Person, face.person_id) if face.person_id else None
        emb_row = await session.get(FaceEmbedding, face.id)
        area = float(face.bbox_width * face.bbox_height)
        aspect = float(face.bbox_width / face.bbox_height) if face.bbox_height else 0.0
        entry: dict[str, object] = {
            "face_id": face.id,
            "detection_confidence": face.detection_confidence,
            "bbox": {
                "x": face.bbox_x,
                "y": face.bbox_y,
                "w": face.bbox_width,
                "h": face.bbox_height,
                "area": area,
                "aspect_w_h": aspect,
            },
            "cluster_id": face.cluster_id,
            "cluster_status": cluster.status.value if cluster else None,
            "cluster_member_count": cluster.member_count if cluster else None,
            "person_id": face.person_id,
            "person_name": person.name if person else None,
            "match_threshold": threshold,
        }
        if emb_row is not None:
            vs_persons = sorted(
                (
                    {
                        "similarity": round(cosine_similarity(list(emb_row.embedding), cent), 4),
                        "person_id": p.id,
                        "person_name": p.name,
                        "sample_faces": n,
                    }
                    for p, cent, n in person_cents
                ),
                key=lambda x: float(x["similarity"]),
                reverse=True,
            )[:8]
            entry["vs_named_people"] = vs_persons

            if cluster is not None and cluster.centroid is not None:
                others = (
                    await session.execute(
                        select(FaceCluster).where(
                            FaceCluster.centroid.isnot(None),
                            FaceCluster.id != cluster.id,
                        )
                    )
                ).scalars().all()
                scored: list[dict[str, object]] = []
                for other in others:
                    sim = cosine_similarity(list(cluster.centroid), list(other.centroid))
                    other_person = (
                        await session.get(Person, other.person_id) if other.person_id else None
                    )
                    scored.append(
                        {
                            "similarity": round(sim, 4),
                            "cluster_id": other.id,
                            "status": other.status.value,
                            "member_count": other.member_count,
                            "person_id": other.person_id,
                            "person_name": other_person.name if other_person else None,
                            "representative_face_id": other.representative_face_id,
                            "band": (
                                "auto_match"
                                if sim >= threshold
                                else "near_miss"
                                if sim >= threshold - 0.15
                                else "weak"
                            ),
                        }
                    )
                scored.sort(key=lambda x: float(x["similarity"]), reverse=True)
                entry["vs_clusters_top"] = scored[:15]
                entry["near_miss_named"] = [
                    s for s in scored if s["band"] == "near_miss" and s["person_name"]
                ][:10]
        face_payloads.append(entry)

    return {
        "file": {
            "id": df.id,
            "name": df.name,
            "path": df.path,
            "status": df.status.value if hasattr(df.status, "value") else str(df.status),
        },
        "media_id": media.id if media else None,
        "person_match_threshold": threshold,
        "faces": face_payloads,
    }


@router.get("/library/revision")
async def drive_library_revision(session: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Cheap freshness check — SQL aggregates only, no tree build, no Qdrant."""
    return {"revision": await compute_library_revision_sql(session)}


@router.get("/library/shell")
async def drive_library_shell(
    session: AsyncSession = Depends(get_db),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Response:
    """Folder tree + DB status counts only (no files[], no Qdrant). Shared process cache.

    Revision is computed with SQL aggregates first so cache hits / 304 skip the full row load.
    """
    from app.workers.maintenance import maintenance_status

    revision = await compute_library_revision_sql(session)
    etag = f'"{revision}"'

    if if_none_match and if_none_match.strip() in (etag, revision, f"W/{etag}"):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=5"},
        )

    cache = get_library_shell_cache()
    cached = cache.get(revision)
    if cached is not None:
        return JSONResponse(
            content=cached,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=5",
                "X-Library-Cache": "hit",
            },
        )

    # Cache miss — load rows and build shell once for this revision.
    rows = list(
        (await session.execute(select(DriveFile).order_by(DriveFile.path))).scalars().all()
    )
    paused_paths = await load_paused_folder_paths(session)
    root, summary = build_library_shell(rows, paused_folder_paths=paused_paths)
    payload: dict[str, object] = {
        "tree": folder_node_to_shell_dict(root),
        "summary": summary,
        "maintenance": maintenance_status(),
        "paused_folders": paused_paths,
        "revision": revision,
    }
    cache.put(revision, payload)
    return JSONResponse(
        content=payload,
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=5",
            "X-Library-Cache": "miss",
        },
    )


@router.get("/library/folder")
async def drive_library_folder(
    path: str = Query(default="/"),
    limit: int = Query(default=150, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> dict[str, object]:
    """Serve one folder entirely on the reserved Library thread + DB connection."""
    reader = get_library_reader_runtime()
    return await reader.run(_drive_library_folder_sync, path, limit, cursor)


def _drive_library_folder_sync(
    path: str,
    limit: int,
    cursor: str | None,
) -> dict[str, object]:
    """Blocking folder read. Runs only on LibraryReaderRuntime's single thread."""
    from app.drive.library_filters import is_apple_junk_row, is_folder_marker_row
    from app.qdrant.image_captions import valid_caption_ids_sync
    from app.qdrant.images import existing_image_ids_sync
    from sqlalchemy import and_, func, not_, or_

    folder = normalize_folder_path(path)

    if folder == "/":
        # Historical rows exist both with and without a leading slash.
        # Direct root children are "/name" or "name", with no nested slash.
        path_filter = or_(
            and_(DriveFile.path.like("/%"), not_(DriveFile.path.like("/%/%"))),
            not_(DriveFile.path.like("%/%")),
        )
    else:
        # Direct children may be stored as "/folder/name" or "folder/name".
        relative_folder = folder.lstrip("/")
        path_filter = or_(
            and_(
                DriveFile.path.like(f"{folder}/%"),
                not_(DriveFile.path.like(f"{folder}/%/%")),
            ),
            and_(
                DriveFile.path.like(f"{relative_folder}/%"),
                not_(DriveFile.path.like(f"{relative_folder}/%/%")),
            ),
        )

    reader = get_library_reader_runtime()
    with reader.session() as session:
        rows = list(
            session.execute(
                select(DriveFile).where(path_filter).order_by(DriveFile.name)
            ).scalars().all()
        )

        children: list[DriveFile] = []
        for df in rows:
            if is_apple_junk_row(name=df.name, error_message=df.error_message):
                continue
            if is_folder_marker_row(mime_type=df.mime_type, error_message=df.error_message):
                continue
            if is_direct_child_of_folder(df.path, folder):
                children.append(df)

        children.sort(key=lambda d: (d.name or "").lower())

        start = 0
        if cursor:
            for i, df in enumerate(children):
                if df.id == cursor:
                    start = i + 1
                    break

        page = children[start : start + limit]
        image_ids = [df.id for df in page if df.mime_type.startswith("image/")]
        captioned_ids = valid_caption_ids_sync(image_ids) if image_ids else set()
        embedded_ids = existing_image_ids_sync(image_ids) if image_ids else set()

        files = [
            thin_file_dict(
                df,
                has_caption=df.id in captioned_ids,
                has_embedding=df.id in embedded_ids,
            )
            for df in page
        ]

        cache = get_library_shell_cache()
        revision = cache.get_recent_revision(60.0)
        if revision is None:
            count = int(session.scalar(select(func.count()).select_from(DriveFile)) or 0)
            max_synced = session.scalar(select(func.max(DriveFile.last_synced_at)))
            hist_rows = session.execute(
                select(DriveFile.status, func.count()).group_by(DriveFile.status)
            ).all()
            status_hist = {
                status.value if hasattr(status, "value") else str(status): int(n)
                for status, n in hist_rows
            }
            hist_part = ",".join(
                f"{key}:{status_hist[key]}" for key in sorted(status_hist)
            )
            revision = f"{count}:{max_synced.isoformat() if max_synced else ''}:{hist_part}"
            cache.put_revision(revision)

        next_cursor = (
            page[-1].id
            if len(page) == limit and (start + limit) < len(children)
            else None
        )
        return {
            "path": folder,
            "files": files,
            "total": len(children),
            "limit": limit,
            "next_cursor": next_cursor,
            "revision": revision,
        }


@router.get("/library")
async def drive_library(session: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Global historical library tree — all indexed roots, including soft-archived.

    Not scoped to the current Drive OAuth session or active folder selection.
    Soft-archived files remain visible so folder switch / disconnect never hides
    previously indexed media, captions, or embeddings.

    Prefer /library/shell + /library/folder for UI (faster TTFB).
    """
    from app.qdrant.image_captions import get_captions_by_ids_sync, valid_caption_ids_sync
    from app.qdrant.images import existing_image_ids_sync
    from app.workers.maintenance import maintenance_status

    rows = list(
        (await session.execute(select(DriveFile).order_by(DriveFile.path))).scalars().all()
    )
    image_ids = [df.id for df in rows if df.mime_type.startswith("image/")]

    captioned_ids: set[str] = set()
    embedded_ids: set[str] = set()
    caption_texts: dict[str, str] = {}

    if image_ids:
        valid_ids, embedded_ids, caption_texts = await asyncio.gather(
            asyncio.to_thread(valid_caption_ids_sync, image_ids),
            asyncio.to_thread(existing_image_ids_sync, image_ids),
            asyncio.to_thread(get_captions_by_ids_sync, image_ids),
        )
        captioned_ids = valid_ids

    paused_paths = await load_paused_folder_paths(session)

    root, _all_files, summary = build_library_tree(
        rows,
        captioned_ids=captioned_ids,
        embedded_ids=embedded_ids,
        caption_texts=caption_texts,
        paused_folder_paths=paused_paths,
    )

    revision = compute_library_revision(rows)

    return {
        "tree": folder_node_to_dict(root),
        "summary": summary,
        "maintenance": maintenance_status(),
        "paused_folders": paused_paths,
        "revision": revision,
    }


@router.post("/sync")
async def sync_drive_files(
    background_tasks: BackgroundTasks,
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, str | int | bool]:
    """Fetch the latest Drive folder listing into the database."""
    if worker.is_running:
        raise HTTPException(status_code=409, detail="An indexing run is already in progress")

    async def _sync() -> None:
        try:
            seen = await worker.sync_file_list(cache_source="manual")
            logger.info("Manual Drive file-list sync: %d file(s)", seen)
        except Exception:  # noqa: BLE001
            logger.exception("Manual Drive file-list sync failed")

    background_tasks.add_task(_sync)
    return {"ok": True, "scheduled": True}


@router.post("/library/folders/pause")
async def pause_folder(
    body: FolderIndexingAction,
    session: AsyncSession = Depends(get_db),
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> dict[str, object]:
    """Stop indexing all files under a library folder."""
    stopped = await pause_folder_indexing(session, body.folder_path)
    await session.commit()
    cancelled = await worker.cancel_indexing_under_folder(body.folder_path)
    get_library_shell_cache().invalidate()
    return {"ok": True, "folder_path": body.folder_path, "stopped": stopped, "cancelled": cancelled}


@router.post("/library/folders/resume")
async def resume_folder(
    body: FolderIndexingAction,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Re-enable indexing for a paused library folder."""
    resumed = await resume_folder_indexing(session, body.folder_path)
    await session.commit()
    get_library_shell_cache().invalidate()
    return {"ok": True, "folder_path": body.folder_path, "resumed": resumed}


@router.post("/skip-corrupt")
async def skip_corrupt(
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Permanently skip corrupt/unreadable pending files so other folders keep indexing."""
    skipped = await skip_corrupt_files(session)
    await session.commit()
    return {"ok": True, "skipped": skipped}


@router.post("/files/{file_id}/retry", response_model=DriveFileOut)
async def retry_drive_file(
    file_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    worker: IndexingWorker = Depends(get_indexing_worker),
) -> DriveFile:
    from app.pipelines.common import file_has_media

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    # Permanent library: PROCESSED with media is add-if-missing complete — no wipe/requeue.
    if drive_file.status == DriveFileStatus.PROCESSED and await file_has_media(
        session, drive_file.id
    ):
        await session.refresh(drive_file)
        return drive_file

    if drive_file.gemini_document_name:
        gemini = get_gemini_service()
        gemini.delete_document(drive_file.gemini_document_name)

    drive_file.status = DriveFileStatus.PENDING
    drive_file.error_message = None
    drive_file.gemini_document_name = None
    await session.commit()
    await session.refresh(drive_file)

    if not worker.is_running:
        background_tasks.add_task(worker.run_cycle, 1)
    return drive_file


@router.delete("/files/{file_id}", status_code=204)
async def delete_drive_file(file_id: str, session: AsyncSession = Depends(get_db)) -> None:
    stmt = select(DriveFile).where(DriveFile.id == file_id).options(selectinload(DriveFile.media))
    drive_file = (await session.execute(stmt)).scalar_one_or_none()
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")
    gemini = get_gemini_service()
    await remove_drive_file(
        session,
        drive_file,
        gemini=gemini,
        reason="explicit API detach",
    )
    await session.commit()


@router.get("/files/{file_id}/thumbnail")
async def thumbnail_drive_file(
    file_id: str,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """Serve a compressed JPEG thumb from disk. Never contacts Drive."""
    from app.config import get_settings
    from app.drive.image_thumbs import image_thumb_path, write_image_thumbnail
    from app.drive.media_cache import resolve_cache_path
    from app.pipelines.common import is_image_mime, is_video_mime

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")
    if is_video_mime(drive_file.mime_type) or not is_image_mime(
        drive_file.mime_type, drive_file.name
    ):
        raise HTTPException(status_code=404, detail="No image thumbnail")

    settings = get_settings()
    dest = image_thumb_path(settings, drive_file.id)
    if not dest.is_file() or dest.stat().st_size <= 0:
        cached = resolve_cache_path(settings, drive_file)
        if cached is None:
            raise HTTPException(status_code=404, detail="Thumbnail not ready")
        try:
            dest = await asyncio.to_thread(
                write_image_thumbnail,
                cached,
                drive_file.id,
                settings,
                drive_file.name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("thumbnail generate failed for %s: %s", file_id, exc)
            raise HTTPException(status_code=404, detail="Thumbnail not ready") from exc

    return FileResponse(
        dest,
        media_type="image/jpeg",
        filename=f"{drive_file.id}.jpg",
        headers={
            "Cache-Control": "public, max-age=604800",
            "Content-Disposition": f'inline; filename="{drive_file.id}.jpg"',
        },
    )


@router.get("/files/{file_id}/preview")
async def preview_drive_file(
    file_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """Return indexed file bytes for inline preview in the UI."""
    from app.config import get_settings
    from app.pipelines.common import is_video_mime
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    settings = get_settings()
    local_path = video_cache_path(settings, drive_file)
    if is_video_mime(drive_file.mime_type) and local_path.is_file():
        media_type = drive_file.mime_type or "video/mp4"
        return FileResponse(
            local_path,
            media_type=media_type,
            filename=drive_file.name,
            headers={"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{drive_file.name}"'},
        )

    # Images are served from the durable indexed-media cache when available.
    # This avoids browser requests to drive.google.com, whose thumbnail endpoint
    # applies the viewer's Google account and returns 403 for other accounts.
    if not is_video_mime(drive_file.mime_type):
        from app.drive.media_cache import resolve_cache_path

        cached_path = resolve_cache_path(settings, drive_file)
        if cached_path is not None:
            return FileResponse(
                cached_path,
                media_type=drive_file.mime_type or "application/octet-stream",
                filename=drive_file.name,
                headers={"Content-Disposition": f'inline; filename="{drive_file.name}"'},
            )

    if is_youtube_source(drive_file):
        if local_path.is_file():
            media_type = drive_file.mime_type or "video/webm"
            return FileResponse(
                local_path,
                media_type=media_type,
                filename=drive_file.name,
                headers={"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{drive_file.name}"'},
            )
        raise HTTPException(status_code=404, detail="YouTube local file not on volume yet")

    from app.db.session import get_session_factory

    client = DriveDirectClient(session_factory=get_session_factory(), settings=settings)
    range_header = request.headers.get("range")
    stream_context = client.stream_file_content(file_id, range_header=range_header)
    try:
        upstream = await stream_context.__aenter__()
    except (DriveConnectorError, DriveDirectError) as exc:
        raise HTTPException(
            status_code=_live_drive_http_status(exc),
            detail=str(exc),
        ) from exc

    media_type = drive_file.mime_type or "application/octet-stream"
    headers = {
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
        "Content-Disposition": f'inline; filename="{drive_file.name}"',
    }
    for name in ("content-range", "content-length", "etag", "last-modified"):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(stream_context.__aexit__, None, None, None),
    )


@router.get("/files/{file_id}/download")
async def download_drive_file(file_id: str, session: AsyncSession = Depends(get_db)) -> Response:
    """Download indexed file bytes as an attachment."""
    from app.config import get_settings
    from app.video.youtube_cache import video_cache_path
    from app.video.youtube_registry import is_youtube_source

    drive_file = await session.get(DriveFile, file_id)
    if drive_file is None:
        raise HTTPException(status_code=404, detail="File not found")

    settings = get_settings()
    local_path = video_cache_path(settings, drive_file)
    # Cache-first for all sources so disconnect does not block already-indexed media.
    if local_path.is_file():
        media_type = drive_file.mime_type or (
            "video/webm" if is_youtube_source(drive_file) else "application/octet-stream"
        )
        return FileResponse(
            local_path,
            media_type=media_type,
            filename=drive_file.name,
            headers={"Content-Disposition": f'attachment; filename="{drive_file.name}"'},
        )

    if is_youtube_source(drive_file):
        raise HTTPException(status_code=404, detail="YouTube local file not on volume yet")

    from app.db.session import get_session_factory

    client = DriveDirectClient(session_factory=get_session_factory(), settings=settings)
    try:
        content = await download_to_memory(client, file_id)
    except (DriveConnectorError, DriveDirectError) as exc:
        raise HTTPException(
            status_code=_live_drive_http_status(exc),
            detail=str(exc),
        ) from exc

    media_type = drive_file.mime_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{drive_file.name}"'},
    )
