from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile, Face, Media, MediaType, Person, VideoSegment
from app.gemini.rerank import rerank_moments
from app.gemini.tags import person_names_for_drive_file
from app.gemini.video_embeddings import embed_text_sync
from app.qdrant.client import search_frames_sync
from app.schemas import SearchMoment

logger = logging.getLogger(__name__)


async def search_video_moments(
    session: AsyncSession,
    *,
    query: str,
    visual_query: str,
    person_name: str | None,
    folder_path: str | None = None,
    folder_context: str | None = None,
    rerank: bool = True,
) -> list[SearchMoment]:
    settings = get_settings()
    if not settings.video_indexing_enabled:
        return []

    search_text = (visual_query or query).strip()
    if not search_text:
        return []

    # Resolve best face thumbnail for person-context re-ranking
    face_thumbnail_path: str | None = None
    if person_name:
        face_thumbnail_path = await _best_face_thumbnail(session, person_name)

    # Gemini Embedding 2 (via Qdrant) handles visual video search.
    # Transcript keyword search + face-timestamp moments run in parallel.
    transcript_task = asyncio.create_task(_transcript_moments(session, search_text, person_name, folder_path))
    gemini_task     = asyncio.create_task(_gemini_moments(session, search_text, person_name, folder_path))
    face_task       = asyncio.create_task(_face_moments(session, person_name, folder_path)) if person_name else None

    if face_task:
        transcript_hits, gemini_hits, face_hits = await asyncio.gather(
            transcript_task, gemini_task, face_task
        )
    else:
        transcript_hits, gemini_hits = await asyncio.gather(transcript_task, gemini_task)
        face_hits = []

    # Re-rank Gemini visual hits — filter out outliers via Gemini multimodal
    if rerank and gemini_hits:
        gemini_hits = await rerank_moments(
            search_text,
            gemini_hits,
            person_name=person_name,
            face_thumbnail_path=face_thumbnail_path,
            folder_context=folder_context,
            thumbnail_dir=settings.thumbnail_dir,
        )

    merged: list[SearchMoment] = []
    seen: set[tuple[str, float]] = set()

    # Face-detected moments first (highest precision), then Gemini, then transcript
    for moment in face_hits + gemini_hits + transcript_hits:
        key = (moment.drive_file_id, round(moment.timestamp_sec, 2))
        if key in seen:
            continue
        seen.add(key)
        merged.append(moment)

    merged.sort(key=lambda m: -(m.score or 0))
    return merged[:settings.gemini_video_result_limit]


async def _best_face_thumbnail(session: AsyncSession, person_name: str) -> str | None:
    """Return the thumbnail path of the best face for *person_name*, or None."""
    row = (
        await session.execute(
            select(Face)
            .join(Person, Face.person_id == Person.id)
            .where(Person.name == person_name, Face.thumbnail_path.isnot(None))
            .order_by(Face.detection_confidence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.thumbnail_path if row else None


async def _transcript_moments(
    session: AsyncSession,
    query: str,
    person_name: str | None,
    folder_path: str | None = None,
) -> list[SearchMoment]:
    tokens = [t for t in query.lower().split() if len(t) > 2]
    if not tokens:
        tokens = [query.lower()]

    stmt = (
        select(VideoSegment, Media, DriveFile)
        .join(Media, VideoSegment.media_id == Media.id)
        .join(DriveFile, Media.drive_file_id == DriveFile.id)
        .where(Media.type == MediaType.VIDEO)
    )
    if folder_path and folder_path.strip() and folder_path.strip() != "/":
        fp = folder_path.strip().rstrip("/")
        stmt = stmt.where(
            (DriveFile.path.startswith(fp + "/")) | (DriveFile.path == fp)
        )
    rows = (await session.execute(stmt)).all()
    moments: list[SearchMoment] = []

    for segment, media, drive_file in rows:
        hay = f"{segment.text} {segment.vlm_description or ''}".lower()
        if not any(tok in hay for tok in tokens):
            continue

        person_names = await person_names_for_drive_file(session, drive_file.id)
        if person_name and not _person_matches(person_name, person_names, hay):
            continue

        score = sum(1.0 for tok in tokens if tok in hay) / len(tokens)
        snippet = (segment.text or segment.vlm_description or "")[:240] or None
        moments.append(
            _build_moment(
                drive_file,
                timestamp_sec=segment.start_sec,
                end_timestamp_sec=segment.end_sec,
                match_type="transcript",
                snippet=snippet,
                person_names=person_names,
                score=score,
            )
        )

    return moments



async def _gemini_moments(
    session: AsyncSession,
    query: str,
    person_name: str | None,
    folder_path: str | None = None,
) -> list[SearchMoment]:
    """
    Embed query with Gemini Embedding 2, search Qdrant, map hits to SearchMoment.

    Each Qdrant point stores {drive_file_id, timestamp} embedded at index time,
    so we can look up the DriveFile directly and build exact-second timestamps.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        logger.debug("Gemini video search: no API key — skipping")
        return []

    # Multi-query fusion: expand the query into visual variants, embed + search
    # each, then fuse hits by max score per (file, timestamp) for higher recall.
    if settings.search_query_expansion:
        from app.gemini.query_expand import expand_queries_sync
        queries = list(await asyncio.to_thread(expand_queries_sync, query))
    else:
        queries = [query]

    try:
        fused: dict[tuple[str, float], dict] = {}
        for variant in queries:
            vec = await asyncio.to_thread(embed_text_sync, variant)
            for hit in await asyncio.to_thread(
                search_frames_sync,
                vec,
                limit=settings.gemini_video_result_limit,
                min_score=settings.gemini_video_min_score,
            ):
                key = (hit["drive_file_id"], round(hit["timestamp"], 3))
                if key not in fused or hit["score"] > fused[key]["score"]:
                    fused[key] = hit
        raw_hits = sorted(fused.values(), key=lambda h: -h["score"])[: settings.gemini_video_result_limit]
    except Exception as exc:
        logger.warning("Gemini video search failed (query=%r): %s", query, exc)
        return []

    if not raw_hits:
        return []

    moments: list[SearchMoment] = []
    for hit in raw_hits:
        drive_file_id = hit["drive_file_id"]
        timestamp     = hit["timestamp"]
        score         = hit["score"]

        drive_file: DriveFile | None = await session.get(DriveFile, drive_file_id)
        if drive_file is None:
            continue

        # Apply folder filter to Qdrant hits
        if folder_path and folder_path.strip() and folder_path.strip() != "/":
            fp = folder_path.strip().rstrip("/")
            if not (drive_file.path.startswith(fp + "/") or drive_file.path == fp):
                continue

        person_names = await person_names_for_drive_file(session, drive_file_id)
        if person_name and not any(n.lower() == person_name.lower() for n in person_names):
            continue

        ts = f"{timestamp:.3f}"
        moments.append(SearchMoment(
            drive_file_id=drive_file_id,
            name=drive_file.name,
            path=drive_file.path,
            mime_type=drive_file.mime_type,
            timestamp_sec=timestamp,
            end_timestamp_sec=None,
            match_type="gemini_visual",
            fennec_scene_id=None,
            preview_url=f"/media/video/{drive_file_id}/frame?ts={ts}",
            video_url=f"/drive/files/{drive_file_id}/preview#t={ts}",
            person_names=person_names,
            snippet=None,
            score=score,
        ))

    logger.info("Gemini video search: %d moments for query %r", len(moments), query)
    return moments


async def _face_moments(
    session: AsyncSession,
    person_name: str,
    folder_path: str | None = None,
) -> list[SearchMoment]:
    """
    Return one SearchMoment per video frame where *person_name*'s face was detected.

    This is the highest-precision source for person+video search — it shows
    exactly the timestamps where the person is visible, directly from the
    InsightFace detections that were stored when the video was indexed.
    """
    stmt = (
        select(Face, Media, DriveFile)
        .join(Media, Face.media_id == Media.id)
        .join(DriveFile, Media.drive_file_id == DriveFile.id)
        .join(Person, Face.person_id == Person.id)
        .where(Media.type == MediaType.VIDEO)
        .where(Person.name == person_name)
        .where(Face.frame_timestamp.isnot(None))
        .order_by(DriveFile.id, Face.frame_timestamp)
    )
    if folder_path and folder_path.strip() and folder_path.strip() != "/":
        fp = folder_path.strip().rstrip("/")
        stmt = stmt.where(
            (DriveFile.path.startswith(fp + "/")) | (DriveFile.path == fp)
        )
    rows = (await session.execute(stmt)).all()

    moments: list[SearchMoment] = []
    for face, media, drive_file in rows:
        ts = face.frame_timestamp
        ts_str = f"{ts:.3f}"
        moments.append(SearchMoment(
            drive_file_id=drive_file.id,
            name=drive_file.name,
            path=drive_file.path,
            mime_type=drive_file.mime_type,
            timestamp_sec=ts,
            end_timestamp_sec=None,
            match_type="face_detected",
            fennec_scene_id=None,
            preview_url=f"/media/video/{drive_file.id}/frame?ts={ts_str}",
            video_url=f"/drive/files/{drive_file.id}/preview#t={ts_str}",
            person_names=[person_name],
            snippet=f"{person_name} detected (confidence {face.detection_confidence:.0%})",
            score=face.detection_confidence,
        ))

    logger.info("Face detection contributed %d moments for person %r", len(moments), person_name)
    return moments


def _person_matches(person_name: str, person_names: list[str], haystack: str) -> bool:
    if any(n.lower() == person_name.lower() for n in person_names):
        return True
    return person_name.lower() in haystack


def _build_moment(
    drive_file: DriveFile,
    *,
    timestamp_sec: float,
    end_timestamp_sec: float | None,
    match_type: str,
    snippet: str | None,
    person_names: list[str],
    score: float,
) -> SearchMoment:
    ts = f"{timestamp_sec:.2f}"
    return SearchMoment(
        drive_file_id=drive_file.id,
        name=drive_file.name,
        path=drive_file.path,
        mime_type=drive_file.mime_type,
        timestamp_sec=timestamp_sec,
        end_timestamp_sec=end_timestamp_sec,
        match_type=match_type,
        fennec_scene_id=None,
        preview_url=f"/media/video/{drive_file.id}/frame?ts={ts}",
        video_url=f"/drive/files/{drive_file.id}/preview#t={ts}",
        person_names=person_names,
        snippet=snippet,
        score=score,
    )
