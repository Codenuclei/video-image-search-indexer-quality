"""durable versioned Qwen enrichment

Revision ID: 0002_qwen_enrichment
Revises: 0001_initial_schema
Create Date: 2026-09-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_qwen_enrichment"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qwen_enrichment_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_key", sa.String(96), nullable=False),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE")),
        sa.Column(
            "video_segment_id",
            sa.Integer(),
            sa.ForeignKey("video_segments.id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_version", sa.String(96), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("raw_response", postgresql.JSONB()),
        sa.Column("normalized_result", postgresql.JSONB()),
        sa.Column("error_message", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "target_key",
            "prompt_version",
            "model_version",
            name="uq_qwen_enrichment_target_prompt_model",
        ),
    )
    op.create_index(
        "ix_qwen_enrichment_jobs_status_created",
        "qwen_enrichment_jobs",
        ["status", "created_at"],
    )
    op.create_index("ix_qwen_enrichment_jobs_media_id", "qwen_enrichment_jobs", ["media_id"])
    op.create_index(
        "ix_qwen_enrichment_jobs_video_segment_id",
        "qwen_enrichment_jobs",
        ["video_segment_id"],
    )

    op.create_table(
        "qwen_captions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_key", sa.String(96), nullable=False),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE")),
        sa.Column(
            "video_segment_id",
            sa.Integer(),
            sa.ForeignKey("video_segments.id", ondelete="CASCADE"),
        ),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(96), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "target_key",
            "prompt_version",
            "model_version",
            name="uq_qwen_caption_target_prompt_model",
        ),
    )
    op.create_index("ix_qwen_captions_target_key", "qwen_captions", ["target_key"])
    op.create_index("ix_qwen_captions_media_id", "qwen_captions", ["media_id"])
    op.create_index("ix_qwen_captions_video_segment_id", "qwen_captions", ["video_segment_id"])

    op.create_table(
        "video_segment_labels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "video_segment_id",
            sa.Integer(),
            sa.ForeignKey("video_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_label", sa.String(96), nullable=False),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_text", sa.String(240)),
        sa.Column("prompt_version", sa.String(96), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "video_segment_id",
            "canonical_label",
            "prompt_version",
            "model_version",
            name="uq_video_segment_label_versioned",
        ),
    )
    op.create_index(
        "ix_video_segment_labels_video_segment_id",
        "video_segment_labels",
        ["video_segment_id"],
    )
    op.create_index(
        "ix_video_segment_labels_label",
        "video_segment_labels",
        ["canonical_label", "confidence"],
    )


def downgrade() -> None:
    op.drop_table("video_segment_labels")
    op.drop_table("qwen_captions")
    op.drop_table("qwen_enrichment_jobs")
