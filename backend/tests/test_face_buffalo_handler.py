"""RunPod buffalo handler wraps the same FaceEngine.detect_faces extras as CPU."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runpod" / "face-buffalo"))

from handler import extract_frame_ffmpeg, faces_from_insightface  # noqa: E402


def test_handler_uses_faceanalysis_get_not_split_det_rec() -> None:
    text = (REPO / "runpod" / "face-buffalo" / "handler.py").read_text()
    assert "_app.get(" in text
    assert "round(float(x), 7)" not in text
    assert "det_model.detect" not in text
    assert "allowed_modules" not in text


def test_faces_from_insightface_matches_engine_fields() -> None:
    vec = np.zeros(512, dtype=np.float32)
    vec[1] = 1.0
    image = np.zeros((40, 40, 3), dtype=np.uint8)
    image[5:20, 5:20] = 255
    face = SimpleNamespace(
        det_score=0.91,
        bbox=np.array([5.0, 5.0, 20.0, 20.0], dtype=np.float32),
        normed_embedding=vec,
    )
    out = faces_from_insightface(image, [face], min_confidence=0.5)
    assert len(out) == 1
    assert out[0]["bbox_x"] == 5.0
    assert out[0]["bbox_y"] == 5.0
    assert out[0]["bbox_width"] == 15.0
    assert out[0]["bbox_height"] == 15.0
    assert out[0]["confidence"] == 0.91
    assert out[0]["embedding"][1] == 1.0
    assert len(out[0]["embedding"]) == 512
    assert isinstance(out[0]["thumbnail_b64"], str)
    assert out[0]["thumbnail_b64"]


def test_handler_ffmpeg_video_path_cleans_tmp() -> None:
    text = (REPO / "runpod" / "face-buffalo" / "handler.py").read_text()
    assert "extract_frame_ffmpeg" in text
    assert "-hwaccel" in text
    assert "shutil.rmtree" in text
    assert "dfi-rface-video-" in text
    assert "download_http_file" in text
    assert "video_url" in text
    assert "refusing Drive URL" in text


def test_download_http_file_parallel_ranges(tmp_path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from handler import download_http_file, video_url_is_blocked

    payload = os.urandom(512 * 1024)

    class RangeHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            rng = self.headers.get("Range") or ""
            if rng.lower().startswith("bytes="):
                spec = rng.split("=", 1)[1]
                start_s, end_s = spec.split("-", 1)
                start = int(start_s)
                end = int(end_s) if end_s else len(payload) - 1
                end = min(end, len(payload) - 1)
                chunk = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/clip.bin"
        dest = tmp_path / "out.bin"
        info = download_http_file(url, str(dest), max_bytes=10 * 1024 * 1024, parts=8)
        assert dest.read_bytes() == payload
        assert info["bytes"] == len(payload)
        assert info["mode"] == "range"
        assert info["parts"] >= 2
        assert video_url_is_blocked("https://drive.google.com/file/d/x") is True
    finally:
        server.shutdown()
        server.server_close()


def test_extract_frame_ffmpeg_prefers_nvdec(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("handler.subprocess.run", fake_run)
    out = tmp_path / "frame.jpg"
    mode = extract_frame_ffmpeg("/tmp/clip.mp4", 1.25, str(out))
    assert mode == "nvdec"
    assert "-hwaccel" in calls[0]
    assert "cuda" in calls[0]
    assert len(calls) == 1


def test_extract_frame_ffmpeg_falls_back_to_cpu(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-hwaccel" in cmd:
            return SimpleNamespace(returncode=1, stderr=b"no nvdec")
        Path(cmd[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("handler.subprocess.run", fake_run)
    out = tmp_path / "frame.jpg"
    mode = extract_frame_ffmpeg("/tmp/clip.mp4", 0.0, str(out))
    assert mode == "cpu"
    assert len(calls) == 2


def test_faces_from_insightface_drops_low_confidence() -> None:
    vec = np.ones(512, dtype=np.float32)
    face = SimpleNamespace(
        det_score=0.1,
        bbox=np.array([0.0, 0.0, 10.0, 10.0], dtype=np.float32),
        normed_embedding=vec,
    )
    image = np.zeros((20, 20, 3), dtype=np.uint8)
    assert faces_from_insightface(image, [face], min_confidence=0.5) == []
