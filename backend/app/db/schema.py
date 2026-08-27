from __future__ import annotations

import logging

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db import models  # noqa: F401
from app.db.models import DriveFile, DriveFileStatus

logger = logging.getLogger(__name__)


async def _ensure_enum_label(conn, type_name: str, label: str) -> None:
    """Add a Postgres enum label if missing (safe, non-destructive).

    Checks ``pg_enum`` first so we never rely solely on IF NOT EXISTS support,
    then runs ``ALTER TYPE … ADD VALUE IF NOT EXISTS``.
    """
    if not type_name.replace("_", "").isalnum() or not label.replace("_", "").isalnum():
        raise ValueError(f"refusing unsafe enum identifiers: {type_name!r} {label!r}")
    exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM pg_type t
            JOIN pg_enum e ON t.oid = e.enumtypid
            WHERE t.typname = :type_name AND e.enumlabel = :label
            """
        ),
        {"type_name": type_name, "label": label},
    )
    if exists:
        return
    await conn.execute(
        text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")
    )
    logger.info("Added Postgres enum label %s.%s", type_name, label)


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        await conn.scalar(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"table": table, "column": column},
        )
    )


async def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """ADD COLUMN only when missing.

    Postgres ``ADD COLUMN IF NOT EXISTS`` still takes AccessExclusiveLock even when
    the column already exists — that blocks boot behind idle-in-transaction indexers.
    """
    if await _column_exists(conn, table, column):
        return
    await conn.execute(text(ddl))


async def _index_exists(conn, index_name: str) -> bool:
    return bool(
        await conn.scalar(
            text(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'public' AND indexname = :name
                """
            ),
            {"name": index_name},
        )
    )


async def _ensure_index(conn, index_name: str, ddl: str) -> None:
    if await _index_exists(conn, index_name):
        return
    await conn.execute(text(ddl))


_ONLINE_INDEX_DDLS = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_faces_media_id ON faces (media_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_faces_person_media "
    "ON faces (person_id, media_id) WHERE person_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_faces_cluster_media "
    "ON faces (cluster_id, media_id) WHERE cluster_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_face_clusters_person_id "
    "ON face_clusters (person_id) WHERE person_id IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_face_embeddings_embedding_cosine "
    "ON face_embeddings USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_face_clusters_centroid_cosine "
    "ON face_clusters USING hnsw (centroid vector_cosine_ops) WHERE centroid IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_drive_files_content_algo_hash "
    "ON drive_files (content_hash_algo, content_hash) WHERE content_hash IS NOT NULL",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_drive_files_status_created "
    "ON drive_files (status, created_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_face_jobs_pending_created "
    "ON face_jobs (created_at, id) WHERE status = 'PENDING'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_object_jobs_pending_created "
    "ON object_jobs (created_at, id) WHERE status = 'PENDING'",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_media_object_labels_search "
    "ON media_object_labels (canonical_label, confidence DESC, media_id)",
)


async def _ensure_online_indexes(engine: AsyncEngine) -> None:
    """Repair production index drift without blocking table reads/writes."""
    async with engine.connect() as raw_conn:
        conn = await raw_conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool(
            await conn.scalar(
                text("SELECT pg_try_advisory_lock(hashtext('dfi-online-indexes'))")
            )
        )
        if not acquired:
            logger.info("Online index verification already running on another replica")
            return
        try:
            for ddl in _ONLINE_INDEX_DDLS:
                try:
                    await conn.execute(text(ddl))
                except Exception:  # noqa: BLE001
                    # A failed optional online index must not take down API boot.
                    # It remains visible in logs and will be retried next boot.
                    logger.exception("Online index creation failed: %s", ddl)
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtext('dfi-online-indexes'))")
            )


async def ensure_schema(engine: AsyncEngine) -> None:
    """Create pgvector extension and tables if missing (idempotent)."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
                )
        except Exception:  # noqa: BLE001
            logger.info(
                "pg_stat_statements unavailable; enable it in Railway Postgres "
                "shared_preload_libraries for per-query telemetry"
            )
        await conn.run_sync(Base.metadata.create_all)

        # Required by the current ORM at startup. Keep this ahead of the warm-DB
        # shortcut so a newly introduced setting cannot be skipped.
        await _ensure_column(
            conn,
            "app_settings",
            "search_semantic_min_score",
            "ALTER TABLE app_settings ADD COLUMN search_semantic_min_score "
            "DOUBLE PRECISION NOT NULL DEFAULT 0.32",
        )
        await _ensure_column(
            conn,
            "face_jobs",
            "scan_completed_at",
            "ALTER TABLE face_jobs ADD COLUMN scan_completed_at TIMESTAMPTZ",
        )
        await _ensure_column(
            conn,
            "face_jobs",
            "detected_face_count",
            "ALTER TABLE face_jobs ADD COLUMN detected_face_count INTEGER",
        )
        object_settings = (
            ("object_lane_enabled", "BOOLEAN NOT NULL DEFAULT false"),
            ("object_backfill_enabled", "BOOLEAN NOT NULL DEFAULT false"),
            ("object_confidence_floor", "DOUBLE PRECISION NOT NULL DEFAULT 0.72"),
            ("object_max_labels", "INTEGER NOT NULL DEFAULT 12"),
            ("object_batch_size", "INTEGER NOT NULL DEFAULT 8"),
            ("object_face_priority_ratio", "INTEGER NOT NULL DEFAULT 10"),
        )
        for column, ddl_type in object_settings:
            await _ensure_column(
                conn,
                "app_settings",
                column,
                f"ALTER TABLE app_settings ADD COLUMN {column} {ddl_type}",
            )

        # Warm prod DBs already have additive columns. Skip ALTER TABLE entirely —
        # even IF NOT EXISTS takes AccessExclusiveLock and can 503 the API on boot.
        warm = await _column_exists(conn, "drive_files", "archived_at")
        face_jobs = bool(
            await conn.scalar(
                text(
                    """
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'face_jobs'
                    """
                )
            )
        )
        algo_len = await conn.scalar(
            text(
                """
                SELECT character_maximum_length FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'carousel_generation_saves'
                  AND column_name = 'algorithm_version'
                """
            )
        )
        size_type = await conn.scalar(
            text(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'drive_files'
                  AND column_name = 'size'
                """
            )
        )
        if warm and face_jobs and (algo_len or 0) >= 64 and size_type == "bigint":
            await _ensure_enum_label(conn, "drive_file_status", "ARCHIVED")
            logger.info("ensure_schema: warm DB — skipped additive ALTER TABLE locks")
            # CONCURRENTLY must use a separate autocommit connection after all
            # startup DDL is visible and this transaction has released locks.
            await conn.commit()
            await _ensure_online_indexes(engine)
            return

        await _ensure_column(
            conn,
            "drive_files",
            "gemini_document_name",
            "ALTER TABLE drive_files ADD COLUMN gemini_document_name VARCHAR",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "source",
            "ALTER TABLE drive_files ADD COLUMN source VARCHAR NOT NULL DEFAULT 'drive'",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_status",
            "ALTER TABLE drive_files ADD COLUMN carousel_status "
            "VARCHAR(24) NOT NULL DEFAULT 'idle'",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_lock_token",
            "ALTER TABLE drive_files ADD COLUMN carousel_lock_token VARCHAR(64)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_lock_input_hash",
            "ALTER TABLE drive_files ADD COLUMN carousel_lock_input_hash VARCHAR(64)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_locked_at",
            "ALTER TABLE drive_files ADD COLUMN carousel_locked_at TIMESTAMPTZ",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_error",
            "ALTER TABLE drive_files ADD COLUMN carousel_error TEXT",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "carousel_attempts",
            "ALTER TABLE drive_files ADD COLUMN carousel_attempts "
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_index(
            conn,
            "ix_drive_files_carousel_status",
            "CREATE INDEX ix_drive_files_carousel_status ON drive_files (carousel_status)",
        )
        await _ensure_index(
            conn,
            "ix_drive_files_path",
            "CREATE INDEX ix_drive_files_path ON drive_files (path)",
        )
        await _ensure_column(
            conn,
            "persons",
            "role",
            "ALTER TABLE persons ADD COLUMN role VARCHAR(32)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "decode_attempts",
            "ALTER TABLE drive_files ADD COLUMN decode_attempts "
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "follow_shortcut_folders",
            "ALTER TABLE app_settings ADD COLUMN follow_shortcut_folders "
            "BOOLEAN NOT NULL DEFAULT true",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "experimental_manual_face_tag",
            "ALTER TABLE app_settings ADD COLUMN experimental_manual_face_tag "
            "BOOLEAN NOT NULL DEFAULT false",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "reindex_errored_files",
            "ALTER TABLE app_settings ADD COLUMN reindex_errored_files "
            "BOOLEAN NOT NULL DEFAULT false",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "reindex_skipped_files",
            "ALTER TABLE app_settings ADD COLUMN reindex_skipped_files "
            "BOOLEAN NOT NULL DEFAULT false",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "go_indexer_enabled",
            "ALTER TABLE app_settings ADD COLUMN go_indexer_enabled "
            "BOOLEAN NOT NULL DEFAULT false",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "carousel_llm_provider",
            "ALTER TABLE app_settings ADD COLUMN carousel_llm_provider "
            "VARCHAR NOT NULL DEFAULT 'auto'",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "openrouter_model",
            "ALTER TABLE app_settings ADD COLUMN openrouter_model "
            "VARCHAR NOT NULL DEFAULT 'anthropic/claude-sonnet-4'",
        )
        await _ensure_column(
            conn,
            "app_settings",
            "claude_model",
            "ALTER TABLE app_settings ADD COLUMN claude_model "
            "VARCHAR NOT NULL DEFAULT 'claude-sonnet-4-5-20250929'",
        )
        # Carousel generation saves: themes vs topics/hooks + cache keys
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "kind",
            "ALTER TABLE carousel_generation_saves ADD COLUMN kind "
            "VARCHAR(32) NOT NULL DEFAULT 'topics_hooks'",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "model",
            "ALTER TABLE carousel_generation_saves ADD COLUMN model VARCHAR(128)",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "transcript_hash",
            "ALTER TABLE carousel_generation_saves ADD COLUMN transcript_hash VARCHAR(64)",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "source",
            "ALTER TABLE carousel_generation_saves ADD COLUMN source VARCHAR(32)",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "status",
            "ALTER TABLE carousel_generation_saves ADD COLUMN status "
            "VARCHAR(24) NOT NULL DEFAULT 'ready'",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "input_hash",
            "ALTER TABLE carousel_generation_saves ADD COLUMN input_hash VARCHAR(64)",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "layout_mode",
            "ALTER TABLE carousel_generation_saves ADD COLUMN layout_mode "
            "VARCHAR(32) NOT NULL DEFAULT 'single_1'",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "copy_version",
            "ALTER TABLE carousel_generation_saves ADD COLUMN copy_version "
            "INTEGER NOT NULL DEFAULT 1",
        )
        await _ensure_column(
            conn,
            "carousel_generation_saves",
            "algorithm_version",
            "ALTER TABLE carousel_generation_saves ADD COLUMN algorithm_version "
            "VARCHAR(64) NOT NULL DEFAULT 'p0'",
        )
        # Widen for CAROUSEL_ALGORITHM_VERSION (e.g. p0-fast-grouped-v3-quality-diversity).
        if (algo_len or 0) < 64 and await _column_exists(
            conn, "carousel_generation_saves", "algorithm_version"
        ):
            await conn.execute(
                text(
                    "ALTER TABLE carousel_generation_saves "
                    "ALTER COLUMN algorithm_version TYPE VARCHAR(64)"
                )
            )
        await _ensure_index(
            conn,
            "ix_carousel_generation_saves_kind",
            "CREATE INDEX ix_carousel_generation_saves_kind "
            "ON carousel_generation_saves (kind)",
        )
        await _ensure_index(
            conn,
            "ix_carousel_generation_saves_transcript_hash",
            "CREATE INDEX ix_carousel_generation_saves_transcript_hash "
            "ON carousel_generation_saves (transcript_hash)",
        )
        await _ensure_index(
            conn,
            "ix_carousel_generation_saves_input_hash",
            "CREATE INDEX ix_carousel_generation_saves_input_hash "
            "ON carousel_generation_saves (input_hash)",
        )
        await _ensure_index(
            conn,
            "ix_carousel_gen_saves_drive_kind_created",
            "CREATE INDEX ix_carousel_gen_saves_drive_kind_created "
            "ON carousel_generation_saves (drive_file_id, kind, created_at DESC)",
        )
        # Content-hash dedupe + durable media cache paths (additive).
        await _ensure_column(
            conn,
            "drive_files",
            "content_hash",
            "ALTER TABLE drive_files ADD COLUMN content_hash VARCHAR(128)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "content_hash_algo",
            "ALTER TABLE drive_files ADD COLUMN content_hash_algo VARCHAR(16)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "cache_rel_path",
            "ALTER TABLE drive_files ADD COLUMN cache_rel_path VARCHAR",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "root_folder_id",
            "ALTER TABLE drive_files ADD COLUMN root_folder_id VARCHAR",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "processing_started_at",
            "ALTER TABLE drive_files ADD COLUMN processing_started_at TIMESTAMPTZ",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "completed_at",
            "ALTER TABLE drive_files ADD COLUMN completed_at TIMESTAMPTZ",
        )
        # Large camera videos exceed int32 (~2.1GB); widen size so folder sync can insert them.
        if size_type and size_type != "bigint":
            await conn.execute(
                text("ALTER TABLE drive_files ALTER COLUMN size TYPE BIGINT USING size::bigint")
            )
        await _ensure_index(
            conn,
            "ix_drive_files_content_hash",
            "CREATE INDEX ix_drive_files_content_hash ON drive_files (content_hash)",
        )
        await _ensure_index(
            conn,
            "ix_drive_files_root_folder_id",
            "CREATE INDEX ix_drive_files_root_folder_id ON drive_files (root_folder_id)",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "index_name",
            "ALTER TABLE drive_files ADD COLUMN index_name VARCHAR",
        )
        await _ensure_column(
            conn,
            "drive_files",
            "visual_hash",
            "ALTER TABLE drive_files ADD COLUMN visual_hash VARCHAR(16)",
        )
        await _ensure_index(
            conn,
            "ix_drive_files_visual_hash",
            "CREATE INDEX ix_drive_files_visual_hash ON drive_files (visual_hash)",
        )
        # Transcript language for English-ensure / non-English purge.
        await _ensure_column(
            conn,
            "video_segments",
            "language",
            "ALTER TABLE video_segments ADD COLUMN language VARCHAR(16)",
        )
        await _ensure_index(
            conn,
            "ix_video_segments_language",
            "CREATE INDEX ix_video_segments_language ON video_segments (language)",
        )
        # Soft-archive: never-delete policy for indexed artifacts.
        # Live PG enums use SQLAlchemy *member names* (PENDING, SKIPPED, …).
        # ADD 'ARCHIVED' (uppercase). A prior mistaken 'archived' label may also
        # exist — leave it; code binds ARCHIVED via the Python enum member name.
        await _ensure_enum_label(conn, "drive_file_status", "ARCHIVED")
        await _ensure_column(
            conn,
            "drive_files",
            "archived_at",
            "ALTER TABLE drive_files ADD COLUMN archived_at TIMESTAMPTZ",
        )
        await _ensure_index(
            conn,
            "ix_drive_files_archived_at",
            "CREATE INDEX ix_drive_files_archived_at ON drive_files (archived_at)",
        )
        # Durable folder history (survives disconnect / folder switch).
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS indexed_folders (
                    id VARCHAR NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    drive_url TEXT NOT NULL,
                    drive_user_id VARCHAR,
                    drive_user_email VARCHAR,
                    is_active BOOLEAN NOT NULL DEFAULT false,
                    first_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_file_count INTEGER
                )
                """
            )
        )
        await _ensure_index(
            conn,
            "ix_indexed_folders_drive_user_id",
            "CREATE INDEX ix_indexed_folders_drive_user_id ON indexed_folders (drive_user_id)",
        )
        await _ensure_index(
            conn,
            "ix_indexed_folders_is_active",
            "CREATE INDEX ix_indexed_folders_is_active ON indexed_folders (is_active)",
        )
        # Durable InsightFace job queue (FOR UPDATE SKIP LOCKED claim).
        await conn.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE face_job_status AS ENUM (
                        'PENDING', 'PROCESSING', 'DONE', 'ERROR'
                    );
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS face_jobs (
                    id SERIAL PRIMARY KEY,
                    drive_file_id VARCHAR NOT NULL REFERENCES drive_files(id) ON DELETE CASCADE,
                    status face_job_status NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lock_token VARCHAR(64),
                    locked_at TIMESTAMPTZ,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        await _ensure_index(
            conn,
            "ix_face_jobs_status_created",
            "CREATE INDEX ix_face_jobs_status_created ON face_jobs (status, created_at)",
        )
        await _ensure_index(
            conn,
            "ix_face_jobs_drive_file_id",
            "CREATE INDEX ix_face_jobs_drive_file_id ON face_jobs (drive_file_id)",
        )
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_online_indexes(engine)
    logger.info("Database schema verified")


async def recover_stuck_processing_files(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Drain PROCESSING orphans without deleting media.

    Rows that already have Media → PROCESSED (add-if-missing resume),
    unless an open face_job is still pending/processing (multi-index path).
    Rows without Media → PENDING so face/embed can run.
    """
    from sqlalchemy import select

    from app.db.models import FaceJob, FaceJobStatus, Media

    async with session_factory() as session:
        stuck = (
            await session.execute(
                select(DriveFile).where(DriveFile.status == DriveFileStatus.PROCESSING)
            )
        ).scalars().all()
        if not stuck:
            return 0
        restored = 0
        requeued = 0
        face_waiting = 0
        for row in stuck:
            has_media = (
                await session.execute(
                    select(Media.id).where(Media.drive_file_id == row.id).limit(1)
                )
            ).scalar_one_or_none() is not None
            if has_media:
                open_face = (
                    await session.execute(
                        select(FaceJob.id)
                        .where(
                            FaceJob.drive_file_id == row.id,
                            FaceJob.status.in_(
                                (FaceJobStatus.PENDING, FaceJobStatus.PROCESSING)
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if open_face is not None:
                    face_waiting += 1
                    continue
                row.status = DriveFileStatus.PROCESSED
                row.error_message = None
                restored += 1
            else:
                row.status = DriveFileStatus.PENDING
                requeued += 1
        await session.commit()
        count = restored + requeued
        if count or face_waiting:
            logger.warning(
                "Drain stuck PROCESSING: restored=%d requeued=%d face_jobs_open=%d",
                restored,
                requeued,
                face_waiting,
            )
        return count


async def recover_aborted_transaction_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Re-queue ERROR files left by InFailedSQLTransactionError face-cluster fallout."""
    async with session_factory() as session:
        result = await session.execute(
            update(DriveFile)
            .where(
                DriveFile.status == DriveFileStatus.ERROR,
                DriveFile.error_message.ilike("%transaction aborted%"),
            )
            .values(status=DriveFileStatus.PENDING, error_message=None)
        )
        await session.commit()
        count = result.rowcount or 0
        if count:
            logger.warning(
                "Re-queued %d file(s) stuck on aborted face-cluster transactions",
                count,
            )
        return count


# Seeded once at schema ensure — manage via DB / ops after that (not frontend env).
_DEFAULT_APP_ADMINS = (
    "amisha.sharma@mastersunion.org",
    "dhananjay.jain@mastersunion.org",
    "sudeep.purwar@mastersunion.org",
    "abhishek.ghosh1@mastersunion.org",
)


async def ensure_app_admins_seed(engine: AsyncEngine) -> None:
    """Create app_admins rows for the initial operator set (idempotent)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_admins (
                    email VARCHAR(320) PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_by VARCHAR(320)
                )
                """
            )
        )
        for email in _DEFAULT_APP_ADMINS:
            await conn.execute(
                text(
                    """
                    INSERT INTO app_admins (email, created_by)
                    VALUES (:email, 'schema_seed')
                    ON CONFLICT (email) DO NOTHING
                    """
                ),
                {"email": email.strip().lower()},
            )
    logger.info("app_admins seed ensured (%d emails)", len(_DEFAULT_APP_ADMINS))
