"""Add-if-missing gates: never wipe media when it already exists."""

from __future__ import annotations

import inspect

from app.pipelines import image as image_mod
from app.pipelines import video as video_mod


def test_image_pipeline_gates_clear_existing_media():
    src = inspect.getsource(image_mod.process_image_file)
    assert "file_has_media" in src
    assert "clear_existing_media" in src
    # Gate must run before wipe.
    assert src.index("file_has_media") < src.index("await clear_existing_media")


def test_video_pipeline_gates_clear_existing_media():
    src = inspect.getsource(video_mod.process_video_file)
    assert "file_has_media" in src
    assert "clear_existing_media" in src
    assert src.index("file_has_media") < src.index("await clear_existing_media")


def test_caption_backfill_does_not_delete_then_gap():
    from app.workers import maintenance as maintenance_mod

    src = inspect.getsource(maintenance_mod.run_caption_backfill)
    assert "delete_caption_sync" not in src
    assert "Upsert-by-id" in src or "upsert" in src.lower()
