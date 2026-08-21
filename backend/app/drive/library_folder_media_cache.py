"""Cached Qdrant caption/embed presence + per-folder rollup counts for library shell.

Presence is ID-level (only unknown IDs hit Qdrant). Folder rollups are cached and
invalidated when new files land under that folder (and its ancestors).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _folder_ancestors(path: str) -> list[str]:
    """Return path plus every ancestor including '/'."""
    from app.drive.indexing_pause import normalize_folder_path

    p = normalize_folder_path(path)
    out = [p]
    while p != "/":
        parts = [x for x in p.split("/") if x]
        parts = parts[:-1]
        p = "/" + "/".join(parts) if parts else "/"
        out.append(p)
    # de-dupe preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _image_ids_fingerprint(image_ids: Iterable[str]) -> str:
    joined = ",".join(sorted(image_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


@dataclass
class FolderMediaCounts:
    image_count: int
    captioned_count: int
    embedded_count: int
    image_ids_fp: str


@dataclass
class MediaPresenceCache:
    """True/False known presence; missing keys mean 'not resolved yet'."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    captioned: dict[str, bool] = field(default_factory=dict)
    embedded: dict[str, bool] = field(default_factory=dict)

    def resolve_sync(self, image_ids: list[str]) -> tuple[set[str], set[str]]:
        """Return (captioned_ids, embedded_ids); fetch only unknown IDs from Qdrant."""
        if not image_ids:
            return set(), set()

        with self._lock:
            unknown_cap = [i for i in image_ids if i not in self.captioned]
            unknown_emb = [i for i in image_ids if i not in self.embedded]

        if unknown_cap or unknown_emb:
            from app.qdrant.image_captions import valid_caption_ids_sync
            from app.qdrant.images import existing_image_ids_sync

            cap_found: set[str] = set()
            emb_found: set[str] = set()
            try:
                if unknown_cap:
                    cap_found = valid_caption_ids_sync(unknown_cap)
                if unknown_emb:
                    emb_found = existing_image_ids_sync(unknown_emb)
            except Exception:  # noqa: BLE001
                logger.exception("Qdrant media presence resolve failed")
                # Do not poison cache on failure — leave unknowns unresolved.
                unknown_cap = []
                unknown_emb = []

            with self._lock:
                for fid in unknown_cap:
                    self.captioned[fid] = fid in cap_found
                for fid in unknown_emb:
                    self.embedded[fid] = fid in emb_found

        with self._lock:
            caps = {i for i in image_ids if self.captioned.get(i)}
            embs = {i for i in image_ids if self.embedded.get(i)}
        return caps, embs

    def note(self, drive_file_id: str, *, captioned: bool | None = None, embedded: bool | None = None) -> None:
        with self._lock:
            if captioned is not None:
                self.captioned[drive_file_id] = captioned
            if embedded is not None:
                self.embedded[drive_file_id] = embedded

    def invalidate_ids(self, drive_file_ids: Iterable[str]) -> None:
        ids = list(drive_file_ids)
        if not ids:
            return
        with self._lock:
            for fid in ids:
                self.captioned.pop(fid, None)
                self.embedded.pop(fid, None)

    def clear(self) -> None:
        with self._lock:
            self.captioned.clear()
            self.embedded.clear()


@dataclass
class FolderMediaCountsCache:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    by_path: dict[str, FolderMediaCounts] = field(default_factory=dict)

    def get(self, path: str) -> FolderMediaCounts | None:
        from app.drive.indexing_pause import normalize_folder_path

        with self._lock:
            return self.by_path.get(normalize_folder_path(path))

    def put(self, path: str, counts: FolderMediaCounts) -> None:
        from app.drive.indexing_pause import normalize_folder_path

        with self._lock:
            self.by_path[normalize_folder_path(path)] = counts

    def store_tree_counts(self, tree_dict: dict[str, Any], *, image_ids_by_path: dict[str, list[str]]) -> None:
        """Persist rollup counts from a shell tree dict."""

        def walk(node: dict[str, Any]) -> None:
            path = node.get("path") or "/"
            ids = image_ids_by_path.get(path, [])
            self.put(
                path,
                FolderMediaCounts(
                    image_count=int(node.get("image_count") or 0),
                    captioned_count=int(node.get("captioned_count") or 0),
                    embedded_count=int(node.get("embedded_count") or 0),
                    image_ids_fp=_image_ids_fingerprint(ids),
                ),
            )
            for child in node.get("folders") or []:
                walk(child)

        walk(tree_dict)

    def invalidate_folder(self, folder_path: str) -> None:
        """Drop this folder and all ancestors (rollups) and descendants."""
        from app.drive.indexing_pause import normalize_folder_path

        target = normalize_folder_path(folder_path)
        ancestors = set(_folder_ancestors(target))
        with self._lock:
            drop = [
                p
                for p in self.by_path
                if p in ancestors or p == target or p.startswith(target.rstrip("/") + "/")
            ]
            for p in drop:
                self.by_path.pop(p, None)
            if drop:
                logger.debug("Folder media counts invalidated: %s", ", ".join(sorted(drop)[:12]))

    def clear(self) -> None:
        with self._lock:
            self.by_path.clear()


_presence = MediaPresenceCache()
_folder_counts = FolderMediaCountsCache()


def get_media_presence_cache() -> MediaPresenceCache:
    return _presence


def get_folder_media_counts_cache() -> FolderMediaCountsCache:
    return _folder_counts


def note_media_presence(
    drive_file_id: str,
    *,
    captioned: bool | None = None,
    embedded: bool | None = None,
) -> None:
    _presence.note(drive_file_id, captioned=captioned, embedded=embedded)
    try:
        from app.drive.library_shell_cache import get_library_shell_cache

        get_library_shell_cache().invalidate()
    except Exception:  # noqa: BLE001
        pass


def invalidate_folder_media_cache(
    folder_path: str,
    *,
    drive_file_ids: Iterable[str] | None = None,
) -> None:
    """New files (or caption/embed changes) under a folder — drop that folder's counts."""
    _folder_counts.invalidate_folder(folder_path)
    if drive_file_ids:
        _presence.invalidate_ids(drive_file_ids)
    try:
        from app.drive.library_shell_cache import get_library_shell_cache

        get_library_shell_cache().invalidate()
    except Exception:  # noqa: BLE001
        pass


def collect_image_ids_by_folder(drive_files: list[Any]) -> dict[str, list[str]]:
    """Map each folder path (and ancestors) to image drive_file_ids under it."""
    from app.drive.indexing_pause import normalize_folder_path
    from app.drive.library_filters import is_apple_junk_row, is_folder_marker_row
    from app.drive.library_tree import file_folder_path

    by_path: dict[str, list[str]] = {}
    for df in drive_files:
        mime = getattr(df, "mime_type", "") or ""
        if not mime.startswith("image/"):
            continue
        name = getattr(df, "name", "") or ""
        err = getattr(df, "error_message", None)
        if is_apple_junk_row(name=name, error_message=err):
            continue
        if is_folder_marker_row(mime_type=mime, error_message=err):
            continue
        folder = file_folder_path(getattr(df, "path", "") or "/")
        for ancestor in _folder_ancestors(folder):
            by_path.setdefault(ancestor, []).append(df.id)
    # Always include root key
    by_path.setdefault("/", by_path.get("/", []))
    return {normalize_folder_path(k): v for k, v in by_path.items()}
