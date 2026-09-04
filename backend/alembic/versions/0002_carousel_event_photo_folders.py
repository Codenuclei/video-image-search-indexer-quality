"""add per-video carousel event photo folder links

Revision ID: 0002_carousel_event_photo_folders
Revises: 0001_initial_schema
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_carousel_event_photo_folders"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "carousel_event_photo_folders",
        sa.Column(
            "video_drive_file_id",
            sa.String(),
            sa.ForeignKey("drive_files.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "folder_id",
            sa.String(),
            sa.ForeignKey("indexed_folders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("folder_name", sa.String(), nullable=True),
        sa.Column(
            "indexing_state",
            sa.String(length=32),
            nullable=False,
            server_default="linked",
        ),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_carousel_event_photo_folders_folder_id",
        "carousel_event_photo_folders",
        ["folder_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_carousel_event_photo_folders_folder_id",
        table_name="carousel_event_photo_folders",
    )
    op.drop_table("carousel_event_photo_folders")
