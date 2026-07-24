from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# ArcFace (buffalo_l) embeddings are 512-dimensional.
EMBEDDING_DIM = 512


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


class DriveFileStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    ERROR = "error"
    SKIPPED = "skipped"


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
    path: Mapped[str] = mapped_column(String, nullable=False)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[DriveFileStatus] = mapped_column(
        Enum(DriveFileStatus, name="drive_file_status"), default=DriveFileStatus.PENDING, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    decode_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gemini_document_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="drive", server_default="drive")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped["Media | None"] = relationship(back_populates="drive_file", uselist=False)


class Media(Base):
    """A processed unit of content derived from a drive file (image/video/pdf)."""

    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(ForeignKey("drive_files.id", ondelete="CASCADE"), unique=True)
    type: Mapped[MediaType] = mapped_column(Enum(MediaType, name="media_type"), nullable=False)
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
        Enum(ClusterStatus, name="cluster_status"), default=ClusterStatus.UNKNOWN, nullable=False
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
    frame_path: Mapped[str | None] = mapped_column(String, nullable=True)
    vlm_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped[Media] = relationship(back_populates="video_segments")
