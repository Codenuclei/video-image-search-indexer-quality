"use client";

import { FoldersPage } from "@/components/views/folders-view";
import { TestIndexStatus } from "@/components/test-index-status";

export default function TestIndexedFoldersPage() {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs text-muted-foreground">Library · Indexed Folders</p>
          <h1 className="text-2xl font-semibold tracking-tight">Indexed Folders</h1>
        </div>
        <div className="w-full max-w-sm">
          <TestIndexStatus compact />
        </div>
      </div>
      <FoldersPage embedded indexedLayout="cards" />
    </div>
  );
}
