"""Pure-function tests for image-search URL unwrapping (no network)."""

from __future__ import annotations

from urllib.parse import quote

from app.reid.external_crawl import extract_og_image_url, unwrap_image_page_url


def test_unwrap_yahoo_imgurl_param():
    real = "https://cdn.example.com/photos/headshot.jpg"
    wrapper = f"https://images.search.yahoo.com/images/view?imgurl={quote(real, safe='')}&ru=search"
    assert unwrap_image_page_url(wrapper) == real


def test_unwrap_bing_mediaurl_param():
    real = "https://media.example.org/a%20b.png"
    wrapper = f"https://www.bing.com/images/search?view=detailV2&mediaurl={quote(real, safe='')}"
    assert unwrap_image_page_url(wrapper) == real


def test_unwrap_leaves_plain_image_url():
    url = "https://cdn.example.com/face.jpeg"
    assert unwrap_image_page_url(url) == url


def test_unwrap_ignores_non_http_param():
    wrapper = "https://images.search.yahoo.com/images/view?imgurl=javascript:alert(1)"
    assert unwrap_image_page_url(wrapper) == wrapper


def test_extract_og_image_property_first():
    html = '<html><head><meta property="og:image" content="https://cdn.example.com/og.jpg"/></head></html>'
    assert extract_og_image_url(html) == "https://cdn.example.com/og.jpg"


def test_extract_og_image_content_first():
    html = "<meta content='https://cdn.example.com/alt.webp' property='og:image'>"
    assert extract_og_image_url(html) == "https://cdn.example.com/alt.webp"


def test_extract_og_image_missing():
    assert extract_og_image_url("<html><body>no meta</body></html>") is None
