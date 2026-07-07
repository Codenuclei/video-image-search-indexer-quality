"""initial schema: persons, media, faces, face_embeddings, face_clusters, drive_files, ocr_pages, recognitions

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 512


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    drive_file_status = sa.Enum(
        "pending", "processing", "processed", "error", "skipped", name="drive_file_status"
    )
    media_type = sa.Enum("image", "video", "pdf", name="media_type")
    cluster_status = sa.Enum("unknown", "named", "ignored", name="cluster_status")

    bind = op.get_bind()
    drive_file_status.create(bind, checkfirst=True)
    media_type.create(bind, checkfirst=True)
    cluster_status.create(bind, checkfirst=True)

    op.create_table(
        "drive_files",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("modified_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("status", drive_file_status, nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "persons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("representative_face_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "media",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "drive_file_id",
            sa.String(),
            sa.ForeignKey("drive_files.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("type", media_type, nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "face_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("representative_face_id", sa.Integer(), nullable=True),
        sa.Column("status", cluster_status, nullable=False, server_default="unknown"),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("centroid", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "faces",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bbox_x", sa.Float(), nullable=False),
        sa.Column("bbox_y", sa.Float(), nullable=False),
        sa.Column("bbox_width", sa.Float(), nullable=False),
        sa.Column("bbox_height", sa.Float(), nullable=False),
        sa.Column("detection_confidence", sa.Float(), nullable=False),
        sa.Column("frame_timestamp", sa.Float(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "cluster_id", sa.Integer(), sa.ForeignKey("face_clusters.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("thumbnail_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Deferred FKs from persons/face_clusters -> faces (representative face), added after `faces` exists.
    op.create_foreign_key(
        "fk_person_representative_face", "persons", "faces", ["representative_face_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_cluster_representative_face",
        "face_clusters",
        "faces",
        ["representative_face_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "face_embeddings",
        sa.Column("face_id", sa.Integer(), sa.ForeignKey("faces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=False),
    )

    op.create_table(
        "recognitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
        sa.Column("face_id", sa.Integer(), sa.ForeignKey("faces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ocr_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("media_id", "page_number", name="uq_ocr_page_media_page"),
    )

    # Vector similarity indexes (cosine distance) for fast nearest-neighbor search.
    op.execute(
        "CREATE INDEX ix_face_embeddings_embedding_cosine ON face_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_face_clusters_centroid_cosine ON face_clusters "
        "USING hnsw (centroid vector_cosine_ops)"
    )

    # Common lookup indexes.
    op.create_index("ix_faces_media_id", "faces", ["media_id"])
    op.create_index("ix_faces_person_id", "faces", ["person_id"])
    op.create_index("ix_faces_cluster_id", "faces", ["cluster_id"])
    op.create_index("ix_media_drive_file_id", "media", ["drive_file_id"])
    op.create_index("ix_recognitions_person_id", "recognitions", ["person_id"])
    op.create_index("ix_recognitions_media_id", "recognitions", ["media_id"])
    op.create_index("ix_drive_files_status", "drive_files", ["status"])
    op.execute("CREATE INDEX ix_ocr_pages_text_trgm ON ocr_pages USING gin (to_tsvector('english', text))")


def downgrade() -> None:
    op.drop_index("ix_ocr_pages_text_trgm", table_name="ocr_pages")
    op.drop_index("ix_drive_files_status", table_name="drive_files")
    op.drop_index("ix_recognitions_media_id", table_name="recognitions")
    op.drop_index("ix_recognitions_person_id", table_name="recognitions")
    op.drop_index("ix_media_drive_file_id", table_name="media")
    op.drop_index("ix_faces_cluster_id", table_name="faces")
    op.drop_index("ix_faces_person_id", table_name="faces")
    op.drop_index("ix_faces_media_id", table_name="faces")
    op.execute("DROP INDEX IF EXISTS ix_face_clusters_centroid_cosine")
    op.execute("DROP INDEX IF EXISTS ix_face_embeddings_embedding_cosine")

    op.drop_table("ocr_pages")
    op.drop_table("recognitions")
    op.drop_table("face_embeddings")
    op.drop_constraint("fk_cluster_representative_face", "face_clusters", type_="foreignkey")
    op.drop_constraint("fk_person_representative_face", "persons", type_="foreignkey")
    op.drop_table("faces")
    op.drop_table("face_clusters")
    op.drop_table("media")
    op.drop_table("persons")
    op.drop_table("drive_files")

    sa.Enum(name="cluster_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="media_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="drive_file_status").drop(op.get_bind(), checkfirst=True)

    op.execute("DROP EXTENSION IF EXISTS vector")
