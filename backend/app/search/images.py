"""Vector search for indexed Drive images (Gemini Embedding 2 + Qdrant)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from sqlalchemy.ext.asyncio import AsyncSession

from app.concurrency.pools import effective_cpu_workers
from app.config import get_settings
from app.db.models import DriveFile
from app.gemini.tags import person_names_for_drive_file
from app.gemini.video_embeddings import embed_frame_sync, embed_text_sync
from app.qdrant.image_captions import search_captions_sync
from app.qdrant.images import search_images_sync
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
) -> list[SearchResultFile]:
    """Text→image retrieval; optional caption fusion when use_captions=True."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return []

    search_text = query.strip()
    if not search_text:
        return []

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
                    limit=settings.gemini_image_result_limit,
                    min_score=settings.gemini_image_min_score,
                ),
                asyncio.to_thread(
                    search_captions_sync,
                    vec,
                    limit=settings.gemini_image_result_limit * (3 if action_query else 1),
                    min_score=0.30 if action_query else 0.0,
                ),
            )
        else:
            visual_hits = await asyncio.to_thread(
                search_images_sync,
                vec,
                limit=settings.gemini_image_result_limit,
                min_score=settings.gemini_image_min_score,
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
    except Exception as exc:
        logger.warning("Image vector search failed (query=%r): %s", query, exc)
        return []

    if action_query and use_captions and settings.image_caption_enabled:
        from app.qdrant.image_captions import get_captions_by_ids_sync

        visual_top = sorted(visual_scores.items(), key=lambda x: -x[1])[:60]
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

    all_ids = set(visual_scores) | set(caption_scores)
    if not all_ids:
        return []

    vw = settings.image_visual_weight
    cw = settings.image_caption_weight
    caption_min = (
        max(settings.image_caption_min_score, 0.62)
        if action_query
        else settings.image_caption_min_score
    )
    ranked: list[tuple[str, float, str | None]] = []

    for fid in all_ids:
        v = visual_scores.get(fid, 0.0)
        c = caption_scores.get(fid, 0.0)
        has_caption_hit = fid in caption_scores
        if use_captions and settings.image_caption_enabled:
            if action_query:
                if not has_caption_hit:
                    continue
                fused = cw * c + vw * v
            elif has_caption_hit:
                if c < caption_min and v < settings.image_visual_strong_score:
                    continue
                fused = vw * v + cw * c
            else:
                fused = v
        elif action_query:
            continue
        else:
            fused = v
        ranked.append((fid, fused, captions.get(fid)))

    ranked.sort(key=lambda x: -x[1])
    ranked = ranked[: settings.gemini_image_result_limit]

    results: list[SearchResultFile] = []
    seen: set[str] = set()

    for drive_file_id, score, caption in ranked:
        if drive_file_id in seen:
            continue

        drive_file: DriveFile | None = await session.get(DriveFile, drive_file_id)
        if drive_file is None or not drive_file.mime_type.startswith("image/"):
            continue

        if folder_path and folder_path.strip() and folder_path.strip() != "/":
            fp = folder_path.strip().rstrip("/")
            if not (drive_file.path.startswith(fp + "/") or drive_file.path == fp):
                continue

        person_names = await person_names_for_drive_file(session, drive_file_id)
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
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=person_names,
                score=round(score, 4),
                caption=caption,
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
        done += 1
    return done


async def index_image_embedding(jpeg_bytes: bytes, drive_file_id: str) -> None:
    """Embed a Drive image and upsert to Qdrant."""
    from app.qdrant.images import upsert_image_sync

    settings = get_settings()
    if not settings.gemini_api_key:
        return

    fd, path = tempfile.mkstemp(suffix=".jpg", dir=settings.temp_dir)
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(jpeg_bytes)
        vec = await asyncio.to_thread(embed_frame_sync, path)
        await asyncio.to_thread(upsert_image_sync, drive_file_id=drive_file_id, vector=vec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Image embed failed for %s: %s", drive_file_id, exc)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
