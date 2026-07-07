from app.pipelines.common import is_indexable_mime


def test_indexable_pdf_and_images():
    assert is_indexable_mime("application/pdf")
    assert is_indexable_mime("image/jpeg")
    assert is_indexable_mime("image/png")


def test_skip_video_when_indexing_disabled(monkeypatch):
    monkeypatch.setenv("VIDEO_INDEXING_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    assert not is_indexable_mime("video/mp4")
    get_settings.cache_clear()


def test_video_indexable_when_enabled(monkeypatch):
    monkeypatch.setenv("VIDEO_INDEXING_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    assert is_indexable_mime("video/mp4")
    get_settings.cache_clear()


def test_skip_spreadsheet():
    assert not is_indexable_mime("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
