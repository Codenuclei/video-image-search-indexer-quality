"""Guard Rails: Postgres enums must use member values, not Python names."""

from __future__ import annotations

from app.db.models import ClusterStatus, DriveFile, DriveFileStatus, Media, MediaType, _enum_values


def test_enum_values_helper_uses_lowercase_labels() -> None:
    assert _enum_values(DriveFileStatus) == [
        "pending",
        "processing",
        "processed",
        "error",
        "skipped",
        "archived",
    ]
    assert "ARCHIVED" not in _enum_values(DriveFileStatus)
    assert _enum_values(MediaType) == ["image", "video", "pdf"]
    assert _enum_values(ClusterStatus) == ["unknown", "named", "ignored"]


def test_mapped_enums_bind_values_callable() -> None:
    for table, col_name, expected in (
        (DriveFile.__table__, "status", "archived"),
        (Media.__table__, "type", "image"),
    ):
        enum_type = table.c[col_name].type
        assert expected in list(enum_type.enums)
        assert expected.upper() not in list(enum_type.enums) or expected == expected.upper()
