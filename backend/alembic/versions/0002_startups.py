"""Startup / founder indexing tables.

Revision ID: 0002_startups
Revises: 0001_initial_schema
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_startups"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

BODY_EMBEDDING_DIM = 3072


def upgrade() -> None:
    op.create_table(
        "startups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("aliases", JSONB(), nullable=True),
        sa.Column("cohort", sa.Text(), nullable=True),
        sa.Column("sector", sa.Text(), nullable=True),
        sa.Column("one_liner", sa.Text(), nullable=True),
        sa.Column("logo_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_startups_slug", "startups", ["slug"], unique=True)
    op.create_index("ix_startups_name", "startups", ["name"])

    op.create_table(
        "startup_founders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("startup_id", sa.Integer(), sa.ForeignKey("startups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("founder_name", sa.String(length=256), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_candidate", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("startup_id", "founder_name", name="uq_startup_founder_name"),
    )
    op.create_index("ix_startup_founders_startup_id", "startup_founders", ["startup_id"])
    op.create_index("ix_startup_founders_person_id", "startup_founders", ["person_id"])

    op.create_table(
        "startup_face_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("startup_id", sa.Integer(), sa.ForeignKey("startups.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "cluster_id",
            sa.Integer(),
            sa.ForeignKey("face_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_asset", sa.String(length=512), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("startup_id", "cluster_id", name="uq_startup_face_cluster"),
    )
    op.create_index("ix_startup_face_clusters_startup_id", "startup_face_clusters", ["startup_id"])
    op.create_index("ix_startup_face_clusters_cluster_id", "startup_face_clusters", ["cluster_id"])

    op.create_table(
        "startup_logo_refs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("startup_id", sa.Integer(), sa.ForeignKey("startups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_path", sa.String(length=512), nullable=False),
        sa.Column("embedding", Vector(BODY_EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("startup_id", "asset_path", name="uq_startup_logo_ref_asset"),
    )
    op.create_index("ix_startup_logo_refs_startup_id", "startup_logo_refs", ["startup_id"])


def downgrade() -> None:
    op.drop_table("startup_logo_refs")
    op.drop_table("startup_face_clusters")
    op.drop_table("startup_founders")
    op.drop_table("startups")
