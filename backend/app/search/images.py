"""Vector search for indexed Drive images (Gemini Embedding 2 + Qdrant)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DriveFile
from app.gemini.tags import person_names_for_drive_file
from app.gemini.video_embeddings import embed_frame_sync, embed_text_sync
from app.qdrant.images import search_images_sync
from app.schemas import SearchResultFile

logger = logging.getLogger(__name__)


async def search_image_files(
    session: AsyncSession,
    query: str,
    *,
    person_name: str | None = None,
    folder_path: str | None = None,
) -> list[SearchResultFile]:
    """DeepImageSearch-style text→image retrieval using our Qdrant image index."""
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

    try:
        fused: dict[str, float] = {}
        for variant in queries:
            vec = await asyncio.to_thread(embed_text_sync, variant)
            for h in await asyncio.to_thread(
                search_images_sync,
                vec,
                limit=settings.gemini_image_result_limit,
                min_score=settings.gemini_image_min_score,
            ):
                fid = h["drive_file_id"]
                if fid not in fused or h["score"] > fused[fid]:
                    fused[fid] = h["score"]
        hits = [
            {"drive_file_id": fid, "score": score}
            for fid, score in sorted(fused.items(), key=lambda kv: -kv[1])
        ][: settings.gemini_image_result_limit]
    except Exception as exc:
        logger.warning("Image vector search failed (query=%r): %s", query, exc)
        return []

    results: list[SearchResultFile] = []
    seen: set[str] = set()

    for hit in hits:
        drive_file_id = hit["drive_file_id"]
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
        if person_name and not any(n.lower() == person_name.lower() for n in person_names):
            continue

        seen.add(drive_file_id)
        results.append(
            SearchResultFile(
                drive_file_id=drive_file_id,
                name=drive_file.name,
                path=drive_file.path,
                mime_type=drive_file.mime_type,
                person_names=person_names,
            )
        )

    logger.info("Image vector search: %d files for query %r", len(results), query)
    return results


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
