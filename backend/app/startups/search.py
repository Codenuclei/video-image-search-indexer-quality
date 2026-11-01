"""Gather Drive images for a resolved startup (founders OR logos)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    DriveFile,
    DriveFileStatus,
    Face,
    FaceCluster,
    Media,
    MediaOcrSpan,
    StartupFaceCluster,
    StartupFounder,
    StartupLogoRef,
)
from app.schemas import SearchResultFile
from app.search.local import files_for_drive_ids
from app.startups.resolve import ResolvedStartup

logger = logging.getLogger(__name__)


@dataclass
class StartupHit:
    drive_file_id: str
    match_reason: str  # founder:<name|cluster:N> | logo
    score: float | None = None


async def _founder_drive_ids(
    session: AsyncSession,
    startup: ResolvedStartup,
) -> list[StartupHit]:
    cluster_ids = list(
        (
            await session.execute(
                select(StartupFaceCluster.cluster_id).where(
                    StartupFaceCluster.startup_id == startup.id
                )
            )
        ).scalars().all()
    )
    person_ids = list(
        (
            await session.execute(
                select(StartupFounder.person_id).where(
                    StartupFounder.startup_id == startup.id,
                    StartupFounder.person_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    person_ids = [int(p) for p in person_ids if p is not None]
    cluster_ids = [int(c) for c in cluster_ids]

    if not cluster_ids and not person_ids:
        return []

    # Also include clusters that were later named to a linked person.
    if person_ids:
        named_clusters = list(
            (
                await session.execute(
                    select(FaceCluster.id).where(FaceCluster.person_id.in_(person_ids))
                )
            ).scalars().all()
        )
        cluster_ids = list({*cluster_ids, *[int(c) for c in named_clusters]})

    clauses = []
    if cluster_ids:
        clauses.append(
            exists(
                select(1)
                .select_from(Face)
                .join(Media, Media.id == Face.media_id)
                .where(Media.drive_file_id == DriveFile.id)
                .where(Face.cluster_id.in_(cluster_ids))
            )
        )
        # Faces moved onto a person keep cluster_id null — cover via person_id too.
    if person_ids:
        clauses.append(
            exists(
                select(1)
                .select_from(Face)
                .join(Media, Media.id == Face.media_id)
                .where(Media.drive_file_id == DriveFile.id)
                .where(Face.person_id.in_(person_ids))
            )
        )
    if not clauses:
        return []

    stmt = (
        select(DriveFile.id)
        .where(DriveFile.status.in_([DriveFileStatus.PROCESSED, DriveFileStatus.ERROR]))
        .where(or_(*clauses))
    )
    drive_ids = list((await session.execute(stmt)).scalars().all())

    # Build reason labels from linked founders / clusters.
    founder_names = list(
        (
            await session.execute(
                select(StartupFounder.founder_name).where(
                    StartupFounder.startup_id == startup.id,
                    StartupFounder.is_candidate.is_(False),
                )
            )
        ).scalars().all()
    )
    reason = (
        f"founder:{founder_names[0]}"
        if len(founder_names) == 1
        else "founder:linked"
    )
    return [StartupHit(drive_file_id=fid, match_reason=reason, score=0.9) for fid in drive_ids]


async def _logo_ann_hits(session: AsyncSession, startup: ResolvedStartup) -> list[StartupHit]:
    refs = list(
        (
            await session.execute(
                select(StartupLogoRef).where(StartupLogoRef.startup_id == startup.id)
            )
        ).scalars().all()
    )
    if not refs:
        return []
    settings = get_settings()
    min_score = float(settings.startup_logo_min_score)
    from app.qdrant.images import search_images_sync

    hits: dict[str, float] = {}
    for ref in refs:
        try:
            rows = await asyncio.to_thread(
                search_images_sync,
                list(ref.embedding),
                limit=40,
                min_score=min_score,
            )
        except Exception:  # noqa: BLE001
            logger.warning("startup logo ANN failed slug=%s", startup.slug, exc_info=True)
            continue
        for row in rows:
            fid = row.get("drive_file_id")
            score = float(row.get("score") or 0.0)
            if not fid:
                continue
            hits[fid] = max(hits.get(fid, 0.0), score)
    return [
        StartupHit(drive_file_id=fid, match_reason="logo", score=score)
        for fid, score in hits.items()
    ]


async def _logo_ocr_hits(session: AsyncSession, startup: ResolvedStartup) -> list[StartupHit]:
    needles = {
        (startup.name or "").strip().lower(),
        *[(a or "").strip().lower() for a in startup.aliases],
    }
    needles = {n for n in needles if len(n) >= 3}
    if not needles:
        return []
    # Prefer indexed normalized_text ILIKE over a full table scan in Python.
    clauses = [
        MediaOcrSpan.normalized_text.ilike(f"%{n}%")
        for n in sorted(needles, key=len, reverse=True)[:8]
    ]
    clauses += [
        MediaOcrSpan.text.ilike(f"%{n}%")
        for n in sorted(needles, key=len, reverse=True)[:8]
    ]
    rows = (
        await session.execute(
            select(Media.drive_file_id)
            .join(Media, Media.id == MediaOcrSpan.media_id)
            .where(or_(*clauses))
            .distinct()
            .limit(200)
        )
    ).scalars().all()
    return [
        StartupHit(drive_file_id=fid, match_reason="logo", score=0.75)
        for fid in rows
    ]


async def _logo_caption_hits(startup: ResolvedStartup) -> list[StartupHit]:
    """Caption ANN for 'X logo' style queries."""
    try:
        from app.gemini.video_embeddings import embed_text_sync
        from app.qdrant.image_captions import search_captions_sync
    except Exception:  # noqa: BLE001
        return []

    queries = [
        f"{startup.name} logo",
        f"{startup.name} brand",
        startup.name,
    ]
    hits: dict[str, float] = {}
    for q in queries:
        try:
            vec = await asyncio.to_thread(embed_text_sync, q)
            rows = await asyncio.to_thread(search_captions_sync, vec, limit=30, min_score=0.55)
        except Exception:  # noqa: BLE001
            logger.debug("startup caption search failed q=%s", q, exc_info=True)
            continue
        for row in rows:
            fid = row.get("drive_file_id")
            score = float(row.get("score") or 0.0)
            if fid:
                hits[fid] = max(hits.get(fid, 0.0), score)
    return [
        StartupHit(drive_file_id=fid, match_reason="logo", score=score)
        for fid, score in hits.items()
    ]


async def gather_startup_hits(
    session: AsyncSession,
    startups: list[ResolvedStartup],
    *,
    include_founders: bool = True,
    include_logos: bool = True,
) -> list[StartupHit]:
    """OR-merge founder and logo hits across all matched startups."""
    merged: dict[str, StartupHit] = {}

    def _keep(hit: StartupHit) -> None:
        prev = merged.get(hit.drive_file_id)
        if prev is None:
            merged[hit.drive_file_id] = hit
            return
        # Prefer founder reason when both match; keep higher score.
        prefer_founder = hit.match_reason.startswith("founder") and not prev.match_reason.startswith(
            "founder"
        )
        better_score = (hit.score or 0) > (prev.score or 0)
        if prefer_founder or (better_score and hit.match_reason[:7] == prev.match_reason[:7]):
            if prefer_founder:
                merged[hit.drive_file_id] = StartupHit(
                    drive_file_id=hit.drive_file_id,
                    match_reason=hit.match_reason,
                    score=max(hit.score or 0, prev.score or 0) or None,
                )
            elif better_score:
                merged[hit.drive_file_id] = hit

    for startup in startups:
        if include_founders:
            for hit in await _founder_drive_ids(session, startup):
                _keep(hit)
        if include_logos:
            for collector in (_logo_ann_hits, _logo_ocr_hits):
                for hit in await collector(session, startup):
                    _keep(hit)
            for hit in await _logo_caption_hits(startup):
                _keep(hit)
    return list(merged.values())


async def startup_hits_to_files(
    session: AsyncSession,
    hits: list[StartupHit],
) -> list[SearchResultFile]:
    if not hits:
        return []
    by_id = {h.drive_file_id: h for h in hits}
    files = await files_for_drive_ids(session, list(by_id.keys()))
    out: list[SearchResultFile] = []
    for f in files:
        hit = by_id.get(f.drive_file_id)
        update: dict = {}
        if hit and hit.score is not None:
            update["score"] = hit.score
        # Stash match reason in caption-adjacent field via matched_objects-like channel:
        # use a synthetic ObjectEvidence-free field — put reason into a light tag on person_names? 
        # Better: extend SearchResultFile with match_reasons (done in schemas).
        if hit:
            update["match_reasons"] = [hit.match_reason]
        out.append(f.model_copy(update=update) if update else f)
    return out


async def link_person_to_startup_founders(
    session: AsyncSession,
    *,
    person_id: int,
    person_name: str,
    cluster_id: int | None = None,
) -> int:
    """After naming a cluster, attach person_id to matching StartupFounder rows.

    Prefers founders on startups linked to this cluster; otherwise matches by name.
    Never creates Person rows.
    """
    updated = 0
    startup_ids: list[int] = []
    if cluster_id is not None:
        startup_ids = list(
            (
                await session.execute(
                    select(StartupFaceCluster.startup_id).where(
                        StartupFaceCluster.cluster_id == cluster_id
                    )
                )
            ).scalars().all()
        )

    founders = (
        await session.execute(select(StartupFounder))
    ).scalars().all()
    name_key = person_name.strip().casefold()
    for founder in founders:
        if founder.person_id is not None:
            continue
        if founder.founder_name.strip().casefold() != name_key:
            continue
        if startup_ids and founder.startup_id not in startup_ids:
            # Name matches a founder of a different startup — still link by name.
            pass
        founder.person_id = person_id
        updated += 1
    if updated:
        await session.flush()
    return updated
