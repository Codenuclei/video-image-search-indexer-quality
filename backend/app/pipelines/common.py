from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Media
from app.drive.client import DriveConnectorClient

INDEXABLE_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/avif", "image/heic", "image/heif"})
INDEXABLE_VIDEO_TYPES = frozenset(
    {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
)
INDEXABLE_TYPES = INDEXABLE_IMAGE_TYPES | frozenset(
    {"application/pdf", "text/plain", "text/markdown", "text/csv"}
)


def is_video_mime(mime_type: str) -> bool:
    return mime_type in INDEXABLE_VIDEO_TYPES or mime_type.startswith("video/")


def is_image_mime(mime_type: str) -> bool:
    return mime_type in INDEXABLE_IMAGE_TYPES or mime_type.startswith("image/")


def is_indexable_mime(mime_type: str) -> bool:
    if is_video_mime(mime_type):
        return get_settings().video_indexing_enabled
    return mime_type in INDEXABLE_TYPES or is_image_mime(mime_type)


def save_face_thumbnail(face_id: int, jpeg_bytes: bytes, settings: Settings) -> str | None:
    if not jpeg_bytes:
        return None
    os.makedirs(settings.thumbnail_dir, exist_ok=True)
    path = os.path.join(settings.thumbnail_dir, f"{face_id}.jpg")
    with open(path, "wb") as fh:
        fh.write(jpeg_bytes)
    return path


async def clear_existing_media(session: AsyncSession, drive_file_id: str) -> None:
    existing = (await session.execute(select(Media).where(Media.drive_file_id == drive_file_id))).scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
        await session.flush()


async def file_has_media(session: AsyncSession, drive_file_id: str) -> bool:
    stmt = select(Media.id).where(Media.drive_file_id == drive_file_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def download_to_memory(client: DriveConnectorClient, file_id: str) -> bytes:
    chunks: list[bytes] = []
    async with client.stream_file_content(file_id) as response:
        async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
            chunks.append(chunk)
    return b"".join(chunks)


def decode_image_bgr(raw_bytes: bytes) -> "np.ndarray":
    """Decode image bytes to OpenCV BGR. Falls back to Pillow for AVIF/HEIC."""
    import io

    import cv2
    import numpy as np
    from PIL import Image

    image_bgr = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image_bgr is not None:
        return image_bgr

    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Could not decode image bytes") from exc

    rgb = np.array(img)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def needs_jpeg_normalization(mime_type: str, file_name: str) -> bool:
    mime = (mime_type or "").lower()
    if mime in ("image/avif", "image/heic", "image/heif"):
        return True
    lower = file_name.lower()
    return lower.endswith((".avif", ".heic", ".heif"))


def write_jpeg_file(raw_bytes: bytes, dest_path: str) -> None:
    """Convert arbitrary image bytes to JPEG on disk (for Gemini upload)."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    img.save(dest_path, format="JPEG", quality=92)


@asynccontextmanager
async def download_image_for_upload(
    client: DriveConnectorClient,
    file_id: str,
    settings: Settings,
    *,
    mime_type: str,
    file_name: str,
) -> AsyncIterator[tuple[str, str]]:
    """Download an image; normalize AVIF/HEIC to JPEG for downstream upload."""
    raw_bytes = await download_to_memory(client, file_id)
    if needs_jpeg_normalization(mime_type, file_name):
        os.makedirs(settings.temp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=settings.temp_dir, prefix=f"{file_id}_", suffix=".jpg")
        os.close(fd)
        try:
            write_jpeg_file(raw_bytes, path)
            yield path, "image/jpeg"
        finally:
            if os.path.exists(path):
                os.remove(path)
    else:
        suffix = ""
        if "." in file_name:
            suffix = file_name[file_name.rindex(".") :]
        async with download_to_temp_file(client, file_id, settings, suffix=suffix) as path:
            yield path, mime_type


@asynccontextmanager
async def download_to_temp_file(
    client: DriveConnectorClient,
    file_id: str,
    settings: Settings,
    suffix: str = "",
) -> AsyncIterator[str]:
    os.makedirs(settings.temp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=settings.temp_dir, prefix=f"{file_id}_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            async with client.stream_file_content(file_id) as response:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                    fh.write(chunk)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)
