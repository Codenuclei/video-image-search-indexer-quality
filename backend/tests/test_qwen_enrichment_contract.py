from __future__ import annotations

import inspect

import pytest

from app.db.models import QwenCaption, QwenEnrichmentJob, VideoSegmentLabel
from app.qwen.persistence import (
    QWEN_ENRICHMENT_PROMPT_VERSION,
    qwen_target_key,
)


def test_qwen_schema_has_versioned_idempotency_and_raw_response() -> None:
    job_columns = QwenEnrichmentJob.__table__.columns
    caption_columns = QwenCaption.__table__.columns
    label_columns = VideoSegmentLabel.__table__.columns
    assert {"target_key", "prompt_version", "model_version", "raw_response"} <= set(
        job_columns.keys()
    )
    assert {"caption", "prompt_version", "model_version"} <= set(caption_columns.keys())
    assert {"video_segment_id", "canonical_label", "prompt_version", "model_version"} <= set(
        label_columns.keys()
    )


def test_qwen_target_requires_exactly_one_kind() -> None:
    assert qwen_target_key(media_id=7) == "media:7"
    assert qwen_target_key(video_segment_id=9) == "segment:9"
    with pytest.raises(ValueError):
        qwen_target_key()
    with pytest.raises(ValueError):
        qwen_target_key(media_id=1, video_segment_id=2)


def test_active_identify_worker_persists_then_embeds_qwen_caption() -> None:
    from app.workers.identify_queue import IdentifyWorkerLoop

    source = inspect.getsource(IdentifyWorkerLoop._persist_worker)
    assert "persist_qwen_result" in source
    assert "index_image_caption_texts" in source
    assert source.index("persist_qwen_result") < source.index("index_image_caption_texts")
    assert "canonical Qwen caption was not embedded" in source
    assert QWEN_ENRICHMENT_PROMPT_VERSION


def test_video_qwen_writes_segment_caption_and_labels() -> None:
    from app.pipelines.video import _enrich_video_segments_qwen

    source = inspect.getsource(_enrich_video_segments_qwen)
    assert "encode_identify_jpeg" in source
    assert "max_edge=settings.qwen_identify_max_edge" in source
    assert "max_bytes=settings.qwen_identify_max_bytes" in source
    assert "persist_qwen_result" in source
    assert "video_segment_id=int(segment.id)" in source
    assert "segment.vlm_description = parsed.caption" in source
