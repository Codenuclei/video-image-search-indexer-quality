#!/usr/bin/env python3
"""Download OpenCV Zoo + InsightFace ONNX packs into face-ab-benchmark/models."""

from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
MODELS.mkdir(parents=True, exist_ok=True)

# OpenCV Zoo (https://huggingface.co/opencv/opencv_zoo)
OPENCV_MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
        "face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx"
    ),
}

# InsightFace buffalo packs (same CDN FaceAnalysis uses).
INSIGHTFACE_ZIPS = {
    "buffalo_s": "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip",
    # det_2.5g lives in antelopev2 / older packs; also ship standalone SCRFD-2.5G if present in buffalo_l siblings.
}


def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"OK exists: {dest.name}")
        return
    print(f"Downloading {url} -> {dest}")
    tmp = dest.with_suffix(dest.suffix + ".partial")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    print(f"  saved {dest.stat().st_size} bytes")


def _ensure_insightface_pack(name: str) -> Path:
    """Prefer ~/.insightface/models/{name}; else download zip into models/."""
    home = Path.home() / ".insightface" / "models" / name
    if home.is_dir() and any(home.glob("*.onnx")):
        link = MODELS / name
        if link.exists() or link.is_symlink():
            print(f"OK pack: {link}")
            return link
        try:
            link.symlink_to(home, target_is_directory=True)
        except OSError:
            shutil.copytree(home, link, dirs_exist_ok=True)
        print(f"Linked/copied {home} -> {link}")
        return link

    zip_url = INSIGHTFACE_ZIPS.get(name)
    if not zip_url:
        raise FileNotFoundError(f"InsightFace pack {name} not found locally and no download URL")
    zip_path = MODELS / f"{name}.zip"
    _download(zip_url, zip_path)
    out = MODELS / name
    if not out.exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(MODELS)
        # zip may extract as buffalo_s/ or flat
        if not out.exists():
            # find folder with onnx
            candidates = [p for p in MODELS.iterdir() if p.is_dir() and list(p.glob("*.onnx"))]
            for c in candidates:
                if name in c.name:
                    if c != out:
                        c.rename(out)
                    break
    print(f"OK pack: {out}")
    return out


def _download_scrfd_25g() -> None:
    """SCRFD-2.5GF detector used in some InsightFace packs."""
    dest = MODELS / "det_2.5g.onnx"
    if dest.exists():
        print(f"OK exists: {dest.name}")
        return
    # Known release asset used by community / insightface model zoo mirrors.
    urls = [
        "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",  # has det_10g only
    ]
    # Prefer onnx from insightface model zoo raw (det_2.5g).
    alt = [
        "https://huggingface.co/public-data/insightface/resolve/main/models/buffalo_l/det_10g.onnx",
    ]
    # Use buffalo_sc / antelope if available via FaceAnalysis bootstrap.
    try:
        from insightface.utils import ensure_available

        # ensure_available downloads pack into ~/.insightface
        path = ensure_available("models", "buffalo_sc", root=str(Path.home() / ".insightface"))
        print(f"buffalo_sc via insightface: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"buffalo_sc optional skip: {exc}")

    # Standalone SCRFD 2.5G from onnx model zoo style CDN used by onnxruntime-extensions demos.
    scrfd_urls = [
        "https://github.com/onnx/models/raw/main/validated/vision/body_analysis/ultraface/models/version-RFB-320.onnx",
    ]
    # Better: InsightFace model zoo ONNX hosted on ghproxy-less GitHub raw for det_2.5g
    # from buffalo pack variants. Download antelopev2 which includes det_2.5g historically.
    antelope = {
        "url": "https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip",
        "member_hints": ("det_2.5g.onnx", "scrfd_2.5g.onnx"),
    }
    zip_path = MODELS / "antelopev2.zip"
    try:
        _download(antelope["url"], zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            hit = next((n for n in names if n.endswith("det_2.5g.onnx")), None)
            if hit:
                with zf.open(hit) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"Extracted {hit} -> {dest}")
                return
            print("antelopev2.zip members:", names[:20])
    except Exception as exc:  # noqa: BLE001
        print(f"antelopev2 download failed: {exc}")

    # Fallback: use buffalo_l det_10g as SCRFD-family heavy detector reference only
    # (already compared via our architecture). Mark missing.
    print("WARN: det_2.5g.onnx not available; SCRFD-2.5GF pipelines will be skipped.")


def main() -> None:
    for name, url in OPENCV_MODELS.items():
        _download(url, MODELS / name)

    # Our architecture pack (already local for this Mac).
    _ensure_insightface_pack("buffalo_l")
    # SCRFD-0.5GF (det_500m) + MobileFaceNet (w600k_mbf)
    try:
        _ensure_insightface_pack("buffalo_s")
    except Exception as exc:  # noqa: BLE001
        print(f"buffalo_s download failed: {exc}")
        # FaceAnalysis will download on first use if network works
        try:
            from insightface.app import FaceAnalysis

            app = FaceAnalysis(name="buffalo_s", root=str(Path.home() / ".insightface"), providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            _ensure_insightface_pack("buffalo_s")
        except Exception as exc2:  # noqa: BLE001
            print(f"buffalo_s FaceAnalysis bootstrap failed: {exc2}")

    _download_scrfd_25g()

    print("\nModels directory:")
    for p in sorted(MODELS.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(MODELS)}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
