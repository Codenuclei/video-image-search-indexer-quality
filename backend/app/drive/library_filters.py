"""Rows that must never count as library files, enter the index queue, or appear in UI stats.

AppleDouble / .DS_Store junk and Drive folder markers are structural noise — block at
sync and filter from every aggregate / listing.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, not_, or_
from sqlalchemy.sql import ColumnElement

from app.drive.content_hash import APPLEDOUBLE_SKIP_PREFIX, is_macos_junk_name

FOLDER_MARKER_ERROR = "folder_marker"
FOLDER_MIME = "application/vnd.google-apps.folder"


def is_folder_marker_row(
    *,
    mime_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    if (error_message or "") == FOLDER_MARKER_ERROR:
        return True
    return (mime_type or "") == FOLDER_MIME


def is_apple_junk_row(
    *,
    name: str | None = None,
    error_message: str | None = None,
) -> bool:
    if is_macos_junk_name(name):
        return True
    msg = error_message or ""
    return msg.startswith(APPLEDOUBLE_SKIP_PREFIX) or msg.startswith("appledouble_junk")


def is_blocked_library_row(obj: Any) -> bool:
    """True for folder markers and Apple junk — never count / queue / list as files."""
    name = getattr(obj, "name", None)
    mime_type = getattr(obj, "mime_type", None)
    error_message = getattr(obj, "error_message", None)
    if isinstance(obj, dict):
        name = obj.get("name")
        mime_type = obj.get("mime_type")
        error_message = obj.get("error_message")
    return is_folder_marker_row(mime_type=mime_type, error_message=error_message) or is_apple_junk_row(
        name=name, error_message=error_message
    )


def sql_exclude_blocked_drive_files(model: Any) -> ColumnElement[bool]:
    """SQLAlchemy predicate: real media rows only (excludes folder markers + Apple junk).

    NULL-safe: ``NOT (col LIKE …)`` alone excludes rows where ``error_message`` is NULL
    (SQL three-valued logic), which would hide nearly all PROCESSED files from counts.
    """
    name = model.name
    mime = model.mime_type
    err = model.error_message
    return and_(
        or_(err.is_(None), err != FOLDER_MARKER_ERROR),
        or_(mime.is_(None), mime != FOLDER_MIME),
        or_(name.is_(None), not_(name.like("._%"))),
        or_(name.is_(None), name != ".DS_Store"),
        or_(name.is_(None), not_(name.like(".DS_Store%"))),
        or_(err.is_(None), not_(err.like(f"{APPLEDOUBLE_SKIP_PREFIX}%"))),
        or_(err.is_(None), not_(err.like("appledouble_junk%"))),
    )
