"""Unit tests for batch embed + caption production defaults."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.db.models import DriveFileStatus
from app.gemini.video_embeddings import embed_frames_batch_sync
from app.routers.drive import _parse_drive_file_status
from app.workers import maintenance as maintenance_mod


def test_production_defaults_match_probe_settings():
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
    )
    # pydantic-settings still reads env; assert field defaults on the class
    assert Settings.model_fields["image_embed_batch_size"].default == 5
    assert Settings.model_fields["image_embed_backfill_parallel"].default == 20
    assert Settings.model_fields["image_embed_max_edge"].default == 1024
    # Production caption path: 10 images per Gemini call × Semaphore(5)
    assert Settings.model_fields["image_caption_batch_size"].default == 10
    assert Settings.model_fields["image_caption_batch_parallel"].default == 5
    assert Settings.model_fields["image_caption_model"].default == "gemini-3.5-flash-lite"
    assert Settings.model_fields["gemini_embed_max_concurrent"].default == 32
    assert Settings.model_fields["gemini_vlm_max_concurrent"].default == 24
    _ = s


def test_caption_backfill_wires_batch_size_and_semaphore():
    """run_caption_backfill must chunk by batch_size and gate Gemini with Semaphore(parallel)."""
    src = inspect.getsource(maintenance_mod.run_caption_backfill)
    assert "settings.image_caption_batch_size" in src
    assert "settings.image_caption_batch_parallel" in src
    assert "asyncio.Semaphore(batch_parallel)" in src
    assert "index_image_captions_batch" in src


def test_queue_active_tab_maps_to_processing_status():
    assert _parse_drive_file_status(None) is None
    assert _parse_drive_file_status("processing") is DriveFileStatus.PROCESSING
    assert _parse_drive_file_status("Active") is DriveFileStatus.PROCESSING
    assert _parse_drive_file_status("completed") is DriveFileStatus.PROCESSED
    assert _parse_drive_file_status("failed") is DriveFileStatus.ERROR


def test_embed_frames_batch_sync_builds_content_per_image(tmp_path):
    # Minimal 1x1 JPEG
    from PIL import Image

    paths = []
    for i in range(3):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (32, 32), color=(i * 40, 10, 10)).save(p, format="JPEG")
        paths.append(str(p))

    fake_emb = MagicMock()
    fake_emb.values = [0.1] * 8
    fake_result = MagicMock()
    fake_result.embeddings = [fake_emb, fake_emb, fake_emb]

    fake_client = MagicMock()
    fake_client.models.embed_content.return_value = fake_result

    with (
        patch("app.gemini.video_embeddings._get_client", return_value=fake_client),
        patch("app.gemini.video_embeddings.gemini_embed_slot") as slot,
        patch("app.config.get_settings") as gs,
    ):
        slot.return_value.__enter__ = MagicMock(return_value=None)
        slot.return_value.__exit__ = MagicMock(return_value=False)
        gs.return_value = MagicMock(image_embed_max_edge=1024, gemini_api_key="x")

        vectors = embed_frames_batch_sync(paths)

    assert len(vectors) == 3
    assert fake_client.models.embed_content.called
    call_kw = fake_client.models.embed_content.call_args.kwargs
    assert len(call_kw["contents"]) == 3
