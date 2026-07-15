"""Extension/MIME helpers and decode recovery for TIFF + camera RAW (ARW, etc.)."""
from __future__ import annotations

# Sony ARW, Canon CR2, Nikon NEF, Adobe DNG, Fuji RAF, etc.
RAW_EXTENSIONS = (
    ".arw",
    ".cr2",
    ".cr3",
    ".nef",
    ".nrw",
    ".dng",
    ".raf",
    ".orf",
    ".rw2",
    ".pef",
    ".srw",
    ".raw",
)

TIFF_EXTENSIONS = (".tif", ".tiff")

RECOVERABLE_IMAGE_EXTENSIONS = TIFF_EXTENSIONS + RAW_EXTENSIONS

_EXTENSION_TO_MIME: dict[str, str] = {
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".arw": "image/x-sony-arw",
    ".cr2": "image/x-canon-cr2",
    ".cr3": "image/x-canon-cr3",
    ".nef": "image/x-nikon-nef",
    ".nrw": "image/x-nikon-nrw",
    ".dng": "image/x-adobe-dng",
    ".raf": "image/x-fuji-raf",
    ".orf": "image/x-olympus-orf",
    ".rw2": "image/x-panasonic-rw2",
    ".pef": "image/x-pentax-pef",
    ".srw": "image/x-samsung-srw",
    ".raw": "image/x-raw",
}

_DECODE_ERROR_MARKERS = (
    "could not decode image bytes",
    "could not decode raw image bytes",
    "cannot identify image file",
    "cannot open image",
    "decoder error",
    "invalid image",
    "unsupported image",
    "heif",
    "heic",
    "avif",
    "tiff",
    "tif",
    "libtiff",
    "arw",
    "raw",
    "cr2",
    "cr3",
    "dng",
    "nef",
    "libraw",
    "compression method",
    "not a TIFF",
)


def extension_of(file_name: str) -> str:
    lower = (file_name or "").lower()
    for ext in sorted(_EXTENSION_TO_MIME, key=len, reverse=True):
        if lower.endswith(ext):
            return ext
    return ""


def infer_image_mime(mime_type: str, file_name: str) -> str:
    """Map octet-stream / missing MIME to a concrete image type from the filename."""
    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    if mime.startswith("image/") and mime not in {"image/octet-stream", "application/octet-stream"}:
        return mime
    ext = extension_of(file_name)
    if ext:
        return _EXTENSION_TO_MIME[ext]
    return mime_type or ""


def is_raw_filename(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return lower.endswith(RAW_EXTENSIONS)


def is_tiff_filename(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return lower.endswith(TIFF_EXTENSIONS)


def is_recoverable_image_extension(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return lower.endswith(RECOVERABLE_IMAGE_EXTENSIONS)


def error_suggests_decode_failure(error_message: str | None) -> bool:
    msg = (error_message or "").lower()
    return any(marker in msg for marker in _DECODE_ERROR_MARKERS)
