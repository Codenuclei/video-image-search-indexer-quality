from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoProbe:
    duration_seconds: float
    width: int
    height: float


def probe_video(path: str) -> VideoProbe:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
    payload = json.loads(result.stdout)
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    streams = payload.get("streams") or [{}]
    stream = streams[0] if streams else {}
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    return VideoProbe(duration_seconds=duration, width=width, height=height)


def extract_audio_wav(video_path: str, wav_path: str) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        "16000",
        wav_path,
    ]
    subprocess.run(cmd, capture_output=True, timeout=600, check=True)


def extract_frame_at(video_path: str, timestamp_sec: float, output_path: str) -> bool:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp_sec:.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    return proc.returncode == 0 and Path(output_path).is_file()


def sample_timestamps(duration_sec: float, interval_sec: float, max_frames: int) -> list[float]:
    if duration_sec <= 0:
        return [0.0]
    times: list[float] = []
    t = 0.0
    while t < duration_sec and len(times) < max_frames:
        times.append(round(t, 3))
        t += interval_sec
    if not times:
        times.append(0.0)
    return times
