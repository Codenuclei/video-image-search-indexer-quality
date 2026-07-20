from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.runtime_settings import get_runtime_settings
from app.db.models import DriveFile, Face, Media, MediaType, Person, VideoSegment
from app.gemini.rerank import rerank_moments, rerank_transcript_moments
from app.gemini.tags import person_names_for_drive_file
from app.gemini.video_embeddings import embed_text_sync
from app.qdrant.client import search_frames_sync
from app.schemas import SearchMoment
from app.search.local import (
    action_match_keywords,
    caption_contradicts_action,
    caption_matches_action,
    is_action_query,
)
from app.search.transcript_match import score_transcript_match

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
    action_query: bool | None = None,
    source: str | None = None,
) -> list[SearchMoment]:
    settings = get_settings()
    if not settings.video_indexing_enabled:
        return []

    search_text = (visual_query or query).strip()
    if not search_text:
        return []

    is_action = action_query if action_query is not None else is_action_query(search_text)
    use_rerank = rerank and get_runtime_settings().search_rerank_enabled
    strict = True
    source_filter = (source or "").strip().lower() or None
    if source_filter in ("", "all"):
        source_filter = None

    # Resolve best face thumbnail for person-context re-ranking
    face_thumbnail_path: str | None = None
    if person_name:
        face_thumbnail_path = await _best_face_thumbnail(session, person_name)

    # Gemini Embedding 2 (via Qdrant) handles visual video search.
    # Transcript keyword search + face-timestamp moments run in parallel.
    transcript_task = asyncio.create_task(_transcript_moments(session, search_text, person_name, folder_path))
    regex_transcript_task = asyncio.create_task(
        _regex_transcript_moments(session, search_text, person_name, folder_path)
    )
    gemini_task     = asyncio.create_task(_gemini_moments(session, search_text, person_name, folder_path))
    face_task       = asyncio.create_task(_face_moments(session, person_name, folder_path)) if person_name else None

    if face_task:
        transcript_hits, regex_transcript_hits, gemini_hits, face_hits = await asyncio.gather(
            transcript_task, regex_transcript_task, gemini_task, face_task
        )
    else:
        transcript_hits, regex_transcript_hits, gemini_hits = await asyncio.gather(
            transcript_task, regex_transcript_task, gemini_task
        )
        face_hits = []

    gemini_reranked = False
    if use_rerank and gemini_hits:
        gemini_hits = await rerank_moments(
            search_text,
            gemini_hits,
            person_name=person_name,
            face_thumbnail_path=face_thumbnail_path,
            folder_context=folder_context,
            thumbnail_dir=settings.thumbnail_dir,
            strict=strict,
        )
        gemini_reranked = True

    transcript_reranked = False
    if use_rerank and transcript_hits:
        transcript_hits = await rerank_transcript_moments(
            search_text,
            transcript_hits,
            strict=strict,
        )
        transcript_reranked = True

    merged: list[SearchMoment] = []
    seen: set[tuple[str, float]] = set()

    # Regex transcript hits on top (fast, no rerank), then face, Gemini, legacy transcript
    for moment in regex_transcript_hits + face_hits + gemini_hits + transcript_hits:
        key = (moment.drive_file_id, round(moment.timestamp_sec, 2))
        if key in seen:
            continue
        seen.add(key)
        merged.append(moment)

    merged.sort(key=_moment_priority_key)
    merged = _filter_certain_moments(
        merged,
        query=search_text,
        action_query=is_action,
        gemini_reranked=gemini_reranked,
        transcript_reranked=transcript_reranked,
        use_rerank=use_rerank,
    )
    if source_filter:
        merged = await _filter_moments_by_source(session, merged, source_filter)
    return merged[:settings.gemini_video_result_limit]


async def _filter_moments_by_source(
    session: AsyncSession,
    moments: list[SearchMoment],
    source: str,
) -> list[SearchMoment]:
    """Keep moments whose DriveFile.source matches (e.g. youtube-added videos)."""
    if not moments:
        return moments
    ids = list({m.drive_file_id for m in moments})
    rows = (
        await session.execute(select(DriveFile.id, DriveFile.source).where(DriveFile.id.in_(ids)))
    ).all()
    by_id = {row.id: (row.source or "drive").lower() for row in rows}
    kept: list[SearchMoment] = []
    for moment in moments:
        src = by_id.get(moment.drive_file_id)
        if src is None and moment.drive_file_id.startswith("yt:"):
            src = "youtube"
        if src == source:
            kept.append(moment)
        elif source == "youtube" and (
            moment.drive_file_id.startswith("yt:") or moment.path.startswith("/youtube/")
        ):
            kept.append(moment)
    return kept


def _filter_certain_moments(
    moments: list[SearchMoment],
    *,
    query: str,
    action_query: bool,
    gemini_reranked: bool,
    transcript_reranked: bool,
    use_rerank: bool,
) -> list[SearchMoment]:
    """Keep only moments we are confident about."""
    settings = get_settings()
    keywords = action_match_keywords(query) if action_query else set()
    kept: list[SearchMoment] = []

    for moment in moments:
        score = moment.score or 0.0
        match_type = moment.match_type

        if match_type == "face_detected":
            if score >= 0.70:
                kept.append(moment)
            continue

        if match_type == "transcript_regex":
            if score >= 0.45:
                kept.append(moment)
            continue

        if match_type == "transcript":
            if score < 0.5:
                continue
            if action_query and moment.snippet:
                if not caption_matches_action(moment.snippet, keywords):
                    continue
                if caption_contradicts_action(moment.snippet, query):
                    continue
            if use_rerank and not transcript_reranked:
                continue
            kept.append(moment)
            continue

        if match_type == "gemini_visual":
            if use_rerank and not gemini_reranked:
                continue
            if use_rerank and gemini_reranked:
                if score < settings.gemini_video_display_min_score:
                    continue
                kept.append(moment)
                continue
            if score >= settings.gemini_video_display_min_score:
                kept.append(moment)

    return kept


def _moment_priority_key(moment: SearchMoment) -> tuple[int, float]:
    """Transcript hits first, then score within each tier."""
    priority = {
        "transcript_regex": 0,
        "transcript": 1,
        "svs_transcript": 2,
        "face_detected": 3,
        "gemini_visual": 4,
        "svs_visual": 5,
    }.get(moment.match_type, 6)
    if moment.match_type.startswith("svs_transcript"):
        priority = 2
    if moment.match_type.startswith("svs_visual"):
        priority = 5
    return (priority, -(moment.score or 0))


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
    min_hits = min(len(tokens), max(2, len(tokens) // 2 + 1))

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
        hit_count = sum(1 for tok in tokens if tok in hay)
        if hit_count < min_hits:
            continue

        person_names = await person_names_for_drive_file(session, drive_file.id)
        if person_name and not _person_matches(person_name, person_names, hay):
            continue

        score = hit_count / len(tokens)
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


async def _regex_transcript_moments(
    session: AsyncSession,
    query: str,
    person_name: str | None,
    folder_path: str | None = None,
) -> list[SearchMoment]:
    """Fast regex/phrase transcript search — addon path, no Gemini rerank."""
    stmt = (
        select(VideoSegment, Media, DriveFile)
        .join(Media, VideoSegment.media_id == Media.id)
        .join(DriveFile, Media.drive_file_id == DriveFile.id)
        .where(Media.type == MediaType.VIDEO, VideoSegment.text != "")
    )
    if folder_path and folder_path.strip() and folder_path.strip() != "/":
        fp = folder_path.strip().rstrip("/")
        stmt = stmt.where(
            (DriveFile.path.startswith(fp + "/")) | (DriveFile.path == fp)
        )
    rows = (await session.execute(stmt)).all()
    moments: list[SearchMoment] = []

    for segment, _media, drive_file in rows:
        scored = score_transcript_match(segment.text, query)
        if scored is None:
            continue
        score, _kind = scored

        person_names = await person_names_for_drive_file(session, drive_file.id)
        hay = segment.text.lower()
        if person_name and not _person_matches(person_name, person_names, hay):
            continue

        snippet = (segment.text or "")[:240] or None
        moments.append(
            _build_moment(
                drive_file,
                timestamp_sec=segment.start_sec,
                end_timestamp_sec=segment.end_sec,
                match_type="transcript_regex",
                snippet=snippet,
                person_names=person_names,
                score=score,
            )
        )

    moments.sort(key=lambda m: -(m.score or 0))
    logger.info("Regex transcript search: %d moment(s) for query %r", len(moments), query)
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

    def _merge_hits(hits: list[dict], fused: dict[tuple[str, float], dict]) -> None:
        for hit in hits:
            key = (hit["drive_file_id"], round(hit["timestamp"], 3))
            if key not in fused or hit["score"] > fused[key]["score"]:
                fused[key] = hit

    async def _search_variant(variant: str) -> list[dict]:
        vec = await asyncio.to_thread(embed_text_sync, variant)
        return await asyncio.to_thread(
            search_frames_sync,
            vec,
            limit=settings.gemini_video_result_limit,
            min_score=settings.gemini_video_min_score,
        )

    parallel = get_runtime_settings().search_parallel_variants_enabled

    try:
        fused: dict[tuple[str, float], dict] = {}
        if parallel:
            variant_hits = await asyncio.gather(*[_search_variant(v) for v in queries])
            for hits in variant_hits:
                _merge_hits(hits, fused)
        else:
            for variant in queries:
                _merge_hits(await _search_variant(variant), fused)
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

        snippet = await _nearest_segment_snippet(session, drive_file_id, timestamp)

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
            video_url=_video_playback_url(drive_file, timestamp),
            person_names=person_names,
            snippet=snippet,
            score=score,
        ))

    logger.info("Gemini video search: %d moments for query %r", len(moments), query)
    return moments


async def _nearest_segment_snippet(
    session: AsyncSession,
    drive_file_id: str,
    timestamp: float,
) -> str | None:
    """Nearest indexed transcript/VLM description for a frame timestamp."""
    from sqlalchemy import func

    row = (
        await session.execute(
            select(VideoSegment.text, VideoSegment.vlm_description)
            .join(Media, VideoSegment.media_id == Media.id)
            .where(Media.drive_file_id == drive_file_id)
            .order_by(func.abs(VideoSegment.start_sec - timestamp))
            .limit(1)
        )
    ).first()
    if not row:
        return None
    text, vlm = row
    snippet = (vlm or text or "").strip()
    return snippet[:240] if snippet else None


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
            video_url=_video_playback_url(drive_file, ts),
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


def _video_playback_url(drive_file: DriveFile, timestamp_sec: float) -> str:
    ts = f"{timestamp_sec:.2f}"
    return f"/drive/files/{drive_file.id}/preview#t={ts}"


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
        video_url=_video_playback_url(drive_file, timestamp_sec),
        person_names=person_names,
        snippet=snippet,
        score=score,
    )
