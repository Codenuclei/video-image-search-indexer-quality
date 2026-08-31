"""Carousel vs drive-search log tagging."""

from __future__ import annotations

import logging

import pytest

from app.search.carousel_trace import (
    CAROUSEL_TAG,
    DRIVE_SEARCH_TAG,
    bind_carousel_context,
    carousel_log,
    carousel_step,
    drive_search_log,
    reset_carousel_context,
)


def test_carousel_log_includes_tag_and_trace(caplog):
    tokens = bind_carousel_context(trace_id="abc123", route="/pipeline/themes", drive_file_id="yt:1")
    try:
        with caplog.at_level(logging.INFO, logger="app.carousel.trace"):
            carousel_log("themes_request_start", force=True, generate=True)
        assert any(CAROUSEL_TAG in r.message for r in caplog.records)
        assert any("trace=abc123" in r.message for r in caplog.records)
        assert any("event=themes_request_start" in r.message for r in caplog.records)
        assert any("route=/pipeline/themes" in r.message for r in caplog.records)
    finally:
        reset_carousel_context(tokens)


def test_drive_search_log_uses_distinct_tag(caplog):
    with caplog.at_level(logging.INFO, logger="app.carousel.trace"):
        drive_search_log("image_search_start", query="tshirt")
    assert any(DRIVE_SEARCH_TAG in r.message for r in caplog.records)
    assert not any(CAROUSEL_TAG in r.message for r in caplog.records)


def test_carousel_step_logs_ok_and_error(caplog):
    tokens = bind_carousel_context(trace_id="step1", route="/pipeline/themes")
    try:
        with caplog.at_level(logging.INFO, logger="app.carousel.trace"):
            with carousel_step("themes_chunk", chunk=1):
                pass
        assert any("event=step_start" in r.message for r in caplog.records)
        assert any("event=step_ok" in r.message for r in caplog.records)

        with caplog.at_level(logging.WARNING, logger="app.carousel.trace"):
            with pytest.raises(RuntimeError, match="boom"):
                with carousel_step("themes_chunk", chunk=2):
                    raise RuntimeError("boom")
        assert any("event=step_error" in r.message for r in caplog.records)
        assert any("error_type=RuntimeError" in r.message for r in caplog.records)
    finally:
        reset_carousel_context(tokens)
