from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectorFolder(BaseModel):
    id: str
    name: str


class ConnectorFile(BaseModel):
    id: str
    name: str
    mime_type: str = Field(alias="mimeType")
    is_folder: bool = Field(alias="isFolder")
    size: str | None = None
    modified_time: datetime | None = Field(default=None, alias="modifiedTime")
    parent_id: str = Field(alias="parentId")
    path: str
    # Google Drive binary checksum when available (not present for Google Docs types).
    md5_checksum: str | None = Field(default=None, alias="md5Checksum")
    sha1_checksum: str | None = Field(default=None, alias="sha1Checksum")

    model_config = {"populate_by_name": True}

    @property
    def size_bytes(self) -> int | None:
        return int(self.size) if self.size else None


class ConnectorFolderListing(BaseModel):
    folder: ConnectorFolder
    files: list[ConnectorFile]
    truncated: bool = False
