import pytest

from app.config import Settings
from app.drive.client import DriveConnectorClient, DriveConnectorError

BASE_URL = "http://connector.test"


def _client() -> DriveConnectorClient:
    settings = Settings(drive_connector_base_url=BASE_URL, drive_connector_api_key="test-key")
    return DriveConnectorClient(settings=settings)


@pytest.mark.asyncio
async def test_list_folder_files_sends_bearer_token_and_parses_response(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/api/folder/files",
        json={
            "folder": {"id": "root", "name": "shared"},
            "files": [
                {
                    "id": "f1",
                    "name": "a.jpg",
                    "mimeType": "image/jpeg",
                    "isFolder": False,
                    "parentId": "root",
                    "path": "/a.jpg",
                }
            ],
            "truncated": False,
        },
    )

    listing = await _client().list_folder_files()

    assert listing.folder.name == "shared"
    assert listing.files[0].id == "f1"

    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_list_folder_files_raises_on_401(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/api/folder/files", status_code=401)

    with pytest.raises(DriveConnectorError, match="401"):
        await _client().list_folder_files()


@pytest.mark.asyncio
async def test_stream_file_content_yields_bytes(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/api/files/f1/content", content=b"hello world")

    chunks = []
    async with _client().stream_file_content("f1") as response:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)

    assert b"".join(chunks) == b"hello world"


@pytest.mark.asyncio
async def test_stream_file_content_raises_on_error_status(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/api/files/missing/content", status_code=404, content=b"not found")

    with pytest.raises(DriveConnectorError):
        async with _client().stream_file_content("missing"):
            pass
