from app.pipelines.pdf import page_needs_ocr


def test_page_needs_ocr_skips_pure_text():
    settings = type("S", (), {"pdf_ocr_min_native_text_chars": 20})()
    assert page_needs_ocr("This is a long paragraph of native PDF text content.", settings) is False


def test_page_needs_ocr_scanned_page():
    settings = type("S", (), {"pdf_ocr_min_native_text_chars": 20})()
    assert page_needs_ocr("", settings) is True


def test_page_needs_ocr_short_caption():
    settings = type("S", (), {"pdf_ocr_min_native_text_chars": 20})()
    assert page_needs_ocr("caption only", settings) is True
