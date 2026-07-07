"""Video pipeline: ffmpeg frames + optional VTT; no Fennec Docker."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.config import Settings
from app.db.models import DriveFile, DriveFileStatus, MediaType
from app.pipelines.video import process_video_file
from tests.conftest import requires_postgres

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "faces"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not found on PATH",
)


class _LocalFileDriveClient:
    def __init__(self, path: Path) -> None:
        self._path = path

    @asynccontextmanager
    async def stream_file_content(self, file_id: str):
        yield _FileResponse(self._path.read_bytes())


class _FileResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def aiter_bytes(self, chunk_size: int = 1024 * 256):
        yield self._content


def _build_tiny_video(tmp_path: Path) -> Path:
    out_path = tmp_path / "clip.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", "2", "-i", str(FIXTURES_DIR / "person_a_1.jpg"),
        "-vf", "scale=250:250,fps=1",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    return out_path


@requires_postgres
@requires_ffmpeg
@pytest.mark.asyncio
async def test_video_pipeline_caches_for_fennec(db_session, tmp_path):
    if not (FIXTURES_DIR / "person_a_1.jpg").exists():
        pytest.skip("face fixture images not present")

    video_path = _build_tiny_video(tmp_path)
    cache_dir = tmp_path / "video-cache"

    drive_file = DriveFile(
        id=f"drive-{uuid.uuid4().hex}",
        name="clip.mp4",
        mime_type="video/mp4",
        path="/clip.mp4",
        status=DriveFileStatus.PROCESSING,
    )
    db_session.add(drive_file)
    await db_session.flush()

    settings = Settings(
        thumbnail_dir=str(tmp_path / "thumbnails"),
        temp_dir=str(tmp_path / "tmp"),
        video_cache_dir=str(cache_dir),
        video_vlm_enrich=False,
        gemini_api_key="",
    )
    client = _LocalFileDriveClient(video_path)

    result = await process_video_file(db_session, drive_file, client, settings)
    await db_session.commit()
    media = result.media

    assert media.type == MediaType.VIDEO
    cached = list(cache_dir.glob(f"{drive_file.id}.*"))
    assert len(cached) == 1
    assert cached[0].stat().st_size > 0

    frames = list((tmp_path / "thumbnails" / "video" / drive_file.id).glob("*.jpg"))
    assert len(frames) >= 1
