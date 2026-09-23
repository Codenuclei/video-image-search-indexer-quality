from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess

import pytest

from app.config import Settings
from app.video import runpod_whisper
from app.video.runpod_whisper import (
    MAX_PAYLOAD_BASE64_BYTES,
    RunPodWhisperError,
    chunk_windows,
    encode_opus_chunk,
    offset_segments,
    transcribe_wav_runpod,
)
from app.video.whisper_engine import WhisperSegment

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _settings(**overrides) -> Settings:
    base = dict(
        runpod_whisper_enabled=True,
        runpod_api_key="test-key",
        runpod_whisper_endpoint_id="endpoint",
        runpod_whisper_chunk_seconds=600.0,
        runpod_whisper_max_audio_seconds=7200.0,
        runpod_whisper_chunk_concurrency=3,
    )
    base.update(overrides)
    return Settings(**base)


def _make_wav(path: str, seconds: float, source: str = "anoisesrc=a=0.1") -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"{source}:d={seconds}",
            "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", path,
        ],
        check=True,
        capture_output=True,
    )


def test_two_hour_audio_splits_into_twelve_contiguous_chunks():
    windows = chunk_windows(7200.0, 600.0)
    assert len(windows) == 12
    assert windows[0] == (0.0, 600.0)
    assert windows[-1] == (6600.0, 600.0)
    for (start, length), (next_start, _) in zip(windows, windows[1:]):
        assert start + length == pytest.approx(next_start)


def test_partial_last_chunk_covers_remaining_audio():
    windows = chunk_windows(1350.0, 600.0)
    assert windows == [(0.0, 600.0), (600.0, 600.0), (1200.0, 150.0)]


def test_offset_segments_moves_chunk_timestamps_onto_video_timeline():
    segs = [WhisperSegment(1.0, 2.5, "hello"), WhisperSegment(598.0, 605.0, "edge")]
    out = offset_segments(segs, 1200.0, 1800.0)
    assert (out[0].start_sec, out[0].end_sec) == (1201.0, 1202.5)
    assert (out[1].start_sec, out[1].end_sec) == (1798.0, 1800.0)


@needs_ffmpeg
def test_ten_minute_opus_chunk_fits_runpod_payload_limit(tmp_path):
    wav = str(tmp_path / "noise.wav")
    _make_wav(wav, 600)
    out = str(tmp_path / "chunk.ogg")
    encode_opus_chunk(wav, 0.0, 600.0, out)
    encoded = base64.b64encode(open(out, "rb").read())
    assert len(encoded) < MAX_PAYLOAD_BASE64_BYTES
    assert os.path.getsize(wav) > 10 * 1024 * 1024


@needs_ffmpeg
def test_transcribe_merges_chunks_in_order_with_offsets(tmp_path, monkeypatch):
    wav = str(tmp_path / "tone.wav")
    _make_wav(wav, 75, source="sine=frequency=440")
    calls: list[int] = []

    async def fake_run_job(client, settings, audio_b64, *, label):
        index = int(label.split()[-1])
        calls.append(index)
        assert len(audio_b64) < MAX_PAYLOAD_BASE64_BYTES
        return [WhisperSegment(0.5, 1.5, f"chunk-{index}")]

    monkeypatch.setattr(runpod_whisper, "_run_job", fake_run_job)
    segments = asyncio.run(
        transcribe_wav_runpod(wav, _settings(runpod_whisper_chunk_seconds=30.0))
    )

    assert sorted(calls) == [0, 1, 2]
    assert [s.text for s in segments] == ["chunk-0", "chunk-1", "chunk-2"]
    assert [s.start_sec for s in segments] == [0.5, 30.5, 60.5]


def test_audio_over_limit_is_rejected_before_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(runpod_whisper, "wav_duration_seconds", lambda _p: 7300.0)

    async def fail_run_job(*_a, **_k):
        raise AssertionError("must not upload")

    monkeypatch.setattr(runpod_whisper, "_run_job", fail_run_job)
    with pytest.raises(RunPodWhisperError, match="limit"):
        asyncio.run(transcribe_wav_runpod(str(tmp_path / "x.wav"), _settings()))
