from app.db.models import MediaType
from app.pipelines.common import classify_mime


def test_classify_image_mime():
    assert classify_mime("image/jpeg") == MediaType.IMAGE
    assert classify_mime("image/png") == MediaType.IMAGE


def test_classify_video_mime():
    assert classify_mime("video/mp4") == MediaType.VIDEO
    assert classify_mime("video/quicktime") == MediaType.VIDEO


def test_classify_pdf_mime():
    assert classify_mime("application/pdf") == MediaType.PDF


def test_classify_unsupported_mime_returns_none():
    assert classify_mime("application/vnd.google-apps.document") is None
    assert classify_mime("text/plain") is None
    assert classify_mime("application/octet-stream") is None
