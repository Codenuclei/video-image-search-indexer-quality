from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.runtime_settings import get_runtime_settings
from app.db.app_settings_store import load_runtime_settings_from_db
from app.db.schema import (
    ensure_app_admins_seed,
    ensure_schema,
    recover_aborted_transaction_errors,
    recover_stuck_processing_files,
)
from app.pipelines.decode_recovery import (
    quarantine_stuck_decode_errors,
)
from app.db.advisory_locks import (
    is_background_leader,
    release_background_leader,
    try_become_background_leader,
)
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.dependencies import get_indexing_worker
from app.routers import (
    carousel_auth,
    carousel_oauth,
    carousel_script,
    drive,
    index,
    media,
    persons,
    settings,
    transcripts,
    youtube,
)
from app.pipelines.common import register_image_plugins
from app.workers.auto_indexer import auto_index_loop
from app.workers.maintenance import startup_maintenance
from app.workers.backup import backup_loop

logging.basicConfig(level=logging.INFO)


# Set after ensure_schema + runtime settings load. /health stays open; other
# routes return 503 until ready so requests never hang on a blocked first connect.
_boot_ready = asyncio.Event()
_boot_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _boot_error
    settings_obj = get_settings()
    logger = logging.getLogger(__name__)
    logger.info("Carousel Studio API starting — GIS + Drive OAuth served by this process")

    register_image_plugins()

    from app.video.youtube_download import prepare_youtube_cookies_at_startup

    prepare_youtube_cookies_at_startup()

    # Bound asyncio.to_thread / default executor so IMAGE_INDEX_MAX_PARALLEL cannot
    # spawn unbounded OS threads ("can't start new thread").
    try:
        loop = asyncio.get_running_loop()
        from app.concurrency.pools import cpu_thread_pool

        loop.set_default_executor(cpu_thread_pool())
        logger.info("Startup: asyncio default executor bound to cpu_thread_pool")
    except Exception:  # noqa: BLE001
        logger.exception("Startup: failed to bind default executor")

    library_reader = None

    # Yield ASAP so Railway /health can pass. Prior deploys hung forever on the
    # first DB call (ensure_schema) after cookies — ASGI never accepted traffic,
    # healthcheck timed out, and the proxy returned 502.
    # DB init still runs — just after yield, in _deferred_boot (not skipped).
    stop_event = asyncio.Event()
    worker = get_indexing_worker()
    _boot_ready.clear()
    _boot_error = None

    async def _deferred_boot() -> None:
        global _boot_error
        try:
            # Railway proxy / asyncpg can drop mid-DDL; retry with a fresh pool.
            last_exc: Exception | None = None
            for attempt in range(1, 6):
                try:
                    logger.info("Startup: ensuring DB schema… (attempt %d/5)", attempt)
                    await ensure_schema(get_engine())
                    await ensure_app_admins_seed(get_engine())
                    logger.info("Startup: loading runtime settings…")
                    await load_runtime_settings_from_db(get_session_factory())
                    # Warm every API worker's local revision cache before it is
                    # marked ready. Otherwise the first user routed to each
                    # Gunicorn worker can inherit indexer DB-pool contention.
                    try:
                        from app.drive.library_shell_cache import compute_library_revision_sql

                        async with get_session_factory()() as session:
                            await compute_library_revision_sql(session)
                    except Exception:  # noqa: BLE001
                        logger.exception("Startup: library revision cache warm failed")
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning(
                        "Startup DB attempt %d/5 failed: %s",
                        attempt,
                        str(exc)[:200],
                    )
                    try:
                        await get_engine().dispose()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(min(30, 2 * attempt))
            if last_exc is not None:
                raise last_exc
            _boot_ready.set()
            logger.info("Startup: DB ready (schema + settings)")

            # With Gunicorn multi-worker, only one process may run indexer /
            # push-channel / recovery loops. Others stay API-only.
            # RUN_INDEXER=false → API-only (or face-worker-only when RUN_FACE_WORKER).
            if not settings_obj.run_indexer and not settings_obj.run_face_worker:
                logger.info(
                    "Startup: RUN_INDEXER=false RUN_FACE_WORKER=false — API-only"
                )
                logger.info("Startup: deferred boot complete (api-only)")
                return

            if settings_obj.run_face_worker and not settings_obj.run_indexer:
                logger.info(
                    "Startup: face-worker role (RUN_FACE_WORKER=true, RUN_INDEXER=false)"
                )
                logger.info("Startup: deferred boot complete (face-worker)")
                return

            leader = await try_become_background_leader()
            if not leader:
                logger.info(
                    "Startup: API-only worker (background leader elected elsewhere)"
                )
                logger.info("Startup: deferred boot complete (api-only)")
                return

            logger.info("Startup: this worker is the background leader")

            await recover_stuck_processing_files(get_session_factory())
            await recover_aborted_transaction_errors(get_session_factory())
            quarantined = await quarantine_stuck_decode_errors(get_session_factory())
            if quarantined:
                logger.info("Startup: quarantined %d stuck decode-error file(s)", quarantined)

            from app.drive.indexing_pause import skip_corrupt_files

            async with get_session_factory()() as session:
                corrupt_skipped = await skip_corrupt_files(session)
                await session.commit()
            if corrupt_skipped:
                logger.info("Startup: skipped %d corrupt/unreadable file(s)", corrupt_skipped)

            # AppleDouble/.DS_Store rows are filtered and skipped by the indexer.
            # Never delete historical DriveFile rows during startup.

            from app.drive.conflicts import promote_duplicate_content_skips, reconcile_name_conflict_skips

            async with get_session_factory()() as session:
                promoted = await promote_duplicate_content_skips(session)
                requeued = await reconcile_name_conflict_skips(session)
                await session.commit()
            if promoted:
                logger.info(
                    "Startup: promoted %d duplicate_content skip(s) → PROCESSED",
                    promoted,
                )
            if requeued:
                logger.info(
                    "Startup: requeued %d name_conflict skip(s) with disambiguated names",
                    requeued,
                )

            # A fresh process owns no carousel tasks, so any row still marked
            # processing was orphaned by a previous shutdown and must be released
            # before the backlog can drain.
            await worker.reclaim_stale_carousel_locks(orphaned=True)
            await worker.resume_carousel_generation()
            runtime = get_runtime_settings()
            if runtime.auto_index_enabled:
                logger.info(
                    "Auto-index enabled (interval=%ss)",
                    runtime.auto_index_interval_seconds,
                )

            async def _seed_drive_cache_and_push() -> None:
                from app.drive.cache_refresh import refresh_drive_file_list_cache
                from app.drive.file_list_cache import get_file_list_cache
                from app.drive.push_channels import register_or_renew_channel
                from app.dependencies import get_drive_client

                # Wait briefly if a webhook race already started a refresh.
                cache = get_file_list_cache()
                for _ in range(60):
                    if not cache.refresh_in_flight:
                        break
                    await asyncio.sleep(1)
                if not cache.is_warm():
                    try:
                        # Seed memory on the leader; sync_db so other workers
                        # can serve /api/cache/files from Postgres.
                        result = await refresh_drive_file_list_cache(
                            source="startup",
                            sync_db=True,
                            process_pending=False,
                        )
                        logger.info("Startup Drive file-list cache seed: %s", result)
                    except Exception:  # noqa: BLE001
                        logger.exception("Startup Drive cache seed failed")
                else:
                    logger.info(
                        "Startup Drive cache already warm source=%s count=%d",
                        cache.source,
                        len(cache.files),
                    )
                try:
                    reg = await register_or_renew_channel(
                        get_drive_client(), force=True
                    )
                    logger.info("Startup Drive push channel: %s", reg)
                except Exception:  # noqa: BLE001
                    logger.exception("Startup Drive push registration failed")

            asyncio.create_task(_seed_drive_cache_and_push())
            logger.info("Startup: deferred boot complete (leader)")
        except Exception as exc:  # noqa: BLE001
            _boot_error = str(exc)[:240]
            logger.exception("Deferred startup recovery failed")
            # Unblock waiters so they can 503 with the error instead of hanging.
            _boot_ready.set()

    boot_task = asyncio.create_task(_deferred_boot())

    async def _start_workers_after_db() -> None:
        # Leadership is elected after DB readiness is published. Waiting only on
        # _boot_ready races the election and can make every Gunicorn worker
        # conclude it is API-only before one of them acquires the leader lock.
        await boot_task
        if _boot_error:
            logger.error("Skipping background workers — boot failed: %s", _boot_error)
            return

        worker_tasks: list[asyncio.Task] = []
        settings_now = get_settings()

        if settings_now.run_face_worker:
            from app.workers.face_queue import FaceWorkerLoop
            from app.workers.object_queue import ObjectWorkerLoop

            face_loop = FaceWorkerLoop(settings=settings_now)
            face_loop.ensure_started()
            app.state.face_worker_loop = face_loop
            object_loop = ObjectWorkerLoop()
            object_loop.ensure_started()
            app.state.object_worker_loop = object_loop
            logger.info(
                "Face worker loop started concurrency=%s",
                settings_now.face_worker_concurrency,
            )

        if settings_now.run_indexer:
            if not is_background_leader():
                logger.info("Skipping indexer loops — not leader")
            else:
                from app.workers.index_control import index_control_watch_loop

                auto_task = asyncio.create_task(auto_index_loop(worker, stop_event))
                control_task = asyncio.create_task(
                    index_control_watch_loop(worker, stop_event),
                    name="index-control-watcher",
                )
                maintenance_task = asyncio.create_task(
                    startup_maintenance(worker),
                    name="startup-maintenance",
                )
                backup_task = asyncio.create_task(backup_loop(stop_event))
                worker_tasks.extend([auto_task, control_task, maintenance_task, backup_task])
        else:
            logger.info("Skipping indexer loops — RUN_INDEXER=false")

        app.state.worker_tasks = tuple(worker_tasks)

    workers_starter = asyncio.create_task(_start_workers_after_db())
    app.state.boot_task = boot_task
    app.state.workers_starter = workers_starter
    logger.info("Startup: accepting traffic (deferred boot running)")

    yield

    stop_event.set()
    workers_starter.cancel()
    boot_task.cancel()
    face_loop = getattr(app.state, "face_worker_loop", None)
    if face_loop is not None:
        try:
            await face_loop.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Face worker loop stop failed")
    object_loop = getattr(app.state, "object_worker_loop", None)
    if object_loop is not None:
        try:
            await object_loop.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Object worker loop stop failed")
    worker_tasks = getattr(app.state, "worker_tasks", ())
    for task in (workers_starter, boot_task, *worker_tasks):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await release_background_leader()
    if library_reader is not None:
        library_reader.shutdown()
    await dispose_engine()


app = FastAPI(title="Carousel Studio API", version="1.0.0", lifespan=lifespan)

_settings = get_settings()
_extra_origins = [o.strip() for o in (_settings.allowed_origins or "").split(",") if o.strip()]
_carousel_origin = (_settings.carousel_frontend_url or "http://localhost:3002").rstrip("/")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3002",
        "http://127.0.0.1:3002",
        _carousel_origin,
        *_extra_origins,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _tag_carousel_vs_search(request, call_next):
    """Tag carousel vs Drive-search requests so logs are filterable.

    Carousel routes emit ``[carousel]`` lines with a stable ``trace=`` id.
    Generic ``/search`` (image/moment Drive search) emits ``[drive-search]``.
    """
    import time

    from app.search.carousel_trace import (
        bind_carousel_context,
        carousel_log,
        reset_carousel_context,
    )

    path = request.url.path
    started = time.perf_counter()
    if path.startswith("/search/carousel"):
        route = path.removeprefix("/search/carousel") or "/"
        tokens = bind_carousel_context(
            trace_id=request.headers.get("x-request-id"),
            route=route,
        )
        carousel_log(
            "http_request_start",
            method=request.method,
            path=path,
            client=(request.client.host if request.client else "-"),
        )
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            carousel_log(
                "http_request_error",
                level=40,
                method=request.method,
                path=path,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error_type=type(exc).__name__,
                error=str(exc)[:200] or type(exc).__name__,
            )
            reset_carousel_context(tokens)
            raise
        carousel_log(
            "http_request_end",
            method=request.method,
            path=path,
            status=getattr(response, "status_code", "-"),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        try:
            response.headers["X-Request-ID"] = (
                request.headers.get("x-request-id")
                or response.headers.get("x-request-id")
                or ""
            )
        except Exception:  # noqa: BLE001
            pass
        from app.search.carousel_trace import current_trace_id

        try:
            if not response.headers.get("X-Request-ID"):
                response.headers["X-Request-ID"] = current_trace_id()
        except Exception:  # noqa: BLE001
            pass
        reset_carousel_context(tokens)
        return response

    return await call_next(request)


@app.middleware("http")
async def _require_boot_ready(request, call_next):
    """Liveness (/health) always works; other routes wait briefly then 503."""
    from fastapi.responses import JSONResponse

    path = request.url.path
    # /health does its own readiness gating (see handler) — skip the 20s wait.
    if path == "/health":
        return await call_next(request)
    # Google Drive push must ACK quickly even during deferred boot.
    if path in ("/api/webhooks/drive", "/webhooks/drive"):
        return await call_next(request)
    if not _boot_ready.is_set():
        try:
            await asyncio.wait_for(_boot_ready.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "starting",
                    "message": "Database boot still in progress; retry shortly",
                },
                headers={"Retry-After": "5"},
            )
    if _boot_error:
        return JSONResponse(
            status_code=503,
            content={"detail": "boot_failed", "message": _boot_error},
            headers={"Retry-After": "15"},
        )
    return await call_next(request)

app.include_router(carousel_auth.router)
app.include_router(carousel_oauth.router)
app.include_router(drive.router)
app.include_router(index.router)
app.include_router(carousel_script.router)
app.include_router(settings.router)
app.include_router(persons.router)
app.include_router(media.router)
app.include_router(youtube.router)
app.include_router(transcripts.router)


@app.get("/health")
async def health():
    """Readiness probe: 200 only once deferred boot finished.

    Railway keeps the previous deployment serving until this passes, so
    gating on ``_boot_ready`` makes deploys hitless — otherwise traffic is
    swapped to workers that still 503/hang for up to 20s while the DB boot
    runs. No direct DB/Qdrant/Drive I/O here: ``_boot_ready`` is set by the
    boot task, and the Drive cache seed happens after it, so a slow Drive
    sync can never fail the deploy check. Railway only uses this at deploy
    time, so a later dependency blip cannot take a live service offline.
    Use ``/health/detail`` for dependency diagnostics.
    """
    from fastapi.responses import JSONResponse

    if not _boot_ready.is_set():
        try:
            await asyncio.wait_for(_boot_ready.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=503, content={"status": "starting"})
    if _boot_error:
        return JSONResponse(
            status_code=503,
            content={"status": "boot_failed", "error": _boot_error},
        )
    return {"status": "ok"}


@app.get("/health/detail")
async def health_detail():
    """
    Aggregated dependency check for the whole stack.

    Checks DB/Qdrant/Drive metadata in parallel. Not used as the Railway
    healthcheck — slow or degraded deps must not take the service offline.
    """
    import time
    from sqlalchemy import text as sa_text
    from app.db.session import get_session_factory

    settings_obj = get_settings()
    t0 = time.monotonic()

    async def _ping_db() -> dict:
        db_started = time.monotonic()
        try:
            async with get_session_factory()() as session:
                await session.execute(sa_text("SELECT 1"))
            from app.db.session import get_engine

            pool = get_engine().sync_engine.pool
            return {
                "status": "ok",
                "ping_ms": round((time.monotonic() - db_started) * 1000, 1),
                "pool": {
                    "size": pool.size(),
                    "checked_in": pool.checkedin(),
                    "checked_out": pool.checkedout(),
                    "overflow": pool.overflow(),
                },
            }
        except Exception as exc:
            return {
                "status": "unreachable",
                "ping_ms": round((time.monotonic() - db_started) * 1000, 1),
                "error": str(exc)[:120],
            }

    async def _ping_drive() -> dict:
        try:
            async with get_session_factory()() as s:
                user = (
                    await s.execute(
                        sa_text("SELECT id, email, selected_folder_name FROM drive_users LIMIT 1")
                    )
                ).fetchone()
            if user:
                return {"status": "ok", "email": user[1], "folder": user[2]}
            return {"status": "not_connected"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)[:120]}

    db_result, drive_result = await asyncio.gather(
        _ping_db(),
        _ping_drive(),
        return_exceptions=False,
    )

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    all_ok = db_result.get("status") == "ok"

    return {
        "status": "ok" if all_ok else "degraded",
        "elapsed_ms": elapsed_ms,
        "services": {
            "carousel_api": {
                "status": "ok",
                "product": "carousel",
                "video_indexing": settings_obj.video_indexing_enabled,
                "video_index_max_parallel": settings_obj.video_index_max_parallel,
                "run_indexer": settings_obj.run_indexer,
            },
            "database": db_result,
            "google_drive": drive_result,
        },
    }


