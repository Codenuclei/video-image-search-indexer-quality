from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.runtime_settings import get_runtime_settings
from app.db.app_settings_store import load_runtime_settings_from_db
from app.db.schema import ensure_schema, recover_stuck_processing_files
from app.pipelines.decode_recovery import (
    quarantine_stuck_decode_errors,
)
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.dependencies import get_indexing_worker
from app.fennec.client import get_fennec_client
from app.routers import clusters, drive, drive_oauth, faces, fennec, folder_contexts, index, media, persons, qwen_vlm, search, settings, webhooks, youtube
from app.svs.client import svs_ready  # kept for backwards compat — SVS disabled
from app.pipelines.common import register_image_plugins
from app.workers.auto_indexer import auto_index_loop
from app.workers.maintenance import startup_maintenance

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings_obj = get_settings()
    logger = logging.getLogger(__name__)
    logger.info(
        "DriveFaceIndexer (Gemini File Search) starting — connector=%s store=%s",
        settings_obj.drive_connector_base_url,
        settings_obj.gemini_file_search_store_display_name,
    )

    register_image_plugins()

    await ensure_schema(get_engine())
    await load_runtime_settings_from_db(get_session_factory())
    await recover_stuck_processing_files(get_session_factory())
    quarantined = await quarantine_stuck_decode_errors(get_session_factory())
    if quarantined:
        logger.info("Startup: quarantined %d stuck decode-error file(s)", quarantined)

    from app.drive.indexing_pause import skip_corrupt_files

    async with get_session_factory()() as session:
        corrupt_skipped = await skip_corrupt_files(session)
        await session.commit()
    if corrupt_skipped:
        logger.info("Startup: skipped %d corrupt/unreadable file(s)", corrupt_skipped)

    stop_event = asyncio.Event()
    worker = get_indexing_worker()
    auto_task = asyncio.create_task(auto_index_loop(worker, stop_event))
    maintenance_task = asyncio.create_task(startup_maintenance(worker))
    runtime = get_runtime_settings()
    if runtime.auto_index_enabled:
        logger.info("Auto-index enabled (interval=%ss)", runtime.auto_index_interval_seconds)

    yield

    stop_event.set()
    auto_task.cancel()
    maintenance_task.cancel()
    try:
        await auto_task
    except asyncio.CancelledError:
        pass
    try:
        await maintenance_task
    except asyncio.CancelledError:
        pass
    await dispose_engine()


app = FastAPI(title="DriveFaceIndexer", version="2.0.0", lifespan=lifespan)

_settings = get_settings()
_extra_origins = [o.strip() for o in (_settings.allowed_origins or "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "http://localhost:3000",
        *_extra_origins,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(drive_oauth.router)
app.include_router(drive.router)
app.include_router(index.router)
app.include_router(search.router)
app.include_router(webhooks.router)
app.include_router(settings.router)
app.include_router(persons.router)
app.include_router(clusters.router)
app.include_router(faces.router)
app.include_router(media.router)
app.include_router(folder_contexts.router)
app.include_router(fennec.router)
app.include_router(qwen_vlm.router)
app.include_router(youtube.router)


@app.get("/health")
async def health():
    """
    Aggregated health check for the whole stack.

    Checks every service in parallel and returns a single JSON payload
    showing what is up, what is down, and key runtime metrics (qdrant
    point count, number of tracked Drive files, etc.).
    """
    import time
    import httpx
    from sqlalchemy import text as sa_text
    from app.db.session import get_session_factory

    settings_obj = get_settings()
    t0 = time.monotonic()

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _ping_http(url: str, timeout: float = 4.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(url, headers={"Connection": "close"})
                r.raise_for_status()
                return {"status": "ok", **r.json()}
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)[:120]}

    async def _ping_db() -> dict:
        try:
            async with get_session_factory()() as session:
                await session.execute(sa_text("SELECT 1"))
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)[:120]}

    # ── fire all checks in parallel ───────────────────────────────────────────
    async def _ping_qdrant() -> dict:
        try:
            from app.qdrant.client import collection_info_sync
            from app.qdrant.images import collection_info_sync as image_collection_info_sync
            from app.qdrant.image_captions import collection_info_sync as caption_collection_info_sync
            video = await asyncio.to_thread(collection_info_sync)
            images = await asyncio.to_thread(image_collection_info_sync)
            captions = await asyncio.to_thread(caption_collection_info_sync)
            return {"status": "ok", "video": video, "images": images, "captions": captions}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:120]}

    async def _ping_drive() -> dict:
        try:
            from app.db.models import DriveUser
            from app.db.session import get_session_factory
            async with get_session_factory()() as s:
                user = (await s.execute(sa_text("SELECT id, email, selected_folder_name FROM drive_users LIMIT 1"))).fetchone()
            if user:
                return {"status": "ok", "email": user[1], "folder": user[2]}
            return {"status": "not_connected"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:120]}

    db_result, qdrant_result, drive_result = await asyncio.gather(
        _ping_db(),
        _ping_qdrant(),
        _ping_drive(),
        return_exceptions=False,
    )

    elapsed_ms = round((time.monotonic() - t0) * 1000)

    all_ok = db_result.get("status") == "ok" and qdrant_result.get("status") == "ok"

    return {
        "status": "ok" if all_ok else "degraded",
        "elapsed_ms": elapsed_ms,
        "services": {
            "dfi_backend": {
                "status": "ok",
                "search_mode": "gemini-embedding-2-video+images-qdrant+gemini-file-search",
                "video_indexing": settings_obj.video_indexing_enabled,
                "auto_index_interval_s": settings_obj.auto_index_interval_seconds,
                "qdrant_video_collection": settings_obj.qdrant_collection,
                "qdrant_images_collection": settings_obj.qdrant_images_collection,
                "gemini_video_min_score": settings_obj.gemini_video_min_score,
                "gemini_image_min_score": settings_obj.gemini_image_min_score,
                "video_index_max_parallel": settings_obj.video_index_max_parallel,
                "image_index_max_parallel": settings_obj.image_index_max_parallel,
                "image_caption_batch_size": settings_obj.image_caption_batch_size,
                "image_caption_batch_parallel": settings_obj.image_caption_batch_parallel,
                "image_embed_backfill_parallel": settings_obj.image_embed_backfill_parallel,
                "maintenance_batches_per_tick": settings_obj.maintenance_batches_per_tick,
                "gemini_embed_max_concurrent": settings_obj.gemini_embed_max_concurrent,
                "gemini_vlm_max_concurrent": settings_obj.gemini_vlm_max_concurrent,
                "gemini_upload_max_concurrent": settings_obj.gemini_upload_max_concurrent,
            },
            "database": db_result,
            "google_drive": drive_result,
            "qdrant": qdrant_result,
        },
    }


