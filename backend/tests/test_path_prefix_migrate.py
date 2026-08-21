"""Unit tests for connected-folder path prefix helpers."""
from __future__ import annotations

from app.drive.path_prefix_migrate import (
    apply_path_prefix,
    connected_folder_prefix,
    path_needs_prefix,
)


def test_connected_folder_prefix() -> None:
    assert connected_folder_prefix("Carousal Videos") == "/Carousal Videos"
    assert connected_folder_prefix(" /Album/ ") == "/Album"
    assert connected_folder_prefix("") is None
    assert connected_folder_prefix(None) is None


def test_apply_path_prefix_idempotent() -> None:
    prefix = "/Carousal Videos"
    assert apply_path_prefix("file.jpg", prefix) == "/Carousal Videos/file.jpg"
    assert apply_path_prefix("/sub/a.jpg", prefix) == "/Carousal Videos/sub/a.jpg"
    assert apply_path_prefix("/", prefix) == "/Carousal Videos"
    assert apply_path_prefix("/Carousal Videos/x.jpg", prefix) == "/Carousal Videos/x.jpg"
    assert path_needs_prefix("/Carousal Videos/x.jpg", prefix) is False
    assert path_needs_prefix("/x.jpg", prefix) is True
