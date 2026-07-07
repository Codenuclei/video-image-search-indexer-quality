from app.drive.schemas import ConnectorFile, ConnectorFolderListing


def test_connector_file_parses_camel_case_aliases_from_connector_api():
    raw = {
        "id": "file123",
        "name": "photo.jpg",
        "mimeType": "image/jpeg",
        "isFolder": False,
        "size": "204800",
        "modifiedTime": "2026-01-15T10:30:00Z",
        "parentId": "folder456",
        "path": "/Vacation/photo.jpg",
    }
    parsed = ConnectorFile.model_validate(raw)
    assert parsed.mime_type == "image/jpeg"
    assert parsed.is_folder is False
    assert parsed.size_bytes == 204800
    assert parsed.path == "/Vacation/photo.jpg"


def test_connector_file_size_bytes_none_when_missing():
    raw = {
        "id": "folder456",
        "name": "Vacation",
        "mimeType": "application/vnd.google-apps.folder",
        "isFolder": True,
        "parentId": "root",
        "path": "/Vacation",
    }
    parsed = ConnectorFile.model_validate(raw)
    assert parsed.size_bytes is None


def test_connector_folder_listing_parses_nested_files():
    raw = {
        "folder": {"id": "root", "name": "shared"},
        "files": [
            {
                "id": "f1",
                "name": "a.png",
                "mimeType": "image/png",
                "isFolder": False,
                "parentId": "root",
                "path": "/a.png",
            }
        ],
        "truncated": False,
    }
    listing = ConnectorFolderListing.model_validate(raw)
    assert listing.folder.name == "shared"
    assert len(listing.files) == 1
    assert listing.files[0].id == "f1"
