from app.db.models import DriveFile, DriveFileStatus
from app.pipelines.decode_recovery import (
    DECODE_EXHAUSTED_PREFIX,
    apply_decode_failure,
    decode_max_attempts,
    is_decode_exhausted,
    is_decode_recoverable,
)
from app.pipelines.image_formats import infer_image_mime


def _file(**kwargs) -> DriveFile:
    defaults = {
        "id": "abc123",
        "name": "photo.jpg",
        "mime_type": "image/jpeg",
        "path": "folder/photo.jpg",
        "status": DriveFileStatus.ERROR,
        "error_message": "Could not decode image bytes",
        "decode_attempts": 0,
    }
    defaults.update(kwargs)
    return DriveFile(**defaults)


def test_heic_decode_error_is_recoverable():
    df = _file(name="P2.HEIC", mime_type="image/heif", path="Section/P2.HEIC")
    assert is_decode_recoverable(df) is True


def test_arw_decode_error_is_recoverable():
    df = _file(
        name="DSC001.ARW",
        mime_type="application/octet-stream",
        error_message="Could not decode image bytes",
    )
    assert is_decode_recoverable(df) is True


def test_tiff_decode_error_is_recoverable():
    df = _file(name="scan.TIF", mime_type="image/tiff", error_message="cannot identify image file")
    assert is_decode_recoverable(df) is True


def test_cr3_decode_error_is_recoverable_when_under_retry_limit():
    df = _file(name="IMG_0001.CR3", mime_type="image/x-canon-cr3")
    assert is_decode_recoverable(df) is True


def test_arw_skipped_unsupported_mime_is_recoverable():
    df = _file(
        name="photo.ARW",
        mime_type="application/octet-stream",
        status=DriveFileStatus.SKIPPED,
        error_message="Unsupported mime type for indexing: application/octet-stream",
    )
    assert is_decode_recoverable(df) is True


def test_infer_arw_mime_from_extension():
    assert infer_image_mime("application/octet-stream", "IMG_0001.ARW") == "image/x-sony-arw"


def test_infer_tiff_mime_from_extension():
    assert infer_image_mime("", "archive.TIFF") == "image/tiff"


def test_processed_file_not_recoverable():
    df = _file(status=DriveFileStatus.PROCESSED, error_message=None)
    assert is_decode_recoverable(df) is False


def test_non_decode_video_error_not_recoverable():
    df = _file(mime_type="video/mp4", name="clip.mp4", error_message="ffmpeg failed")
    assert is_decode_recoverable(df) is False


def test_avif_with_decode_marker_is_recoverable():
    df = _file(name="photo.avif", mime_type="image/avif", error_message="cannot identify image file")
    assert is_decode_recoverable(df) is True


def test_exhausted_decode_not_recoverable():
    df = _file(
        name="scan.TIF",
        decode_attempts=decode_max_attempts(),
        error_message=f"{DECODE_EXHAUSTED_PREFIX} gave up",
        status=DriveFileStatus.SKIPPED,
    )
    assert is_decode_exhausted(df) is True
    assert is_decode_recoverable(df) is False


def test_apply_decode_failure_skips_after_max_attempts():
    limit = decode_max_attempts()
    df = _file(name="broken.ARW", decode_attempts=limit - 1)
    status = apply_decode_failure(df, "Could not decode image bytes")
    assert status == DriveFileStatus.SKIPPED
    assert df.decode_attempts == limit
    assert DECODE_EXHAUSTED_PREFIX in (df.error_message or "")


def test_apply_decode_failure_errors_before_max_attempts():
    df = _file(name="broken.TIF", decode_attempts=0)
    status = apply_decode_failure(df, "Could not decode image bytes")
    assert status == DriveFileStatus.ERROR
    assert df.decode_attempts == 1
