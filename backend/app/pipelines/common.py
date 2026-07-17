from __future__ import annotations

import io
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Media
from app.drive.client import DriveConnectorClient

from app.pipelines.image_formats import (
    RAW_EXTENSIONS,
    RECOVERABLE_IMAGE_EXTENSIONS,
    TIFF_EXTENSIONS,
    extension_of,
    infer_image_mime,
    is_raw_filename,
)

logger = logging.getLogger(__name__)

# Formats OpenCV decodes quickly; everything else goes through Pillow (+ plugins).
_OPENCV_NATIVE_MIMES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_NATIVE_UPLOAD_MIMES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_CONVERTIBLE_EXTENSIONS = (
    ".avif",
    ".heic",
    ".heif",
    ".heics",
    ".hif",
    ".bmp",
    ".gif",
    ".jp2",
    ".j2k",
    ".jxl",
) + TIFF_EXTENSIONS + RAW_EXTENSIONS

_MAX_DECODE_PIXELS = 40_000_000
_MAX_TIFF_DIMENSION = 8192

INDEXABLE_IMAGE_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/avif",
        "image/heic",
        "image/heif",
        "image/tiff",
        "image/bmp",
        "image/gif",
        "image/x-tiff",
        "image/jp2",
        "image/jxl",
        "image/x-sony-arw",
        "image/x-canon-cr2",
        "image/x-canon-cr3",
        "image/x-nikon-nef",
        "image/x-nikon-nrw",
        "image/x-adobe-dng",
        "image/x-fuji-raf",
        "image/x-olympus-orf",
        "image/x-panasonic-rw2",
        "image/x-pentax-pef",
        "image/x-samsung-srw",
        "image/x-raw",
    }
)
INDEXABLE_VIDEO_TYPES = frozenset(
    {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"}
)
INDEXABLE_TYPES = INDEXABLE_IMAGE_TYPES | frozenset(
    {"application/pdf", "text/plain", "text/markdown", "text/csv"}
)


def is_video_mime(mime_type: str) -> bool:
    return mime_type in INDEXABLE_VIDEO_TYPES or mime_type.startswith("video/")


def is_image_mime(mime_type: str, file_name: str = "") -> bool:
    resolved = infer_image_mime(mime_type, file_name) if file_name else mime_type
    return resolved in INDEXABLE_IMAGE_TYPES or (
        isinstance(resolved, str) and resolved.startswith("image/")
    )


def is_indexable_mime(mime_type: str, file_name: str = "") -> bool:
    if is_video_mime(mime_type):
        return get_settings().video_indexing_enabled
    if file_name and is_image_mime(mime_type, file_name):
        return True
    return mime_type in INDEXABLE_TYPES or is_image_mime(mime_type)


def save_face_thumbnail(face_id: int, jpeg_bytes: bytes, settings: Settings) -> str | None:
    if not jpeg_bytes:
        return None
    os.makedirs(settings.thumbnail_dir, exist_ok=True)
    path = os.path.join(settings.thumbnail_dir, f"{face_id}.jpg")
    with open(path, "wb") as fh:
        fh.write(jpeg_bytes)
    return path


def save_body_crop_thumbnail(face_id: int, jpeg_bytes: bytes, settings: Settings) -> str | None:
    """Clothing/body crop JPEG for re-id lab previews (`body_{face_id}.jpg`)."""
    if not jpeg_bytes:
        return None
    os.makedirs(settings.thumbnail_dir, exist_ok=True)
    path = os.path.join(settings.thumbnail_dir, f"body_{face_id}.jpg")
    with open(path, "wb") as fh:
        fh.write(jpeg_bytes)
    return path


def body_crop_path(face_id: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return os.path.join(settings.thumbnail_dir, f"body_{face_id}.jpg")


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


_plugins_registered = False


def register_image_plugins() -> None:
    """Register Pillow codecs for HEIC/HEIF (pillow-heif) and AVIF (pillow-avif-plugin)."""
    global _plugins_registered
    if _plugins_registered:
        return

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        logger.warning("pillow-heif not installed — HEIC/HEIF images may fail to decode")

    try:
        import pillow_avif  # noqa: F401
    except ImportError:
        logger.warning("pillow-avif-plugin not installed — AVIF images may fail to decode")

    _plugins_registered = True


def _downscale_if_huge(img) -> None:
    """Avoid OOM on multi-hundred-megapixel TIFF scans."""
    from PIL import Image

    w, h = img.size
    if w * h <= _MAX_DECODE_PIXELS:
        return
    img.thumbnail((_MAX_TIFF_DIMENSION, _MAX_TIFF_DIMENSION), Image.Resampling.LANCZOS)


def _extract_largest_embedded_jpeg(raw_bytes: bytes, *, min_size: int = 40_000) -> bytes | None:
    """Many ARW/CR2/NEF files embed a full-size JPEG preview."""
    best: bytes | None = None
    start = 0
    while True:
        soi = raw_bytes.find(b"\xff\xd8", start)
        if soi == -1:
            break
        eoi = raw_bytes.find(b"\xff\xd9", soi + 2)
        if eoi == -1:
            start = soi + 2
            continue
        chunk = raw_bytes[soi : eoi + 2]
        if len(chunk) >= min_size and (best is None or len(chunk) > len(best)):
            best = chunk
        start = eoi + 2
    return best


def _decode_raw_to_bgr(raw_bytes: bytes, file_name: str = "") -> "np.ndarray":
    """Decode camera RAW via rawpy, falling back to embedded JPEG preview."""
    import cv2
    import numpy as np

    errors: list[str] = []

    try:
        import rawpy

        with rawpy.imread(io.BytesIO(raw_bytes)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=True,
                no_auto_bright=False,
                output_bps=8,
            )
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"rawpy: {exc}")

    embedded = _extract_largest_embedded_jpeg(raw_bytes)
    if embedded:
        try:
            image_bgr = cv2.imdecode(np.frombuffer(embedded, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image_bgr is not None:
                logger.info("Decoded %s via embedded JPEG preview", file_name or "RAW")
                return image_bgr
        except Exception as exc:  # noqa: BLE001
            errors.append(f"embedded-jpeg: {exc}")

    detail = "; ".join(errors)[:500]
    raise ValueError(f"Could not decode RAW image bytes ({detail})")


def _normalize_tiff_array(arr: "np.ndarray") -> "np.ndarray":
    """Convert tifffile output to uint8 RGB HxWx3."""
    import numpy as np

    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    elif arr.ndim == 3 and arr.shape[0] in (3, 4) and arr.shape[0] < arr.shape[-1]:
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim != 3:
        raise ValueError(f"unsupported TIFF shape {arr.shape}")
    if arr.shape[2] >= 4:
        arr = arr[:, :, :3]
    if arr.dtype == np.uint8:
        return arr
    if np.issubdtype(arr.dtype, np.floating):
        peak = float(np.nanmax(arr)) or 1.0
        if peak <= 1.0:
            arr = arr * 255.0
        else:
            arr = arr * (255.0 / peak)
        return np.clip(arr, 0, 255).astype(np.uint8)
    if arr.dtype == np.uint16:
        return (arr / 256).astype(np.uint8)
    peak = int(np.max(arr)) or 1
    return (arr.astype(np.float64) * (255.0 / peak)).astype(np.uint8)


def _decode_tiff_to_rgb(raw_bytes: bytes) -> "np.ndarray":
    """Decode TIFF via tifffile + imagecodecs (handles LZW/JPEG2000/LERC/etc.)."""
    import tifffile

    arr = tifffile.imread(io.BytesIO(raw_bytes))
    return _normalize_tiff_array(arr)


def open_image_rgb(raw_bytes: bytes, *, file_name: str = ""):
    """Open arbitrary image bytes as RGB via Pillow (first frame for GIF/TIFF)."""
    from PIL import Image

    register_image_plugins()
    if is_raw_filename(file_name):
        bgr = _decode_raw_to_bgr(raw_bytes, file_name)
        import cv2

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    ext = extension_of(file_name)
    if ext in TIFF_EXTENSIONS:
        try:
            rgb = _decode_tiff_to_rgb(raw_bytes)
            img = Image.fromarray(rgb)
            _downscale_if_huge(img)
            return img
        except Exception as exc:  # noqa: BLE001
            logger.warning("tifffile decode failed for %s, falling back to Pillow: %s", file_name, exc)

    img = Image.open(io.BytesIO(raw_bytes))
    if getattr(img, "is_animated", False):
        img.seek(0)
    elif getattr(img, "n_frames", 1) > 1:
        try:
            img.seek(0)
        except EOFError:
            pass
    _downscale_if_huge(img)
    return img.convert("RGB")


def bytes_to_jpeg_bytes(raw_bytes: bytes, *, quality: int = 92, file_name: str = "") -> bytes:
    """Convert any decodable image to JPEG bytes."""
    buf = io.BytesIO()
    open_image_rgb(raw_bytes, file_name=file_name).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def decode_image_bgr(raw_bytes: bytes, *, file_name: str = "") -> "np.ndarray":
    """Decode image bytes to OpenCV BGR. Uses rawpy/Pillow for ARW/TIFF and other exotic formats."""
    import cv2
    import numpy as np

    if is_raw_filename(file_name):
        return _decode_raw_to_bgr(raw_bytes, file_name)

    image_bgr = cv2.imdecode(np.frombuffer(raw_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image_bgr is not None:
        return image_bgr

    try:
        rgb = np.array(open_image_rgb(raw_bytes, file_name=file_name))
    except Exception as exc:  # noqa: BLE001
        if extension_of(file_name) in TIFF_EXTENSIONS:
            try:
                rgb = _decode_tiff_to_rgb(raw_bytes)
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            except Exception:
                pass
        if extension_of(file_name) in RAW_EXTENSIONS:
            try:
                return _decode_raw_to_bgr(raw_bytes, file_name)
            except Exception:
                pass
        raise ValueError("Could not decode image bytes") from exc

    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def needs_jpeg_normalization(mime_type: str, file_name: str) -> bool:
    """True when the file should be converted to JPEG before Gemini / file upload."""
    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    if mime in _NATIVE_UPLOAD_MIMES:
        return False
    if mime.startswith("image/"):
        return True
    lower = file_name.lower()
    return lower.endswith(_CONVERTIBLE_EXTENSIONS)


def write_jpeg_file(raw_bytes: bytes, dest_path: str, *, file_name: str = "") -> None:
    """Convert arbitrary image bytes to JPEG on disk (for Gemini upload)."""
    open_image_rgb(raw_bytes, file_name=file_name).save(dest_path, format="JPEG", quality=92)


@asynccontextmanager
async def download_image_for_upload(
    client: DriveConnectorClient,
    file_id: str,
    settings: Settings,
    *,
    mime_type: str,
    file_name: str,
) -> AsyncIterator[tuple[str, str]]:
    """Download an image; convert exotic formats (HEIC/AVIF/TIFF/etc.) to JPEG for upload."""
    raw_bytes = await download_to_memory(client, file_id)
    if needs_jpeg_normalization(mime_type, file_name):
        os.makedirs(settings.temp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=settings.temp_dir, prefix=f"{file_id}_", suffix=".jpg")
        os.close(fd)
        try:
            write_jpeg_file(raw_bytes, path, file_name=file_name)
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
