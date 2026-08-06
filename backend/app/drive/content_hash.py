"""Content identity for Drive files (md5Checksum / sha1 / local sha256)."""
from __future__ import annotations

import hashlib
from typing import Any

from app.drive.schemas import ConnectorFile

# Skip-reason prefixes written to drive_files.error_message.
DUPLICATE_CONTENT_PREFIX = "duplicate_content:"
NAME_CONFLICT_PREFIX = "name_conflict:"
APPLEDOUBLE_SKIP_PREFIX = "appledouble_junk:"


def is_macos_junk_name(name: str | None) -> bool:
    """True for AppleDouble resource forks / .DS_Store that cannot be decoded as media."""
    base = (name or "").rsplit("/", 1)[-1]
    return base.startswith("._") or base == ".DS_Store" or base.startswith(".DS_Store")


def drive_url_for_folder(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def drive_url_for_file(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def hash_from_connector_entry(entry: ConnectorFile) -> tuple[str, str] | None:
    """Return (algo, hex_digest) from Drive listing checksums when present."""
    md5 = (entry.md5_checksum or "").strip().lower()
    if md5:
        return "md5", md5
    sha1 = (entry.sha1_checksum or "").strip().lower()
    if sha1:
        return "sha1", sha1
    return None


def hash_from_drive_meta(meta: dict[str, Any]) -> tuple[str, str] | None:
    md5 = (meta.get("md5Checksum") or "").strip().lower()
    if md5:
        return "md5", md5
    sha1 = (meta.get("sha1Checksum") or "").strip().lower()
    if sha1:
        return "sha1", sha1
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, *, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def content_identity_key(algo: str | None, digest: str | None) -> str | None:
    if not algo or not digest:
        return None
    return f"{algo}:{digest.strip().lower()}"
