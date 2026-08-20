"""Build folder-wise library view from flat Drive file paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.models import DriveFile
from app.drive.indexing_pause import normalize_file_path, normalize_folder_path


@dataclass
class LibraryFileItem:
    id: str
    name: str
    path: str
    folder_path: str
    mime_type: str
    status: str
    size: int | None
    source: str
    is_image: bool
    is_video: bool
    has_caption: bool
    has_embedding: bool
    caption_preview: str | None = None
    error_message: str | None = None


@dataclass
class LibraryFolderNode:
    name: str
    path: str
    file_count: int = 0
    image_count: int = 0
    captioned_count: int = 0
    embedded_count: int = 0
    pending_count: int = 0
    processed_count: int = 0
    processing_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    archived_count: int = 0
    indexing_paused: bool = False
    # Top skip / error reason keys accumulated for this folder subtree.
    skip_reasons: dict[str, int] = field(default_factory=dict)
    error_reasons: dict[str, int] = field(default_factory=dict)
    folders: dict[str, LibraryFolderNode] = field(default_factory=dict)
    files: list[LibraryFileItem] = field(default_factory=list)


def _folder_path(parts: list[str]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def _ancestors(root: LibraryFolderNode, folder_parts: list[str]) -> list[LibraryFolderNode]:
    nodes = [root]
    node = root
    for part in folder_parts:
        node = node.folders[part.lower()]
        nodes.append(node)
    return nodes


def build_library_tree(
    drive_files: list[DriveFile],
    *,
    captioned_ids: set[str],
    embedded_ids: set[str],
    caption_texts: dict[str, str],
    paused_folder_paths: list[str] | None = None,
) -> tuple[LibraryFolderNode, list[LibraryFileItem], dict[str, int]]:
    from app.drive.indexing_pause import is_file_indexing_paused, normalize_folder_path

    paused = [normalize_folder_path(p) for p in (paused_folder_paths or [])]
    paused_set = set(paused)
    root = LibraryFolderNode(name="Library", path="/", indexing_paused="/" in paused_set)
    all_files: list[LibraryFileItem] = []

    for df in drive_files:
        path_parts = [p for p in df.path.replace("\\", "/").split("/") if p]
        from app.drive.library_filters import is_apple_junk_row, is_folder_marker_row

        is_folder_marker = is_folder_marker_row(
            mime_type=df.mime_type, error_message=df.error_message
        )
        # Apple junk never appears in library tree / counts / file lists.
        if is_apple_junk_row(name=df.name, error_message=df.error_message):
            continue

        if is_folder_marker:
            # Path is the folder itself — ensure the node exists even with zero files.
            # Markers are never added to file_count or file lists.
            node = root
            for i, part in enumerate(path_parts):
                key = part.lower()
                if key not in node.folders:
                    sub_path = _folder_path(path_parts[: i + 1])
                    node.folders[key] = LibraryFolderNode(
                        name=part,
                        path=sub_path,
                        indexing_paused=sub_path in paused_set,
                    )
                node = node.folders[key]
            continue

        is_image = df.mime_type.startswith("image/")
        is_video = df.mime_type.startswith("video/")
        has_cap = df.id in captioned_ids and bool((caption_texts.get(df.id) or "").strip())
        has_emb = df.id in embedded_ids
        cap_text = (caption_texts.get(df.id) or "").strip() or None
        preview = (cap_text[:120] + "…") if cap_text and len(cap_text) > 120 else cap_text

        folder_parts = path_parts[:-1] if len(path_parts) > 1 else []
        folder_path = _folder_path(folder_parts)

        item = LibraryFileItem(
            id=df.id,
            name=df.name,
            path=df.path,
            folder_path=folder_path,
            mime_type=df.mime_type,
            status=df.status.value if hasattr(df.status, "value") else str(df.status),
            size=df.size,
            source=df.source or "drive",
            is_image=is_image,
            is_video=is_video,
            has_caption=has_cap,
            has_embedding=has_emb,
            caption_preview=preview,
            error_message=df.error_message,
        )
        all_files.append(item)

        node = root
        for i, part in enumerate(folder_parts):
            key = part.lower()
            if key not in node.folders:
                sub_path = _folder_path(folder_parts[: i + 1])
                node.folders[key] = LibraryFolderNode(
                    name=part,
                    path=sub_path,
                    indexing_paused=sub_path in paused_set,
                )
            node = node.folders[key]

        node.files.append(item)

        for ancestor in _ancestors(root, folder_parts):
            ancestor.file_count += 1
            if is_image:
                ancestor.image_count += 1
            if has_cap:
                ancestor.captioned_count += 1
            if has_emb:
                ancestor.embedded_count += 1
            if item.status == "pending":
                ancestor.pending_count += 1
            if item.status == "processed":
                ancestor.processed_count += 1
            if item.status == "processing":
                ancestor.processing_count += 1
            if item.status == "error":
                ancestor.error_count += 1
            if item.status == "skipped":
                ancestor.skipped_count += 1
            if item.status == "archived":
                ancestor.archived_count += 1

        # Reason tallies (only on the leaf folder — roll up below).
        from app.workers.requeue_failed import normalize_error_bucket, normalize_skip_reason

        if item.status == "skipped":
            key = normalize_skip_reason(item.error_message)
            node.skip_reasons[key] = node.skip_reasons.get(key, 0) + 1
        elif item.status == "error":
            key = normalize_error_bucket(item.error_message)
            node.error_reasons[key] = node.error_reasons.get(key, 0) + 1

    def _rollup_reasons(n: LibraryFolderNode) -> None:
        for child in n.folders.values():
            _rollup_reasons(child)
            for k, v in child.skip_reasons.items():
                n.skip_reasons[k] = n.skip_reasons.get(k, 0) + v
            for k, v in child.error_reasons.items():
                n.error_reasons[k] = n.error_reasons.get(k, 0) + v

    _rollup_reasons(root)

    pending = sum(
        1 for f in all_files
        if f.status == "pending" and not is_file_indexing_paused(f.path, paused)
    )
    errors = sum(1 for f in all_files if f.status == "error")
    # Caption gaps only on already-indexed images — do NOT use (images - captioned),
    # which double-counts every still-pending image into "Needs work".
    from app.drive.content_hash import DUPLICATE_CONTENT_PREFIX

    missing_captions = sum(
        1
        for f in all_files
        if f.is_image
        and f.status == "processed"
        and not f.has_caption
        # Content twins are already searchable via the canonical file — not caption gaps.
        and not (f.error_message or "").startswith(DUPLICATE_CONTENT_PREFIX)
    )
    summary = {
        "total_files": len(all_files),
        "images": sum(1 for f in all_files if f.is_image),
        "videos": sum(1 for f in all_files if f.is_video),
        "captioned": sum(1 for f in all_files if f.is_image and f.has_caption),
        "embedded": sum(1 for f in all_files if f.is_image and f.has_embedding),
        "pending": pending,
        "processed": sum(1 for f in all_files if f.status == "processed"),
        "processing": sum(1 for f in all_files if f.status == "processing"),
        "errors": errors,
        "skipped": sum(1 for f in all_files if f.status == "skipped"),
        "archived": sum(1 for f in all_files if f.status == "archived"),
        "missing_captions": missing_captions,
        # Index queue + failed retries + caption backfill on completed images.
        # Excludes skipped/junk/duplicates and does not double-count pending images.
        "needs_work": pending + errors + missing_captions,
    }
    if summary["images"]:
        summary["caption_pct"] = round(100.0 * summary["captioned"] / summary["images"], 1)
    else:
        summary["caption_pct"] = 0.0

    return root, all_files, summary


def compute_library_revision(drive_files: list[DriveFile]) -> str:
    """Cheap freshness token: file count + max last_synced_at + status histogram."""
    max_synced: datetime | None = None
    status_hist: dict[str, int] = {}
    for df in drive_files:
        key = df.status.value if hasattr(df.status, "value") else str(df.status)
        status_hist[key] = status_hist.get(key, 0) + 1
        if df.last_synced_at is not None and (max_synced is None or df.last_synced_at > max_synced):
            max_synced = df.last_synced_at
    hist_part = ",".join(f"{k}:{status_hist[k]}" for k in sorted(status_hist.keys()))
    return f"{len(drive_files)}:{max_synced.isoformat() if max_synced else ''}:{hist_part}"


def file_folder_path(file_path: str) -> str:
    """Parent folder path for a DriveFile.path (same rules as build_library_tree)."""
    parts = [p for p in normalize_file_path(file_path).split("/") if p]
    folder_parts = parts[:-1] if len(parts) > 1 else []
    return _folder_path(folder_parts)


def is_direct_child_of_folder(file_path: str, folder_path: str) -> bool:
    """True when file is a direct child of folder_path (not nested deeper)."""
    return file_folder_path(file_path) == normalize_folder_path(folder_path)


def build_library_shell(
    drive_files: list[DriveFile],
    *,
    paused_folder_paths: list[str] | None = None,
) -> tuple[LibraryFolderNode, dict[str, Any]]:
    """Folder tree + DB status counts only — no file lists, no Qdrant."""
    root, all_files, summary = build_library_tree(
        drive_files,
        captioned_ids=set(),
        embedded_ids=set(),
        caption_texts={},
        paused_folder_paths=paused_folder_paths,
    )
    # Drop file lists from every node (shell payload).
    def _strip_files(node: LibraryFolderNode) -> None:
        node.files = []
        for child in node.folders.values():
            _strip_files(child)

    _strip_files(root)
    # Caption/embed stats require Qdrant — mark unknown for the client.
    summary["captioned"] = 0
    summary["embedded"] = 0
    summary["missing_captions"] = 0
    summary["caption_pct"] = 0.0
    summary["caption_stats_ready"] = False
    summary["needs_work"] = summary["pending"] + summary["errors"]
    return root, summary


def folder_node_to_shell_dict(node: LibraryFolderNode) -> dict[str, Any]:
    """Serialize folder tree without files[] (and without reason tallies to keep small)."""
    return {
        "name": node.name,
        "path": node.path,
        "file_count": node.file_count,
        "image_count": node.image_count,
        "captioned_count": node.captioned_count,
        "embedded_count": node.embedded_count,
        "pending_count": node.pending_count,
        "processed_count": node.processed_count,
        "processing_count": node.processing_count,
        "error_count": node.error_count,
        "skipped_count": node.skipped_count,
        "archived_count": node.archived_count,
        "indexing_paused": node.indexing_paused,
        "folders": [
            folder_node_to_shell_dict(child)
            for child in sorted(node.folders.values(), key=lambda n: n.name.lower())
        ],
        "files": [],
    }


def thin_file_dict(
    df: DriveFile,
    *,
    has_caption: bool = False,
    has_embedding: bool = False,
) -> dict[str, Any]:
    """List-row file payload — no caption_preview; short error only for failed/skipped."""
    is_image = df.mime_type.startswith("image/")
    is_video = df.mime_type.startswith("video/")
    folder_path = file_folder_path(df.path)
    status = df.status.value if hasattr(df.status, "value") else str(df.status)
    err: str | None = None
    if status in ("error", "skipped") and df.error_message:
        msg = df.error_message.strip()
        err = (msg[:160] + "…") if len(msg) > 160 else msg
    return {
        "id": df.id,
        "name": df.name,
        "path": df.path,
        "folder_path": folder_path,
        "mime_type": df.mime_type,
        "status": status,
        "size": df.size,
        "source": df.source or "drive",
        "is_image": is_image,
        "is_video": is_video,
        "has_caption": has_caption,
        "has_embedding": has_embedding,
        "caption_preview": None,
        "error_message": err,
    }


def folder_node_to_dict(node: LibraryFolderNode) -> dict:
    top_skips = sorted(node.skip_reasons.items(), key=lambda kv: -kv[1])[:8]
    top_errors = sorted(node.error_reasons.items(), key=lambda kv: -kv[1])[:8]
    return {
        "name": node.name,
        "path": node.path,
        "file_count": node.file_count,
        "image_count": node.image_count,
        "captioned_count": node.captioned_count,
        "embedded_count": node.embedded_count,
        "pending_count": node.pending_count,
        "processed_count": node.processed_count,
        "processing_count": node.processing_count,
        "error_count": node.error_count,
        "skipped_count": node.skipped_count,
        "archived_count": node.archived_count,
        "indexing_paused": node.indexing_paused,
        "top_skip_reasons": [{"reason": k, "count": v} for k, v in top_skips],
        "top_error_reasons": [{"reason": k, "count": v} for k, v in top_errors],
        "folders": [folder_node_to_dict(child) for child in sorted(node.folders.values(), key=lambda n: n.name.lower())],
        "files": [
            {
                "id": f.id,
                "name": f.name,
                "path": f.path,
                "folder_path": f.folder_path,
                "mime_type": f.mime_type,
                "status": f.status,
                "size": f.size,
                "source": f.source,
                "is_image": f.is_image,
                "is_video": f.is_video,
                "has_caption": f.has_caption,
                "has_embedding": f.has_embedding,
                "caption_preview": f.caption_preview,
                "error_message": f.error_message,
            }
            for f in sorted(node.files, key=lambda x: x.name.lower())
        ],
    }
