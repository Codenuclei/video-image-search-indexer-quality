"""Vector search for indexed Drive images (Gemini Embedding 2 + Qdrant)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.concurrency.pools import effective_cpu_workers
from app.config import get_settings
from app.db.models import DriveFile
from app.gemini.tags import person_names_for_drive_files
from app.gemini.video_embeddings import embed_text_sync
from app.qdrant.image_captions import search_caption_keywords_sync, search_captions_sync
from app.qdrant.images import search_images_sync
from app.drive.display_name import drive_file_display_name
from app.runtime_settings import get_runtime_settings
from app.schemas import SearchResultFile

logger = logging.getLogger(__name__)


async def search_image_files(
    session: AsyncSession,
    query: str,
    *,
    person_name: str | None = None,
    person_names: list[str] | None = None,
    folder_path: str | None = None,
    use_captions: bool = False,
    action_query: bool = False,
    enable_conjunctive_object_gate: bool = True,
) -> list[SearchResultFile]:
    """Text→image retrieval; optional caption fusion when use_captions=True."""
    settings = get_settings()
    semantic_min_score = get_runtime_settings().search_semantic_min_score
    if not settings.gemini_api_key:
        return []

    search_text = query.strip()
    if not search_text:
        return []

    from app.search.carousel_trace import drive_search_log

    drive_search_log(
        "image_search_start",
        query=search_text[:120],
        use_captions=use_captions,
        person=person_name or "-",
        folder=folder_path or "*",
    )
    started = __import__("time").perf_counter()

    if settings.search_query_expansion:
        from app.gemini.query_expand import expand_queries_sync
        queries = list(await asyncio.to_thread(expand_queries_sync, search_text))
    else:
        queries = [search_text]

    visual_scores: dict[str, float] = {}
    caption_scores: dict[str, float] = {}
    captions: dict[str, str] = {}

    def _merge_visual(visual_hits: list[dict]) -> None:
        for h in visual_hits:
            fid = h["drive_file_id"]
            if fid not in visual_scores or h["score"] > visual_scores[fid]:
                visual_scores[fid] = h["score"]

    def _merge_captions(caption_hits: list[dict]) -> None:
        for h in caption_hits:
            fid = h["drive_file_id"]
            if fid not in caption_scores or h["score"] > caption_scores[fid]:
                caption_scores[fid] = h["score"]
                if h.get("caption"):
                    captions[fid] = h["caption"]

    async def _search_variant(variant: str) -> tuple[list[dict], list[dict]]:
        vec = await asyncio.to_thread(embed_text_sync, variant)
        if use_captions and settings.image_caption_enabled:
            visual_hits, caption_hits = await asyncio.gather(
                asyncio.to_thread(
                    search_images_sync,
                    vec,
                    limit=None,
                    min_score=settings.gemini_image_min_score,
                    page_size=settings.gemini_image_result_limit,
                ),
                asyncio.to_thread(
                    search_captions_sync,
                    vec,
                    limit=None,
                    min_score=semantic_min_score,
                    page_size=settings.gemini_image_result_limit,
                ),
            )
        else:
            visual_hits = await asyncio.to_thread(
                search_images_sync,
                vec,
                limit=None,
                min_score=settings.gemini_image_min_score,
                page_size=settings.gemini_image_result_limit,
            )
            caption_hits = []
        return visual_hits, caption_hits

    required_persons: set[str] = set()
    for name in person_names or []:
        if name.strip():
            required_persons.add(name.strip().lower())
    if person_name and person_name.strip():
        required_persons.add(person_name.strip().lower())

    parallel_variants = (
        settings.search_variant_max_parallel
        if settings.search_variant_max_parallel > 0
        else effective_cpu_workers(settings.cpu_thread_pool_size)
    )

    try:
        if len(queries) > 1 and parallel_variants > 1:
            variant_sem = asyncio.Semaphore(parallel_variants)

            async def _run_variant(variant: str) -> tuple[list[dict], list[dict]]:
                async with variant_sem:
                    return await _search_variant(variant)

            variant_results = await asyncio.gather(*[_run_variant(v) for v in queries])
            for visual_hits, caption_hits in variant_results:
                _merge_visual(visual_hits)
                _merge_captions(caption_hits)
        else:
            for variant in queries:
                visual_hits, caption_hits = await _search_variant(variant)
                _merge_visual(visual_hits)
                _merge_captions(caption_hits)
        if use_captions and settings.image_caption_enabled:
            lexical_hits = await asyncio.to_thread(
                search_caption_keywords_sync,
                queries[0],
                page_size=settings.gemini_image_result_limit,
            )
            for hit in lexical_hits:
                fid = hit["drive_file_id"]
                caption_scores[fid] = max(
                    caption_scores.get(fid, 0.0),
                    semantic_min_score,
                )
                captions[fid] = hit["caption"]
    except Exception as exc:
        logger.warning("Image vector search failed (query=%r): %s", query, exc)
        return []

    if action_query and use_captions and settings.image_caption_enabled:
        from app.qdrant.image_captions import get_captions_by_ids_sync

        visual_top = sorted(visual_scores.items(), key=lambda x: -x[1])
        stored = await asyncio.to_thread(
            get_captions_by_ids_sync,
            [fid for fid, _ in visual_top],
        )
        for fid, v in visual_top:
            cap = (stored.get(fid) or "").strip()
            if not cap:
                continue
            if fid not in caption_scores or v > visual_scores.get(fid, 0):
                caption_scores[fid] = max(caption_scores.get(fid, 0.0), v * 0.75)
                captions[fid] = cap

    from app.objects.query_concepts import (
        all_query_concepts_supported,
        parse_query_concepts,
    )
    from app.objects.search import fuse_object_score, object_matches_for_query

    object_matches = await object_matches_for_query(session, query)
    query_concepts = parse_query_concepts(query)
    conjunctive_object_gate = (
        enable_conjunctive_object_gate
        and not action_query
        and not person_name
        and not person_names
        and query_concepts.is_conjunctive_object_query
    )
    all_ids = set(visual_scores) | set(caption_scores) | set(object_matches)
    if conjunctive_object_gate and all_ids:
        from app.qdrant.image_captions import get_captions_by_ids_sync

        stored = await asyncio.to_thread(
            get_captions_by_ids_sync,
            list(all_ids),
        )
        for fid, caption in stored.items():
            if caption and not captions.get(fid):
                captions[fid] = caption

    if not all_ids:
        return []

    vw = settings.image_visual_weight
    cw = settings.image_caption_weight
    ranked: list[tuple[str, float, str | None]] = []

    for fid in all_ids:
        v = visual_scores.get(fid, 0.0)
        c = caption_scores.get(fid, 0.0)
        has_object_hit = fid in object_matches
        object_evidence = object_matches.get(fid, ())
        fully_satisfies_object_scope = (
            not conjunctive_object_gate
            or all_query_concepts_supported(
                query_concepts,
                structured_labels=(item.label for item in object_evidence),
                evidence_texts=(item.evidence_text for item in object_evidence),
                caption=captions.get(fid),
            )
        )
        if conjunctive_object_gate and not fully_satisfies_object_scope:
            continue
        qualified_object_hit = has_object_hit and fully_satisfies_object_scope
        if max(v, c) < semantic_min_score and not qualified_object_hit:
            continue
        has_caption_hit = fid in caption_scores
        if use_captions and settings.image_caption_enabled:
            if action_query:
                if not has_caption_hit:
                    continue
                fused = cw * c + vw * v
            elif has_caption_hit:
                fused = vw * v + cw * c
            else:
                fused = v
        elif action_query and not has_object_hit:
            continue
        else:
            fused = v
        object_confidence = max(
            (item.confidence for item in object_evidence),
            default=0.0,
        )
        if not conjunctive_object_gate or fully_satisfies_object_scope:
            fused = fuse_object_score(
                fused,
                len(object_evidence),
                object_confidence,
            )
        ranked.append((fid, fused, captions.get(fid)))

    ranked.sort(key=lambda x: -x[1])

    results: list[SearchResultFile] = []
    seen: set[str] = set()
    ranked_ids = [drive_file_id for drive_file_id, _, _ in ranked]
    drive_files = list(
        (
            await session.execute(
                select(DriveFile).where(DriveFile.id.in_(ranked_ids))
            )
        ).scalars().all()
    )
    drive_files_by_id = {drive_file.id: drive_file for drive_file in drive_files}
    names_by_file = await person_names_for_drive_files(session, ranked_ids)

    for drive_file_id, score, caption in ranked:
        if drive_file_id in seen:
            continue

        drive_file = drive_files_by_id.get(drive_file_id)
        if drive_file is None or not drive_file.mime_type.startswith("image/"):
            continue

        if folder_path and folder_path.strip() and folder_path.strip() != "/":
            fp = folder_path.strip().rstrip("/")
            if not (drive_file.path.startswith(fp + "/") or drive_file.path == fp):
                continue

        person_names = names_by_file.get(drive_file_id, [])
        tagged = {n.lower() for n in person_names}
        if required_persons:
            if len(required_persons) >= 2:
                if not required_persons.issubset(tagged):
                    continue
            elif not (required_persons & tagged):
                continue

        seen.add(drive_file_id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file_id,
                name=drive_file_display_name(drive_file),
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=person_names,
                score=round(score, 4),
                caption=caption,
                matched_objects=object_matches.get(drive_file_id, []),
            )
        )

    if results and use_captions:
        from app.qdrant.image_captions import get_captions_by_ids_sync

        stored = await asyncio.to_thread(
            get_captions_by_ids_sync,
            [item.drive_file_id for item in results],
        )
        enriched: list[SearchResultFile] = []
        for item in results:
            cap = (item.caption or stored.get(item.drive_file_id) or "").strip()
            if action_query and not cap:
                continue
            enriched.append(item.model_copy(update={"caption": cap or None}))
        results = enriched

    logger.info("Image vector search: %d files for query %r", len(results), query)
    drive_search_log(
        "image_search_end",
        result_count=len(results),
        elapsed_ms=(__import__("time").perf_counter() - started) * 1000.0,
    )
    return results


async def attach_stored_captions(files: list[SearchResultFile]) -> list[SearchResultFile]:
    """Load caption text from Qdrant for display (independent of caption-vector fusion)."""
    image_ids = [f.drive_file_id for f in files if f.mime_type.startswith("image/")]
    if not image_ids:
        return files

    from app.qdrant.image_captions import get_captions_by_ids_sync

    stored = await asyncio.to_thread(get_captions_by_ids_sync, image_ids)
    enriched: list[SearchResultFile] = []
    for item in files:
        if not item.mime_type.startswith("image/"):
            enriched.append(item)
            continue
        cap = (item.caption or stored.get(item.drive_file_id) or "").strip()
        enriched.append(item.model_copy(update={"caption": cap or None}))
    return enriched


async def _refresh_object_jobs_for_captions(drive_file_ids: list[str]) -> None:
    """Reclassify completed object jobs when richer caption evidence arrives."""
    if not drive_file_ids:
        return
    from app.db.session import get_session_factory
    from app.workers.object_queue import enqueue_object_job

    async with get_session_factory()() as session:
        for drive_file_id in drive_file_ids:
            await enqueue_object_job(session, drive_file_id, force=True)
        await session.commit()


async def index_image_caption(jpeg_bytes: bytes, drive_file_id: str) -> None:
    """Describe image (batched at backfill; single here) and embed caption text."""
    from app.gemini.captions import describe_image_sync
    from app.qdrant.image_captions import is_valid_caption, upsert_caption_sync

    settings = get_settings()
    if not settings.gemini_api_key or not settings.image_caption_enabled:
        return

    caption = await asyncio.to_thread(describe_image_sync, jpeg_bytes)
    if not is_valid_caption(caption):
        return

    vec = await asyncio.to_thread(embed_text_sync, caption)
    await asyncio.to_thread(
        upsert_caption_sync,
        drive_file_id=drive_file_id,
        vector=vec,
        caption=caption,
    )
    await _refresh_object_jobs_for_captions([drive_file_id])
    try:
        from app.workers.index_tat import stamp_completed_at_ids

        await stamp_completed_at_ids([drive_file_id], reason="captioned", force=True)
    except Exception:  # noqa: BLE001
        pass


async def index_image_captions_batch(items: list[tuple[str, bytes]]) -> int:
    """Describe+embed a batch of images. Returns count indexed."""
    from app.gemini.captions import describe_images_batch_sync
    from app.qdrant.image_captions import is_valid_caption, upsert_caption_sync

    settings = get_settings()
    if not settings.gemini_api_key or not settings.image_caption_enabled or not items:
        return 0

    ids = [fid for fid, _ in items]
    blobs = [b for _, b in items]
    captions = await asyncio.to_thread(describe_images_batch_sync, blobs)

    done = 0
    captioned_ids: list[str] = []
    for fid, caption in zip(ids, captions):
        if not is_valid_caption(caption):
            continue
        vec = await asyncio.to_thread(embed_text_sync, caption)
        await asyncio.to_thread(
            upsert_caption_sync,
            drive_file_id=fid,
            vector=vec,
            caption=caption,
        )
        captioned_ids.append(fid)
        done += 1
    if captioned_ids:
        await _refresh_object_jobs_for_captions(captioned_ids)
        try:
            from app.workers.index_tat import stamp_completed_at_ids

            await stamp_completed_at_ids(captioned_ids, reason="captioned", force=True)
        except Exception:  # noqa: BLE001
            pass
    return done


async def index_image_embedding(jpeg_bytes: bytes, drive_file_id: str) -> None:
    """Embed a Drive image and upsert to Qdrant (single-image path)."""
    await index_image_embeddings_batch([(drive_file_id, jpeg_bytes)])


async def index_image_embeddings_batch(items: list[tuple[str, bytes]]) -> int:
    """Embed images via batchEmbedContents and upsert to Qdrant.

    ``items`` is ``(drive_file_id, jpeg_bytes)``. Splits into
    ``image_embed_batch_size`` chunks. Returns count upserted.
    """
    from app.gemini.video_embeddings import embed_frames_batch_bytes_sync
    from app.pipelines.async_cpu import run_gemini_embed_io
    from app.qdrant.images import upsert_image_sync

    settings = get_settings()
    if not settings.gemini_api_key or not items:
        return 0

    batch_size = max(1, settings.image_embed_batch_size)
    done = 0
    embedded_ids: list[str] = []

    def _embed_chunk(payloads: list[bytes]) -> list[list[float]]:
        # Prepare path already downscales — keep PIL off the Gemini I/O pool.
        return embed_frames_batch_bytes_sync(payloads, downscale=False)

    for i in range(0, len(items), batch_size):
        chunk = items[i : i + batch_size]
        ids = [fid for fid, _ in chunk]
        payloads = [jpeg for _, jpeg in chunk]
        try:
            vectors = await run_gemini_embed_io(_embed_chunk, payloads)

            async def _upsert_one(fid: str, vec: list[float]) -> bool:
                if not vec:
                    return False
                await asyncio.to_thread(upsert_image_sync, drive_file_id=fid, vector=vec)
                return True

            results = await asyncio.gather(
                *[_upsert_one(fid, vec) for fid, vec in zip(ids, vectors)]
            )
            done += sum(1 for ok in results if ok)
            embedded_ids.extend(fid for fid, ok in zip(ids, results) if ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Image batch embed failed for %d file(s): %s",
                len(chunk),
                exc,
            )
    if embedded_ids:
        from app.db.session import get_session_factory
        from app.workers.object_queue import enqueue_object_job

        async with get_session_factory()() as session:
            for fid in embedded_ids:
                await enqueue_object_job(session, fid)
            await session.commit()
    return done
