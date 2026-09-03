"""Identity-aware carousel frame catalog + natural HDR helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


def _catalog_fixture() -> dict:
    return {
        "version": "identity-v1",
        "drive_file_id": "vid-1",
        "fingerprint": "abc",
        "frames_scanned": 6,
        "identity_count": 2,
        "identities": [
            {
                "id": "id_0",
                "label": "Person 1",
                "appearances": [
                    {
                        "frame_ts": 1.0,
                        "bbox": [0.3, 0.2, 0.2, 0.3],
                        "front_face_score": 0.4,
                        "quality_score": 0.5,
                        "detection_confidence": 0.9,
                        "face_count": 1,
                    },
                    {
                        "frame_ts": 12.0,
                        "bbox": [0.35, 0.2, 0.25, 0.35],
                        "front_face_score": 0.95,
                        "quality_score": 0.9,
                        "detection_confidence": 0.99,
                        "face_count": 1,
                    },
                    {
                        "frame_ts": 2.0,
                        "bbox": [0.32, 0.2, 0.22, 0.32],
                        "front_face_score": 0.55,
                        "quality_score": 0.6,
                        "detection_confidence": 0.9,
                        "face_count": 1,
                    },
                ],
            },
            {
                "id": "id_1",
                "label": "Person 2",
                "appearances": [
                    {
                        "frame_ts": 20.0,
                        "bbox": [0.4, 0.2, 0.2, 0.3],
                        "front_face_score": 0.8,
                        "quality_score": 0.7,
                        "detection_confidence": 0.9,
                        "face_count": 1,
                    }
                ],
            },
        ],
        "group_frames": [
            {"frame_ts": 5.0, "face_count": 3, "quality_score": 0.75},
            {"frame_ts": 30.0, "face_count": 2, "quality_score": 0.8},
        ],
    }


def test_normalize_bbox_pixel_and_unit():
    from app.search.carousel_identity_catalog import normalize_bbox

    nx, ny, nw, nh = normalize_bbox(128, 72, 256, 144, 1280, 720)
    assert 0.09 < nx < 0.11
    assert 0.09 < ny < 0.11
    assert 0.19 < nw < 0.21
    assert 0.19 < nh < 0.21

    ux, uy, uw, uh = normalize_bbox(0.1, 0.1, 0.2, 0.2, 1280, 720)
    assert ux == pytest.approx(0.1)
    assert uw == pytest.approx(0.2)


def test_front_face_score_normalizes_pixel_boxes():
    from app.search.carousel_frame_select import front_face_score

    pixel = {
        "bbox_x": 400,
        "bbox_y": 150,
        "bbox_width": 300,
        "bbox_height": 360,
        "image_width": 1280,
        "image_height": 720,
        "detection_confidence": 0.95,
        "yaw": 5.0,
        "pitch": 2.0,
        "roll": 1.0,
    }
    unit = {
        "bbox_x": 400 / 1280,
        "bbox_y": 150 / 720,
        "bbox_width": 300 / 1280,
        "bbox_height": 360 / 720,
        "detection_confidence": 0.95,
        "yaw": 5.0,
        "pitch": 2.0,
        "roll": 1.0,
    }
    assert front_face_score(pixel) == pytest.approx(front_face_score(unit), rel=1e-3)


def test_associate_stable_speaker():
    from app.search.carousel_identity_catalog import associate_quote_identity

    assoc = associate_quote_identity(_catalog_fixture(), start_sec=0.5, end_sec=2.5)
    assert assoc["mode"] == "speaker"
    assert assoc["identity_id"] == "id_0"


def test_associate_multi_face_panel():
    from app.search.carousel_identity_catalog import associate_quote_identity

    catalog = _catalog_fixture()
    catalog["identities"][0]["appearances"] = [
        {
            "frame_ts": 5.0,
            "bbox": [0.1, 0.1, 0.2, 0.3],
            "front_face_score": 0.5,
            "quality_score": 0.6,
            "detection_confidence": 0.9,
            "face_count": 3,
        },
        {
            "frame_ts": 5.2,
            "bbox": [0.5, 0.1, 0.2, 0.3],
            "front_face_score": 0.5,
            "quality_score": 0.55,
            "detection_confidence": 0.9,
            "face_count": 3,
        },
    ]
    assoc = associate_quote_identity(catalog, start_sec=4.5, end_sec=5.5)
    assert assoc["mode"] == "group_panel"
    assert assoc["panel_frame_ts"] == pytest.approx(5.0)


def test_associate_low_confidence_text_only():
    from app.search.carousel_identity_catalog import associate_quote_identity

    catalog = {
        "identities": [
            {
                "id": "id_0",
                "label": "A",
                "appearances": [
                    {
                        "frame_ts": 1.0,
                        "face_count": 1,
                        "front_face_score": 0.5,
                        "quality_score": 0.5,
                        "detection_confidence": 0.8,
                    }
                ],
            },
            {
                "id": "id_1",
                "label": "B",
                "appearances": [
                    {
                        "frame_ts": 1.2,
                        "face_count": 1,
                        "front_face_score": 0.5,
                        "quality_score": 0.5,
                        "detection_confidence": 0.8,
                    }
                ],
            },
        ],
        "group_frames": [],
    }
    assoc = associate_quote_identity(catalog, start_sec=0.5, end_sec=1.5)
    assert assoc["mode"] == "text_only"


def test_ranking_ignores_timestamp_proximity(tmp_path, monkeypatch):
    from app.search import carousel_identity_catalog as cat

    root = tmp_path / "thumbs"
    for ts in (1.0, 12.0, 2.0, 20.0, 5.0, 30.0):
        path = root / "video" / "vid-1" / f"{ts:.3f}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpeg")

    monkeypatch.setattr(
        "app.video.frame_enhance.ensure_hdr_for_timestamp",
        lambda *a, **k: {"ok": False},
    )

    assoc = {"mode": "speaker", "identity_id": "id_0", "identity_label": "Person 1"}
    items = cat.build_slide_identity_candidates(
        _catalog_fixture(),
        drive_file_id="vid-1",
        thumbnail_dir=str(root),
        association=assoc,
        prefer_hdr=False,
    )
    # Best portrait is at 12.0 even though the quote is near 1–2s.
    recommended = next(i for i in items if i.get("recommended"))
    assert recommended["frame_ts"] == pytest.approx(12.0)
    assert recommended["category"] == "recommended"
    assert any(i.get("category") == "other_person" for i in items)
    assert any(i.get("category") == "group_panel" for i in items)
    assert all(i.get("selected") is False for i in items)
    assert all(i.get("preview_url") and "cache_only=1" in i["preview_url"] for i in items)


def test_catalog_persistence_reuses_fingerprint(tmp_path, monkeypatch):
    from app.search import carousel_identity_catalog as cat

    fid = "persist-vid"
    root = tmp_path / "thumbs"
    frame_dir = root / "video" / fid
    frame_dir.mkdir(parents=True)
    for ts in (1.0, 2.0, 3.0):
        (frame_dir / f"{ts:.3f}.jpg").write_bytes(b"x")

    calls = {"n": 0}

    def fake_scan(*args, **kwargs):
        calls["n"] += 1
        return {
            "version": cat.IDENTITY_CATALOG_VERSION,
            "drive_file_id": fid,
            "fingerprint": kwargs.get("fingerprint") or "x",
            "frames_scanned": 0,
            "identity_count": 0,
            "identities": [],
            "group_frames": [],
            "elapsed_ms": 1,
        }

    # Exercise real persistence path with empty detection (no face engine).
    monkeypatch.setattr(
        "app.faces.engine.get_face_engine",
        lambda: (_ for _ in ()).throw(RuntimeError("no engine")),
    )
    first = cat.build_identity_catalog(thumbnail_dir=str(root), drive_file_id=fid)
    assert first["version"] == cat.IDENTITY_CATALOG_VERSION
    path = cat.catalog_path(str(root), fid)
    assert path.is_file()
    second = cat.build_identity_catalog(thumbnail_dir=str(root), drive_file_id=fid)
    assert second == first


def test_natural_hdr_cache_and_cache_only_url(tmp_path):
    from app.video.frame_enhance import (
        enhance_frame_natural_hdr,
        ensure_hdr_variant,
        hdr_variant_path,
    )

    # Minimal valid JPEG via OpenCV when available; otherwise skip enhance assert.
    try:
        import cv2

        arr = np.zeros((64, 64, 3), dtype=np.uint8)
        arr[:, :] = (40, 60, 90)
        ok, buf = cv2.imencode(".jpg", arr)
        assert ok
        jpeg = buf.tobytes()
    except Exception:
        pytest.skip("OpenCV unavailable")

    enhanced = enhance_frame_natural_hdr(jpeg)
    assert enhanced and enhanced != jpeg

    source = tmp_path / "1.000.jpg"
    source.write_bytes(jpeg)
    variant = tmp_path / "hdr" / "1.000.jpg"
    built = ensure_hdr_variant(source, variant)
    assert built is not None and built.is_file()

    # Second call reuses cache.
    again = ensure_hdr_variant(source, variant)
    assert again == built

    thumb = tmp_path / "thumbs"
    fid = "hdr-vid"
    src = thumb / "video" / fid / "2.000.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(jpeg)
    path = hdr_variant_path(str(thumb), fid, 2.0)
    assert "hdr" in str(path)
    from app.video.frame_enhance import ensure_hdr_for_timestamp

    result = ensure_hdr_for_timestamp(str(thumb), fid, 2.0)
    assert result["ok"] is True
    assert Path(result["variant"]).is_file()


def test_apply_identity_selection_text_first(tmp_path, monkeypatch):
    from app.search import carousel_identity_catalog as cat

    root = tmp_path / "thumbs"
    for ts in (1.0, 12.0, 2.0, 20.0, 5.0, 30.0):
        path = root / "video" / "vid-1" / f"{ts:.3f}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")

    monkeypatch.setattr(
        cat,
        "load_or_build_identity_catalog",
        lambda **kwargs: _catalog_fixture(),
    )
    monkeypatch.setattr(
        "app.video.frame_enhance.ensure_hdr_for_timestamp",
        lambda *a, **k: {"ok": True},
    )

    slides = [
        {
            "index": 0,
            "timestamp_sec": 1.0,
            "end_timestamp_sec": 2.5,
            "drive_file_id": "vid-1",
            "hook_line": "hello",
        }
    ]
    out, summary = cat.apply_identity_selection_to_slides(
        slides,
        thumbnail_dir=str(root),
        drive_file_id="vid-1",
        prefer_hdr=True,
    )
    assert out[0]["preview_url"] is None
    assert out[0]["frame_ts"] is None
    items = out[0]["frame_candidate_items"]
    assert items
    assert any(i.get("recommended") for i in items)
    assert all(i.get("selected") is False for i in items)
    assert all(i.get("preview_url") and "cache_only=1" in i["preview_url"] for i in items)
    assert summary["algorithm"] == "identity-v1"
