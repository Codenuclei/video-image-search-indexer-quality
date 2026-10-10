"""Unit + ffmpeg integration tests for the 4K burst snapshot picker."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.search.carousel_burst_refine import (
    anchor_from_identity_catalog,
    master_frame_path,
    write_face_sidecar,
)
from app.search.carousel_frame_select import (
    cached_frame_path,
    pick_best_burst_frame,
    score_burst_frame,
)
from app.video.frame_burst import (
    burst_job_key,
    clamp_burst_window,
    cleanup_burst_losers,
    ensure_burst_extracted,
    extract_burst,
    parse_showinfo_pts,
)


def test_clamp_burst_window_centred_inside_quote():
    start, window = clamp_burst_window(
        10.0,
        1.6,
        quote_start=5.0,
        quote_end=20.0,
        duration=60.0,
    )
    assert start == pytest.approx(9.2, abs=0.01)
    assert window == pytest.approx(1.6, abs=0.01)


def test_clamp_burst_window_respects_quote_and_duration():
    start, window = clamp_burst_window(
        1.0,
        1.6,
        quote_start=0.5,
        quote_end=1.5,
        duration=2.0,
    )
    assert start >= 0.5
    assert start + window <= 1.5 + 1e-6
    assert window <= 1.6 + 1e-6


def test_clamp_burst_window_near_video_end():
    start, window = clamp_burst_window(
        9.5,
        1.6,
        quote_start=0.0,
        quote_end=10.0,
        duration=10.0,
    )
    assert start >= 0.0
    assert start + window <= 10.0 + 1e-6


def test_parse_showinfo_pts():
    stderr = (
        b"n:0 pts:0 pts_time:0.000000\n"
        b"n:1 pts:512 pts_time:0.400000\n"
        b"n:2 pts:1024 pts_time:0.800000\n"
    )
    assert parse_showinfo_pts(stderr) == pytest.approx([0.0, 0.4, 0.8])


def test_anchor_prefers_catalog_appearance():
    catalog = {
        "identities": [
            {
                "id": "id_0",
                "appearances": [
                    {
                        "frame_ts": 12.5,
                        "front_face_score": 0.9,
                        "quality_score": 10,
                        "detection_confidence": 0.9,
                    }
                ],
            }
        ]
    }
    association = {"mode": "speaker", "identity_id": "id_0"}
    ts, src = anchor_from_identity_catalog(
        catalog, association=association, start_sec=10.0, end_sec=20.0
    )
    assert ts == 12.5
    assert src == "catalog"


def test_anchor_falls_back_to_heuristic():
    ts, src = anchor_from_identity_catalog(
        {}, association={"mode": "text_only"}, start_sec=10.0, end_sec=20.0
    )
    assert ts == 15.0
    assert src == "heuristic"


def _sharp_synthetic(path: Path, *, blur: bool = False) -> None:
    """High-contrast checkerboard (sharp) or motion-blurred version."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    for y in range(0, 480, 20):
        for x in range(0, 640, 20):
            if ((x // 20) + (y // 20)) % 2 == 0:
                img[y : y + 20, x : x + 20] = (240, 240, 240)
            else:
                img[y : y + 20, x : x + 20] = (20, 20, 20)
    # Fake "face" region with strong edges for face-crop sharpness.
    img[160:320, 240:400] = (180, 140, 120)
    cv2.rectangle(img, (250, 180), (390, 300), (40, 40, 40), 2)
    if blur:
        img = cv2.GaussianBlur(img, (31, 31), 0)
        # Extra horizontal smear ≈ motion blur
        kernel = np.zeros((1, 21), dtype=np.float32)
        kernel[0, :] = 1.0 / 21.0
        img = cv2.filter2D(img, -1, kernel)
    cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])


def test_scorer_prefers_sharp_over_motion_blur(tmp_path):
    sharp = tmp_path / "sharp.jpg"
    blurry = tmp_path / "blurry.jpg"
    _sharp_synthetic(sharp, blur=False)
    _sharp_synthetic(blurry, blur=True)

    # Synthetic face boxes (normalized) so face-crop sharpness participates.
    faces = [
        {
            "bbox_x": 0.375,
            "bbox_y": 0.33,
            "bbox_width": 0.25,
            "bbox_height": 0.29,
            "detection_confidence": 0.95,
            "yaw": 5.0,
            "pitch": 0.0,
            "roll": 0.0,
        }
    ]
    q_sharp = score_burst_frame(sharp, faces=faces, check_pixelation=False)
    q_blur = score_burst_frame(blurry, faces=faces, check_pixelation=False)
    assert q_sharp.get("ok") or q_sharp.get("sharpness", 0) > 0
    assert float(q_sharp.get("score") or 0) > float(q_blur.get("score") or 0)

    picked = pick_best_burst_frame(
        [(1.0, sharp), (1.2, blurry)],
        faces_by_index=[faces, faces],
        check_pixelation=False,
    )
    assert picked is not None
    assert picked[1] == sharp


def test_extract_burst_fallback_on_bad_source(tmp_path):
    frames = extract_burst(
        str(tmp_path / "missing.mp4"),
        1.0,
        out_dir=tmp_path / "burst",
        n=5,
        timeout_sec=5,
    )
    assert frames == []


@pytest.mark.asyncio
async def test_burst_coalesce_single_job(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_extract(*args, **kwargs):
        calls["n"] += 1
        out = Path(kwargs.get("out_dir") or args[4] if len(args) > 4 else tmp_path)
        # extract_burst signature uses out_dir kw; ensure dir exists.
        dest = kwargs.get("out_dir") or tmp_path / "b"
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(5):
            p = dest / f"frame_{i:02d}.jpg"
            p.write_bytes(b"\xff\xd8\xff" + b"x" * 64)
            paths.append((1.0 + i * 0.3, p))
        return paths

    monkeypatch.setattr("app.video.frame_burst.extract_burst", fake_extract)
    # Clear any leftover jobs from prior tests.
    from app.video import frame_burst as fb

    fb._BURST_JOBS.clear()

    sem = asyncio.Semaphore(2)
    out_dir = tmp_path / "burst"
    results = await asyncio.gather(
        *[
            ensure_burst_extracted(
                drive_file_id="vid",
                anchor_ts=5.0,
                source="/tmp/fake.mp4",
                out_dir=out_dir,
                extract_sem=sem,
            )
            for _ in range(4)
        ]
    )
    assert calls["n"] == 1
    assert all(len(r) == 5 for r in results)
    assert burst_job_key("vid", 5.0) == "vid:5.000:burst"


def test_cleanup_deletes_losers(tmp_path):
    frames = []
    for i in range(5):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"x")
        frames.append((float(i), p))
    winner = frames[2][1]
    cleanup_burst_losers(frames, winner)
    assert winner.is_file()
    assert sum(1 for _ts, p in frames if p.is_file()) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_burst_integration(tmp_path):
    clip = tmp_path / "clip.mp4"
    # 3s 320x240 test pattern at 25fps.
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=25",
        "-t",
        "3",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-400:]

    out_dir = tmp_path / "burst"
    frames = extract_burst(
        str(clip),
        anchor_ts=1.5,
        window_sec=1.6,
        n=5,
        out_dir=out_dir,
        max_long_edge=3840,
        quote_start=0.0,
        quote_end=3.0,
        duration=3.0,
        timeout_sec=30,
    )
    assert len(frames) == 5
    stamps = [ts for ts, _ in frames]
    assert min(stamps) >= 0.5 - 0.25
    assert max(stamps) <= 2.5 + 0.25
    for ts, path in frames:
        assert path.is_file()
        assert path.stat().st_size > 100

    picked = pick_best_burst_frame(frames, check_pixelation=False)
    assert picked is not None
    winner_ts, winner_path, quality = picked

    thumb = tmp_path / "thumbs"
    fid = "integration-vid"
    canonical = cached_frame_path(str(thumb), fid, winner_ts)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(winner_path, canonical)
    master = master_frame_path(str(thumb), fid, winner_ts)
    master.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(winner_path, master)
    write_face_sidecar(canonical, focal_x=0.5, focal_y=0.4)

    assert canonical.is_file()
    assert master.is_file()
    assert Path(str(canonical) + ".face.json").is_file()
    assert float(quality.get("score") or 0) >= 0.0


# ── Audit regressions ─────────────────────────────────────────────────────────


def test_clamp_ignores_quote_when_anchor_is_elsewhere():
    """Speaker's best appearance is often outside this slide's quote.

    It used to collapse the window to ~0.05s at the wrong place (near-duplicate
    frames). The quote must only constrain the window when the anchor is inside.
    """
    from app.video.frame_burst import clamp_burst_window

    start, window = clamp_burst_window(
        100.0, 1.6, quote_start=10.0, quote_end=15.0, duration=600.0
    )
    assert window == pytest.approx(1.6, abs=1e-6)
    assert start == pytest.approx(99.2, abs=1e-6)

    start, window = clamp_burst_window(
        2.0, 1.6, quote_start=10.0, quote_end=15.0, duration=600.0
    )
    assert window == pytest.approx(1.6, abs=1e-6)
    assert start == pytest.approx(1.2, abs=1e-6)


def test_clamp_still_bounds_window_when_anchor_inside_quote():
    from app.video.frame_burst import clamp_burst_window

    start, window = clamp_burst_window(
        10.4, 1.6, quote_start=10.0, quote_end=10.9, duration=600.0
    )
    assert start >= 10.0 - 1e-6
    assert start + window <= 10.9 + 1e-6


def test_pick_best_burst_frame_never_promotes_hard_rejects(tmp_path):
    """All-rejected bursts must return None so the existing candidate is kept."""
    from app.search.carousel_frame_select import pick_best_burst_frame

    frames = []
    for i in range(5):
        p = tmp_path / f"black_{i}.jpg"
        cv2.imwrite(str(p), np.zeros((540, 960, 3), np.uint8))
        frames.append((float(i), p))
    assert pick_best_burst_frame(frames) is None


@pytest.mark.asyncio
async def test_apply_burst_refine_cap_deadline_and_update(monkeypatch):
    import time

    from app.config import Settings
    from app.search import carousel_burst_refine as mod

    def _slide(n: int) -> dict:
        return {
            "drive_file_id": "vid",
            "timestamp_sec": float(n * 10),
            "end_timestamp_sec": float(n * 10 + 5),
            "frame_candidate_items": [
                {"frame_ts": float(n), "recommended": True, "label": "AI recommended"},
                {"frame_ts": float(n) + 0.5, "recommended": False},
            ],
            "frame_candidates": [float(n), float(n) + 0.5],
        }

    async def fake_refine(**kw):
        rec = dict(kw["recommended"])
        rec["frame_ts"] = round(rec["frame_ts"] + 0.25, 3)
        rec["burst"] = {"cache_hit": False}
        return rec

    monkeypatch.setattr(mod, "refine_recommended_with_burst", fake_refine)
    settings = Settings(carousel_burst_enabled=True, carousel_burst_max_per_request=2)
    slides = [_slide(i) for i in range(4)]
    out, summary = await mod.apply_burst_refine_to_slides(
        slides,
        thumbnail_dir="/tmp/x",
        drive_file_id="vid",
        catalog=None,
        settings=settings,
        source="/tmp/v.mp4",
        deadline_monotonic=time.monotonic() + 30,
    )
    assert summary["succeeded"] == 2          # per-request cap
    assert summary["skipped"] == 2
    assert out[0]["frame_candidate_items"][0]["frame_ts"] == 0.25
    assert out[0]["frame_candidates"] == [0.25, 0.5]   # kept in sync
    assert out[3]["frame_candidate_items"][0]["frame_ts"] == 3.0  # untouched
    assert slides[0]["frame_candidate_items"][0]["frame_ts"] == 0.0  # input not mutated

    # Expired deadline: nothing is attempted, slides pass through unchanged.
    out2, summary2 = await mod.apply_burst_refine_to_slides(
        slides,
        thumbnail_dir="/tmp/x",
        drive_file_id="vid",
        catalog=None,
        settings=settings,
        source="/tmp/v.mp4",
        deadline_monotonic=time.monotonic() - 1,
    )
    assert summary2["succeeded"] == 0
    assert out2[0]["frame_candidate_items"][0]["frame_ts"] == 0.0


@pytest.mark.asyncio
async def test_apply_burst_refine_slow_slide_times_out_and_keeps_original(monkeypatch):
    import time

    from app.config import Settings
    from app.search import carousel_burst_refine as mod

    async def slow_refine(**kw):
        await asyncio.sleep(5)
        return dict(kw["recommended"], frame_ts=99.0)

    monkeypatch.setattr(mod, "refine_recommended_with_burst", slow_refine)
    slides = [
        {
            "drive_file_id": "vid",
            "timestamp_sec": 1.0,
            "end_timestamp_sec": 3.0,
            "frame_candidate_items": [{"frame_ts": 2.0, "recommended": True}],
        }
    ]
    started = time.monotonic()
    out, summary = await mod.apply_burst_refine_to_slides(
        slides,
        thumbnail_dir="/tmp/x",
        drive_file_id="vid",
        catalog=None,
        settings=Settings(carousel_burst_enabled=True),
        source="/tmp/v.mp4",
        deadline_monotonic=time.monotonic() + 1.2,
    )
    assert time.monotonic() - started < 3.0          # bounded by the deadline
    assert summary["succeeded"] == 0
    assert out[0]["frame_candidate_items"][0]["frame_ts"] == 2.0


@pytest.mark.asyncio
async def test_burst_anchor_resolution_does_not_block_event_loop(monkeypatch, tmp_path):
    import time

    from app.config import Settings
    from app.search import carousel_burst_refine as mod

    def blocking_resolve(**kw):
        time.sleep(0.4)          # stands in for the ffmpeg + face-engine probe
        return 5.0, "probe"

    monkeypatch.setattr(mod, "resolve_burst_anchor", blocking_resolve)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await mod.refine_recommended_with_burst(
            thumbnail_dir=str(tmp_path),
            drive_file_id="vid",
            start_sec=1.0,
            end_sec=9.0,
            catalog=None,
            association={},
            recommended={"frame_ts": 5.0, "recommended": True},
            settings=Settings(carousel_burst_enabled=True),
            source=None,      # no source -> returns right after anchor resolution
        )
    finally:
        task.cancel()
    assert ticks >= 8, f"event loop was blocked (only {ticks} ticks)"


@pytest.mark.asyncio
async def test_burst_winner_preview_keeps_hdr_variant(monkeypatch, tmp_path):
    from app.search import carousel_burst_refine as mod

    monkeypatch.setattr(
        "app.video.frame_enhance.ensure_hdr_for_timestamp",
        lambda *a, **k: {"ok": True},
    )
    preview, hdr = await mod._preview_for_winner(str(tmp_path), "vid", 12.5, prefer_hdr=True)
    assert hdr is True and preview and preview.endswith("&variant=hdr")

    monkeypatch.setattr(
        "app.video.frame_enhance.ensure_hdr_for_timestamp",
        lambda *a, **k: {"ok": False},
    )
    preview, hdr = await mod._preview_for_winner(str(tmp_path), "vid", 12.5, prefer_hdr=True)
    assert hdr is False and "variant=hdr" not in (preview or "")


def test_drive_stream_url_escapes_file_id():
    """A hostile id must not add path/query segments to the Bearer-token URL."""
    import inspect

    from app.search import carousel_burst_refine as mod

    src = inspect.getsource(mod._drive_stream_source)
    assert "quote(str(drive_file_id), safe='')" in src


def test_extract_burst_default_out_dir_is_not_shared(tmp_path):
    import inspect

    from app.video import frame_burst

    assert '"/tmp/burst"' not in inspect.getsource(frame_burst.extract_burst)


def test_pick_best_burst_frame_runs_pixelation_only_on_top_ranked(monkeypatch):
    """Pixelation is the slow check: it must run on ~1 frame, not all of them."""
    from app.search import carousel_frame_select as fs

    calls: list[tuple[str, bool]] = []

    def fake_score(path, *, faces=None, check_pixelation=True):
        calls.append((Path(path).name, bool(check_pixelation)))
        return {"reject": None, "score": float(Path(path).stem)}

    monkeypatch.setattr(fs, "score_burst_frame", fake_score)
    frames = [(float(i), Path(f"{i}.0.jpg")) for i in range(1, 6)]
    picked = fs.pick_best_burst_frame(frames)
    assert picked is not None and picked[0] == 5.0
    assert sum(1 for _n, pix in calls if pix) == 1


def test_pick_best_burst_frame_falls_through_pixelated_top(monkeypatch):
    from app.search import carousel_frame_select as fs

    def fake_score(path, *, faces=None, check_pixelation=True):
        score = float(Path(path).stem)
        reject = "pixelated" if (check_pixelation and score >= 4.0) else None
        return {"reject": reject, "score": score}

    monkeypatch.setattr(fs, "score_burst_frame", fake_score)
    frames = [(float(i), Path(f"{i}.0.jpg")) for i in range(1, 6)]
    picked = fs.pick_best_burst_frame(frames)
    assert picked is not None and picked[0] == 3.0  # 5 and 4 pixelated -> next best

    # every frame pixelated -> never promote one
    monkeypatch.setattr(
        fs,
        "score_burst_frame",
        lambda path, *, faces=None, check_pixelation=True: {
            "reject": "pixelated" if check_pixelation else None,
            "score": 1.0,
        },
    )
    assert fs.pick_best_burst_frame(frames) is None
