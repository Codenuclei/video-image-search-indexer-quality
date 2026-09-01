"""Folder-scoped exact + semantic search result cache.

Exact hits skip Gemini rerank. Semantic hits reuse a same-folder cached
payload when the query embedding is close enough and fingerprints still match.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DriveFile, DriveFileStatus, Face, FaceCluster, Media, Person, SearchQueryCache
from app.runtime_settings import get_runtime_settings
from app.schemas import SearchResponse

logger = logging.getLogger(__name__)

# Balanced paraphrases in the same folder (e.g. "wine glass" ≈ "glass of wine").
SEMANTIC_MIN_COSINE = 0.88
SEARCH_CACHE_VERSION = "v18-sandbag-object-gate"
# Exact repeats should stay fast while the indexer is continuously changing the
# global library fingerprint. After this grace window, full fingerprint
# validation restores freshness.
EXACT_FRESH_TTL = timedelta(minutes=10)
_CACHE_VERSION_FIELD = "_search_cache_version"


def normalize_folder_path(folder_path: str | None) -> str:
    raw = (folder_path or "").strip()
    if not raw or raw == "/":
        return ""
    return raw.rstrip("/")


def make_cache_key(
    *,
    query: str,
    person: str | None,
    mime: str,
    folder_path: str | None,
    captions: bool,
    rerank: bool,
) -> str:
    payload = {
        "version": SEARCH_CACHE_VERSION,
        "q": query.strip().lower(),
        "person": (person or "").strip().lower(),
        "mime": (mime or "all").strip().lower(),
        "folder": normalize_folder_path(folder_path),
        "captions": bool(captions),
        "rerank": bool(rerank),
        "semantic_min_score": get_runtime_settings().search_semantic_min_score,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _folder_path_clause(folder_path: str):
    if not folder_path:
        return True
    prefix = folder_path + "/"
    return or_(DriveFile.path == folder_path, DriveFile.path.startswith(prefix))


async def folder_fingerprint(session: AsyncSession, folder_path: str | None) -> str:
    fp = normalize_folder_path(folder_path)
    stmt = select(func.count(DriveFile.id), func.max(DriveFile.last_synced_at)).where(
        DriveFile.status.in_((DriveFileStatus.PROCESSED, DriveFileStatus.ARCHIVED)),
    )
    if fp:
        stmt = stmt.where(_folder_path_clause(fp))
    count, latest = (await session.execute(stmt)).one()
    stamp = ""
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        stamp = latest.astimezone(timezone.utc).isoformat()
    return f"{int(count or 0)}:{stamp}"


async def cluster_ids_for_person(session: AsyncSession, person_name: str | None) -> list[int]:
    name = (person_name or "").strip()
    if not name:
        return []
    person = (
        await session.execute(select(Person).where(func.lower(Person.name) == name.lower()))
    ).scalar_one_or_none()
    if person is None:
        return []
    rows = (
        await session.execute(select(FaceCluster.id).where(FaceCluster.person_id == person.id))
    ).scalars().all()
    extra = (
        await session.execute(
            select(Face.cluster_id).where(Face.person_id == person.id, Face.cluster_id.is_not(None))
        )
    ).scalars().all()
    ids = {int(x) for x in rows if x is not None}
    ids.update(int(x) for x in extra if x is not None)
    return sorted(ids)


async def cluster_fingerprint(session: AsyncSession, cluster_ids: list[int]) -> str:
    if not cluster_ids:
        return ""
    rows = (
        await session.execute(
            select(Face.cluster_id, func.count(Face.id))
            .where(Face.cluster_id.in_(cluster_ids))
            .group_by(Face.cluster_id)
        )
    ).all()
    counts = {int(cid): int(n) for cid, n in rows}
    parts = [f"{cid}:{counts.get(cid, 0)}" for cid in sorted(cluster_ids)]
    return ",".join(parts)


async def cluster_ids_for_drive_files(session: AsyncSession, drive_file_ids: list[str]) -> list[int]:
    ids = [fid for fid in drive_file_ids if fid]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Face.cluster_id)
            .join(Media, Media.id == Face.media_id)
            .where(Media.drive_file_id.in_(ids), Face.cluster_id.is_not(None))
        )
    ).scalars().all()
    return sorted({int(x) for x in rows if x is not None})


def fingerprints_valid(row: SearchQueryCache, folder_fp: str, cluster_fp: str) -> bool:
    """Folder-scoped keys miss when that folder's contents change.

    Global visual keys miss when the library fingerprint moves.
    Global person/cluster keys miss only when cluster_fp changes.
    """
    folder_scoped = bool((row.folder_path or "").strip())
    person_scoped = bool((row.person or "").strip())
    if folder_scoped or not person_scoped:
        if row.folder_fp != folder_fp:
            return False
    if row.cluster_fp != cluster_fp:
        return False
    return True


def response_from_row(row: SearchQueryCache, *, cache: str) -> SearchResponse:
    payload = dict(row.response_json or {})
    payload.pop(_CACHE_VERSION_FIELD, None)
    payload["cache"] = cache
    return SearchResponse.model_validate(payload)


def row_matches_search_cache_version(row: SearchQueryCache) -> bool:
    """Semantic reuse must not cross logic versions (exact keys already include version)."""
    payload = row.response_json or {}
    return payload.get(_CACHE_VERSION_FIELD) == SEARCH_CACHE_VERSION


def exact_row_is_fresh(row: SearchQueryCache, now: datetime | None = None) -> bool:
    created = row.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - created.astimezone(timezone.utc) <= EXACT_FRESH_TTL


async def lookup_exact(
    session: AsyncSession,
    *,
    query: str,
    person: str | None,
    mime: str,
    folder_path: str | None,
    captions: bool,
    rerank: bool,
) -> SearchResponse | None:
    key = make_cache_key(
        query=query,
        person=person,
        mime=mime,
        folder_path=folder_path,
        captions=captions,
        rerank=rerank,
    )
    row = await session.get(SearchQueryCache, key)
    if row is None:
        return None
    if exact_row_is_fresh(row):
        logger.info("search_cache exact fresh hit folder=%s", row.folder_path or "*")
        return response_from_row(row, cache="exact")
    folder_fp = await folder_fingerprint(session, row.folder_path)
    cluster_fp = await cluster_fingerprint(session, list(row.cluster_ids or []))
    if not fingerprints_valid(row, folder_fp, cluster_fp):
        return None
    return response_from_row(row, cache="exact")


async def lookup_semantic(
    session: AsyncSession,
    *,
    query_embedding: list[float],
    person: str | None,
    mime: str,
    folder_path: str | None,
    captions: bool,
    rerank: bool,
) -> SearchResponse | None:
    if not query_embedding:
        return None
    fp = normalize_folder_path(folder_path)
    person_key = (person or "").strip().lower()
    mime_key = (mime or "all").strip().lower()
    rows = list(
        (
            await session.execute(
                select(SearchQueryCache)
                .where(
                    SearchQueryCache.folder_path == fp,
                    func.lower(SearchQueryCache.person) == person_key,
                    SearchQueryCache.mime == mime_key,
                    SearchQueryCache.captions.is_(bool(captions)),
                    SearchQueryCache.rerank.is_(bool(rerank)),
                )
                .order_by(SearchQueryCache.created_at.desc())
                .limit(40)
            )
        ).scalars().all()
    )
    folder_fp = await folder_fingerprint(session, fp)
    best: tuple[float, SearchQueryCache] | None = None
    for row in rows:
        if not row_matches_search_cache_version(row):
            continue
        emb = row.query_embedding or []
        sim = cosine_similarity(query_embedding, [float(x) for x in emb])
        if sim < SEMANTIC_MIN_COSINE:
            continue
        cluster_fp = await cluster_fingerprint(session, list(row.cluster_ids or []))
        if not fingerprints_valid(row, folder_fp, cluster_fp):
            continue
        if best is None or sim > best[0]:
            best = (sim, row)
    if best is None:
        return None
    logger.info("search_cache semantic hit sim=%.3f folder=%s", best[0], fp or "*")
    return response_from_row(best[1], cache="semantic")


async def store_search_cache(
    session: AsyncSession,
    *,
    query: str,
    person: str | None,
    mime: str,
    folder_path: str | None,
    captions: bool,
    rerank: bool,
    response: SearchResponse,
    query_embedding: list[float] | None,
) -> None:
    fp = normalize_folder_path(folder_path)
    person_key = (person or "").strip()
    mime_key = (mime or "all").strip().lower()
    file_ids = [f.drive_file_id for f in (response.files or [])]
    file_ids.extend(m.drive_file_id for m in (response.moments or []))
    cluster_ids = sorted(
        set(await cluster_ids_for_person(session, person_key or None))
        | set(await cluster_ids_for_drive_files(session, file_ids))
    )
    folder_fp = await folder_fingerprint(session, fp)
    cluster_fp = await cluster_fingerprint(session, cluster_ids)
    key = make_cache_key(
        query=query,
        person=person_key,
        mime=mime_key,
        folder_path=fp,
        captions=captions,
        rerank=rerank,
    )
    payload = response.model_dump(mode="json")
    payload.pop("cache", None)
    payload[_CACHE_VERSION_FIELD] = SEARCH_CACHE_VERSION
    row = await session.get(SearchQueryCache, key)
    if row is None:
        row = SearchQueryCache(cache_key=key)
        session.add(row)
    row.query_text = query.strip()
    row.query_embedding = query_embedding
    row.folder_path = fp
    row.person = person_key
    row.mime = mime_key
    row.captions = bool(captions)
    row.rerank = bool(rerank)
    row.response_json = payload
    row.folder_fp = folder_fp
    row.cluster_fp = cluster_fp
    row.cluster_ids = cluster_ids
    row.created_at = datetime.now(timezone.utc)
    await session.commit()
