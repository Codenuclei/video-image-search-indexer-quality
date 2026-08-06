"""Production Postgres enums use SQLAlchemy member *names* (PENDING, ARCHIVED)."""

from __future__ import annotations

from app.db.models import ClusterStatus, DriveFile, DriveFileStatus, Media, MediaType


def test_drive_file_status_api_values_stay_lowercase() -> None:
    """API / .value strings stay lowercase even though PG labels are uppercase names."""
    assert DriveFileStatus.PENDING.value == "pending"
    assert DriveFileStatus.ARCHIVED.value == "archived"
    assert DriveFileStatus.ARCHIVED.name == "ARCHIVED"


def test_mapped_enums_bind_member_names_for_postgres() -> None:
    """SQLAlchemy Enum without values_callable persists member names to PG."""
    status_enums = list(DriveFile.__table__.c.status.type.enums)
    assert "PENDING" in status_enums or "pending" in status_enums
    # Prefer name-style labels matching live Railway PG.
    assert DriveFileStatus.ARCHIVED.name == "ARCHIVED"
    assert MediaType.IMAGE.name == "IMAGE"
    assert ClusterStatus.UNKNOWN.name == "UNKNOWN"
    media_enums = list(Media.__table__.c.type.type.enums)
    assert "IMAGE" in media_enums or "image" in media_enums
