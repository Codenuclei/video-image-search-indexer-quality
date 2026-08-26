from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# ArcFace (buffalo_l) embeddings are 512-dimensional.
EMBEDDING_DIM = 512

# NOTE: Postgres enums in this project were created by SQLAlchemy using Python
# *member names* (PENDING, IMAGE, UNKNOWN, …), not .value strings. Do not add
# values_callable=lambda e: [m.value …] — that binds lowercase labels the DB
# does not have. Soft-archive must ADD VALUE 'ARCHIVED' (uppercase) to match.


class DriveUser(Base):
    """Stored Google OAuth credentials for the connected Drive account."""

    __tablename__ = "drive_users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Google sub
    email: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    selected_folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_folder_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class IndexedFolder(Base):
    """Historical record of every Drive folder that was selected for indexing.

    Persists the folder Drive URL even after the user switches or disconnects
    the active sync root — additive history only, never wiped on folder change.
    """

    __tablename__ = "indexed_folders"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Google Drive folder id
    name: Mapped[str] = mapped_column(String, nullable=False)
    drive_url: Mapped[str] = mapped_column(Text, nullable=False)
    drive_user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    drive_user_email: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    first_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FileIndexConflict(Base):
    """Pending or resolved same-name / same-content indexing conflicts."""

    __tablename__ = "file_index_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incoming_file_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    existing_file_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # same_content | same_content_diff_name | same_name_diff_content
    conflict_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # pending | skipped | replaced | merged | autoskipped
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    incoming_name: Mapped[str] = mapped_column(String, nullable=False)
    existing_name: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DriveFileStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    ERROR = "error"
    SKIPPED = "skipped"
    # Soft-detached from live Drive listing; vectors/thumbs/media are retained forever.
    ARCHIVED = "archived"


class MediaType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    PDF = "pdf"


class ClusterStatus(str, enum.Enum):
    UNKNOWN = "unknown"
    NAMED = "named"
    IGNORED = "ignored"


class DriveFile(Base):
    """A single file as seen through the existing Drive Connector's API."""

    __tablename__ = "drive_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Google Drive file id
    name: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False, index=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Drive videos can exceed 2GiB; INTEGER overflows (AI Summit C0015.MP4 ~4.7GB).
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[DriveFileStatus] = mapped_column(
        Enum(DriveFileStatus, name="drive_file_status"),
        default=DriveFileStatus.PENDING,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    decode_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gemini_document_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Wall-clock start of the current index claim (PENDING→PROCESSING). Used for TAT.
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Wall-clock when the file is fully done for search (captioned image / indexed
    # video transcript, or ERROR). Claim→done TAT uses this vs processing_started_at.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False, default="drive", server_default="drive")
    # Carousel pipeline lock is deliberately separate from indexing status.
    # It guards generation/edit mutations without making cache reads wait on Drive indexing.
    carousel_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="idle", server_default="idle", index=True
    )
    carousel_lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carousel_lock_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carousel_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    carousel_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    carousel_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Content identity for cross-folder / cross-user dedupe (Drive md5Checksum or local sha256).
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    content_hash_algo: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Relative path under media_cache_dir after a successful durable copy.
    cache_rel_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # IndexedFolders.id of the root folder this file was discovered under (historical).
    root_folder_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Soft-archive timestamp — set when file leaves live Drive listing / 404 / explicit detach.
    # Never implies deletion of Qdrant vectors, captions, thumbnails, or cached media.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Search/index display name when Drive basename collides (e.g. "photo (1).jpg").
    index_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # 64-bit OpenCV dHash (16 hex chars) for cross-resolution visual dedupe.
    visual_hash: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped["Media | None"] = relationship(back_populates="drive_file", uselist=False)

    @property
    def display_name(self) -> str:
        return (self.index_name or self.name or "").strip()


class Media(Base):
    """A processed unit of content derived from a drive file (image/video/pdf)."""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(ForeignKey("drive_files.id", ondelete="CASCADE"), unique=True)
    type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="media_type"),
        nullable=False,
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    drive_file: Mapped[DriveFile] = relationship(back_populates="media")
    faces: Mapped[list["Face"]] = relationship(back_populates="media", cascade="all, delete-orphan")
    ocr_pages: Mapped[list["OcrPage"]] = relationship(back_populates="media", cascade="all, delete-orphan")
    recognitions: Mapped[list["Recognition"]] = relationship(back_populates="media", cascade="all, delete-orphan")
    video_segments: Mapped[list["VideoSegment"]] = relationship(
        back_populates="media", cascade="all, delete-orphan"
    )


class Person(Base):
    """A named individual, created once a face cluster is labeled by the user."""

    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Manual tag: student | non_student (null = student by default for search)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    representative_face_id: Mapped[int | None] = mapped_column(
        ForeignKey("faces.id", ondelete="SET NULL", use_alter=True, name="fk_person_representative_face"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    clusters: Mapped[list["FaceCluster"]] = relationship(back_populates="person")


class FaceCluster(Base):
    """A group of embeddings believed to belong to the same (possibly unnamed) person."""

    __tablename__ = "face_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    representative_face_id: Mapped[int | None] = mapped_column(
        ForeignKey("faces.id", ondelete="SET NULL", use_alter=True, name="fk_cluster_representative_face"),
        nullable=True,
    )
    status: Mapped[ClusterStatus] = mapped_column(
        Enum(ClusterStatus, name="cluster_status"),
        default=ClusterStatus.UNKNOWN,
        nullable=False,
    )
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    person: Mapped[Person | None] = relationship(back_populates="clusters")
    faces: Mapped[list["Face"]] = relationship(back_populates="cluster", foreign_keys="Face.cluster_id")


class Face(Base):
    """A single detected face within a piece of media (one image frame / pdf page)."""

    __tablename__ = "faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"))
    bbox_x: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_y: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_width: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_height: Mapped[float] = mapped_column(Float, nullable=False)
    detection_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    frame_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("face_clusters.id", ondelete="SET NULL"), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped[Media] = relationship(back_populates="faces")
    cluster: Mapped[FaceCluster | None] = relationship(back_populates="faces", foreign_keys=[cluster_id])
    embedding: Mapped["FaceEmbedding | None"] = relationship(
        back_populates="face", uselist=False, cascade="all, delete-orphan"
    )


class FaceEmbedding(Base):
    """The ArcFace embedding vector for a single face, indexed for nearest-neighbor search."""

    __tablename__ = "face_embeddings"

    face_id: Mapped[int] = mapped_column(ForeignKey("faces.id", ondelete="CASCADE"), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)

    face: Mapped[Face] = relationship(back_populates="embedding")


class Recognition(Base):
    """A logged occurrence of a person (or unresolved face) within a piece of media."""

    __tablename__ = "recognitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"))
    face_id: Mapped[int] = mapped_column(ForeignKey("faces.id", ondelete="CASCADE"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped[Media] = relationship(back_populates="recognitions")


class OcrPage(Base):
    """Extracted OCR text for a single rendered PDF page."""

    __tablename__ = "ocr_pages"
    __table_args__ = (UniqueConstraint("media_id", "page_number", name="uq_ocr_page_media_page"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"))
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped[Media] = relationship(back_populates="ocr_pages")


class AppSettings(Base):
    """Singleton (id=1) persisted UI/runtime toggles — survives deploys and restarts."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auto_index_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    auto_index_interval_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    reindex_errored_files: Mapped[bool] = mapped_column(default=False, nullable=False)
    reindex_skipped_files: Mapped[bool] = mapped_column(default=False, nullable=False)
    follow_shortcut_folders: Mapped[bool] = mapped_column(default=True, nullable=False)
    experimental_manual_face_tag: Mapped[bool] = mapped_column(default=False, nullable=False)
    gemini_file_search_search_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    search_parallel_variants_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    search_use_captions: Mapped[bool] = mapped_column(default=False, nullable=False)
    search_rerank_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    go_indexer_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Carousel LLM: auto | openrouter | claude | gemini (key stays in env).
    carousel_llm_provider: Mapped[str] = mapped_column(String, default="auto", nullable=False)
    openrouter_model: Mapped[str] = mapped_column(
        String, default="anthropic/claude-sonnet-4", nullable=False
    )
    claude_model: Mapped[str] = mapped_column(
        String, default="claude-sonnet-4-5-20250929", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FolderContext(Base):
    """User-supplied description / context for a Drive folder path."""

    __tablename__ = "folder_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_path: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IndexingFolderPause(Base):
    """Folders excluded from further indexing (library stop button)."""

    __tablename__ = "indexing_folder_pauses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_path: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexControlState(Base):
    """Heartbeat published by the elected indexer for the isolated control service."""

    __tablename__ = "index_control_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_image_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_video_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watcher_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Gemini Embedding 2 vectors (body crops) are 3072-dimensional.
BODY_EMBEDDING_DIM = 3072


class BodySignature(Base):
    """
    Append-only re-id layer: clothing/body-structure embedding for a detected face.

    Only stored for prominent, (near-)full-body appearances. Links a body crop
    embedding back to its face so unlabeled faces can be matched to persons via
    body/clothing similarity within the same shoot/folder.
    """

    __tablename__ = "body_signatures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    face_id: Mapped[int] = mapped_column(
        ForeignKey("faces.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Body crop box in original image pixels.
    body_x: Mapped[float] = mapped_column(Float, nullable=False)
    body_y: Mapped[float] = mapped_column(Float, nullable=False)
    body_width: Mapped[float] = mapped_column(Float, nullable=False)
    body_height: Mapped[float] = mapped_column(Float, nullable=False)
    # Gating metrics: face area fraction of the image + how much of the
    # expected full-body extent actually fit inside the frame.
    prominence: Mapped[float] = mapped_column(Float, nullable=False)
    body_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    is_full_body: Mapped[bool] = mapped_column(default=False, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(BODY_EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FaceWebMatch(Base):
    """
    Append-only web-identification layer: reverse-image-search results for a
    face thumbnail, including any discovered LinkedIn profile URL.
    """

    __tablename__ = "face_web_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    face_id: Mapped[int] = mapped_column(ForeignKey("faces.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoSegment(Base):
    """Transcript cue or sampled moment from a video (VTT or embedded captions)."""

    __tablename__ = "video_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"))
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # BCP-47-ish tag for spoken/caption language of ``text`` (e.g. "en").
    # Null = unknown / legacy row not yet classified.
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    frame_path: Mapped[str | None] = mapped_column(String, nullable=True)
    vlm_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped[Media] = relationship(back_populates="video_segments")


class CarouselGenerationSave(Base):
    """Autosaved carousel studio generations (themes or topics/hooks)."""

    __tablename__ = "carousel_generation_saves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 'topics_hooks' (default) | 'themes' | 'carousel'
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="topics_hooks", index=True)
    theme_key: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    # Cache key for themes kind: invalidate when transcript or model changes.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transcript_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    layout_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="single_1")
    copy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="p0")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CarouselItemFeedback(Base):
    """Per-theme / per-hook thumbs + short comment from Carousel Studio."""

    __tablename__ = "carousel_item_feedback"
    __table_args__ = (
        UniqueConstraint(
            "drive_file_id",
            "target_kind",
            "target_key",
            name="uq_carousel_item_feedback_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # theme | hook
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Stable id (theme_id / hook id) or normalized text fallback.
    target_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_label: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # up | down | None (comment-only)
    rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppAdmin(Base):
    """Allowlist of emails that may open the Admin UI (SSR/middleware gated)."""

    __tablename__ = "app_admins"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)


class FaceJobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class FaceJob(Base):
    """Durable InsightFace work item claimed via FOR UPDATE SKIP LOCKED."""

    __tablename__ = "face_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(
        ForeignKey("drive_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[FaceJobStatus] = mapped_column(
        Enum(FaceJobStatus, name="face_job_status"),
        default=FaceJobStatus.PENDING,
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CarouselItemReference(Base):
    """Image or copywriting reference attached to a theme/hook in Carousel Studio."""

    __tablename__ = "carousel_item_reference"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # theme | hook
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    target_label: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # image | copy
    ref_kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # Absolute http(s) URL, same-origin /media/... path, or Drive file URL.
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional frame timestamp when the image is a video frame pick.
    frame_ts: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Reference copy / writing notes.
    copy_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Short optional label (e.g. "moodboard", "competitor hook").
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SearchQueryCache(Base):
    """Folder-scoped search result cache shared across Gunicorn workers."""

    __tablename__ = "search_query_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_embedding: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    folder_path: Mapped[str] = mapped_column(String, nullable=False, default="", index=True)
    person: Mapped[str] = mapped_column(String, nullable=False, default="", index=True)
    mime: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    captions: Mapped[bool] = mapped_column(default=False, nullable=False)
    rerank: Mapped[bool] = mapped_column(default=True, nullable=False)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    folder_fp: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    cluster_fp: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cluster_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

