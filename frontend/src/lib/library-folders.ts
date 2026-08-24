import type { LibraryFolder } from "@/lib/api";

export type LibraryFolderOption = {
  value: string;
  label: string;
};

/** Same lookup used by Indexed Folders (`/test/folders`). */
export function findLibraryFolder(
  node: LibraryFolder,
  path: string
): LibraryFolder | null {
  if (node.path === path) return node;
  for (const child of node.folders) {
    const hit = findLibraryFolder(child, path);
    if (hit) return hit;
  }
  return null;
}

/**
 * Subfolders shown in the Indexed Folders grid for a path.
 * At "/" this is `tree.folders` — the top-level folder tiles only.
 */
export function librarySubfoldersAtPath(
  tree: LibraryFolder | null | undefined,
  path: string
): LibraryFolder[] {
  if (!tree) return [];
  if (path === "/") return tree.folders ?? [];
  const current = findLibraryFolder(tree, path);
  return current?.folders ?? [];
}

/** Search folder filter options — same folders as the Indexed Folders home grid. */
export function indexedFolderPickerOptions(
  tree: LibraryFolder | null | undefined
): LibraryFolderOption[] {
  return librarySubfoldersAtPath(tree, "/").map((folder) => ({
    value: folder.path,
    label: folder.name,
  }));
}
