"""Build folder-wise library view from flat Drive file paths."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.db.models import DriveFile


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
    error_count: int = 0
    skipped_count: int = 0
    indexing_paused: bool = False
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
        is_image = df.mime_type.startswith("image/")
        is_video = df.mime_type.startswith("video/")
        has_cap = df.id in captioned_ids and bool((caption_texts.get(df.id) or "").strip())
        has_emb = df.id in embedded_ids
        cap_text = (caption_texts.get(df.id) or "").strip() or None
        preview = (cap_text[:120] + "…") if cap_text and len(cap_text) > 120 else cap_text

        path_parts = [p for p in df.path.replace("\\", "/").split("/") if p]
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
            if item.status == "error":
                ancestor.error_count += 1
            if item.status == "skipped":
                ancestor.skipped_count += 1

    summary = {
        "total_files": len(all_files),
        "images": sum(1 for f in all_files if f.is_image),
        "videos": sum(1 for f in all_files if f.is_video),
        "captioned": sum(1 for f in all_files if f.is_image and f.has_caption),
        "embedded": sum(1 for f in all_files if f.is_image and f.has_embedding),
        "pending": sum(
            1 for f in all_files
            if f.status == "pending" and not is_file_indexing_paused(f.path, paused)
        ),
        "errors": sum(1 for f in all_files if f.status == "error"),
        "skipped": sum(1 for f in all_files if f.status == "skipped"),
    }
    if summary["images"]:
        summary["caption_pct"] = round(100.0 * summary["captioned"] / summary["images"], 1)
    else:
        summary["caption_pct"] = 0.0

    return root, all_files, summary


def folder_node_to_dict(node: LibraryFolderNode) -> dict:
    return {
        "name": node.name,
        "path": node.path,
        "file_count": node.file_count,
        "image_count": node.image_count,
        "captioned_count": node.captioned_count,
        "embedded_count": node.embedded_count,
        "pending_count": node.pending_count,
        "error_count": node.error_count,
        "skipped_count": node.skipped_count,
        "indexing_paused": node.indexing_paused,
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
