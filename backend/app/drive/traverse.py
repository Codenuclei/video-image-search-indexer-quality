"""Plan BFS traversal for Drive folder trees, including folder shortcuts."""
from __future__ import annotations

from dataclasses import dataclass

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


@dataclass(frozen=True)
class ChildTraversal:
    """How a Drive child should be handled during recursive listing."""

    include_in_listing: bool
    traverse_folder_id: str | None
    path_segment: str


def plan_child_traversal(child: dict, *, follow_shortcuts: bool) -> ChildTraversal:
    """Decide whether to list and/or descend into a Drive child."""
    name = child.get("name") or ""
    mime = child.get("mimeType") or ""

    if mime == FOLDER_MIME:
        return ChildTraversal(
            include_in_listing=True,
            traverse_folder_id=child.get("id"),
            path_segment=name,
        )

    if mime == SHORTCUT_MIME:
        if not follow_shortcuts:
            return ChildTraversal(include_in_listing=False, traverse_folder_id=None, path_segment=name)
        details = child.get("shortcutDetails") or {}
        target_id = details.get("targetId")
        target_mime = details.get("targetMimeType") or ""
        if target_id and target_mime == FOLDER_MIME:
            return ChildTraversal(
                include_in_listing=False,
                traverse_folder_id=target_id,
                path_segment=name,
            )
        return ChildTraversal(include_in_listing=False, traverse_folder_id=None, path_segment=name)

    return ChildTraversal(include_in_listing=True, traverse_folder_id=None, path_segment=name)
