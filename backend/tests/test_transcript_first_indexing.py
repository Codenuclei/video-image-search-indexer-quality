"""Transcript-first Studio ingest + quote-window identity helpers."""

from __future__ import annotations

import inspect

from app.search.carousel_quote_identity import (
    quote_intervals_from_slides,
    quote_window_sample_timestamps,
)
from app.search.carousel_visual_prep import read_job, slides_fingerprint, write_job
from app.pipelines import video as video_mod
from app.workers import indexer as indexer_mod
from app.workers.indexer import needs_drive_folder_listing
from types import SimpleNamespace


def test_transcript_first_skips_frame_and_face_work() -> None:
    source = inspect.getsource(video_mod.process_video_file)
    assert "video_transcript_first_enabled" in source
    assert "Indexed video %s transcript-first" in source
    assert "face_job_queued=False" in source
    assert "transcript_first_no_cues" in source
    # Must not enqueue object/face jobs on the early return path.
    early = source.split("Indexed video %s transcript-first")[0]
    assert "enqueue_face_job" not in early
    assert "enqueue_object_job" not in early


def test_indexer_skips_drive_listing_when_transcript_first() -> None:
    source = inspect.getsource(indexer_mod.IndexingWorker._run_video_index_job)
    assert "video_transcript_first_enabled" in source
    assert "list_folder_files" in source
    # Guard must wrap the listing call.
    listing_pos = source.index("list_folder_files")
    guard_pos = source.index("video_transcript_first_enabled")
    assert guard_pos < listing_pos


def test_quote_window_samples_are_hard_capped() -> None:
    slides = [
        {"timestamp_sec": float(i * 10), "end_timestamp_sec": float(i * 10 + 5)}
        for i in range(40)
    ]
    intervals = quote_intervals_from_slides(slides)
    stamps = quote_window_sample_timestamps(intervals, cap=24)
    assert len(stamps) <= 24
    assert stamps == sorted(stamps)
    assert 0.0 in stamps


def test_quote_intervals_swap_inverted_bounds() -> None:
    assert quote_intervals_from_slides([{"timestamp_sec": "nope"}]) == []
    assert quote_intervals_from_slides(
        [{"timestamp_sec": 5, "end_timestamp_sec": 3}]
    ) == [(3.0, 5.0)]


def test_visual_prep_job_roundtrip(tmp_path) -> None:
    data = write_job(
        str(tmp_path),
        "vid-1",
        status="preparing",
        request_body={"slides_fingerprint": "abc"},
    )
    loaded = read_job(str(tmp_path), "vid-1", data["job_id"])
    assert loaded is not None
    assert loaded["status"] == "preparing"
    write_job(
        str(tmp_path),
        "vid-1",
        job_id=data["job_id"],
        status="ready",
        payload={"images_ready": True},
    )
    ready = read_job(str(tmp_path), "vid-1", data["job_id"])
    assert ready["status"] == "ready"
    assert ready["result"]["images_ready"] is True


def test_slides_fingerprint_stable() -> None:
    slides = [
        {"timestamp_sec": 1, "end_timestamp_sec": 2, "transcript_text": "hello"},
        {"timestamp_sec": 3, "end_timestamp_sec": 4, "hook_line": "world"},
    ]
    assert slides_fingerprint(slides) == slides_fingerprint(list(slides))


def test_needs_drive_listing_still_true_for_drive_videos() -> None:
    drive = SimpleNamespace(id="1DriveFileIdxxxxx", source="drive", name="drive.mp4")
    assert needs_drive_folder_listing(drive) is True


def test_runpod_clients_exist() -> None:
    from app.video import runpod_whisper
    from app.faces import runpod_face

    assert hasattr(runpod_whisper, "transcribe_wav_runpod")
    assert hasattr(runpod_face, "detect_faces_runpod_frames")
    assert runpod_whisper.runpod_whisper_configured(
        type("S", (), {"runpod_whisper_enabled": False, "runpod_api_key": "", "runpod_whisper_endpoint_id": ""})()
    ) is False
