#!/usr/bin/env python3
"""Build enrollment / test / unknown / validation face folders for A/B.

Quick default (matches the user's first-test guidance):
  10 enrolled people, up to 5 enrollment + 20 test images each,
  20 unknown people (impostors), plus a held-out validation split for thresholds.

Uses sklearn LFW if available; otherwise downloads a small public face sample set.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACES = ROOT / "faces"
RESULTS = ROOT / "results"

N_ENROLLED = 10
N_UNKNOWN = 20
N_ENROLL_IMGS = 5
N_TEST_IMGS = 20
N_VAL_IMGS = 5  # held-out genuine images for threshold selection
SEED = 42


def _clear_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _from_sklearn_lfw() -> dict[str, list[Path]] | None:
    try:
        from sklearn.datasets import fetch_lfw_people
    except Exception:
        return None

    print("Fetching LFW via sklearn (may download ~200MB once)...")
    # color=True, resize closer to native for fair detection
    bundle = fetch_lfw_people(
        min_faces_per_person=25,
        resize=1.0,
        color=True,
        download_if_missing=True,
    )
    # sklearn returns RGB float images; write JPEGs into a staging dir
    import cv2
    import numpy as np

    staging = FACES / "_lfw_staging"
    _clear_tree(staging)
    by_person: dict[str, list[Path]] = defaultdict(list)
    target_names = list(bundle.target_names)
    for i, (img, y) in enumerate(zip(bundle.images, bundle.target, strict=True)):
        name = target_names[int(y)].replace(" ", "_")
        person_dir = staging / name
        person_dir.mkdir(parents=True, exist_ok=True)
        # sklearn LFW is float 0..1 RGB
        bgr = cv2.cvtColor((np.clip(img, 0, 1) * 255).astype("uint8"), cv2.COLOR_RGB2BGR)
        out = person_dir / f"{i:05d}.jpg"
        cv2.imwrite(str(out), bgr)
        by_person[name].append(out)
    print(f"LFW people with >=25 faces: {len(by_person)}")
    return dict(by_person)


def _from_github_sample() -> dict[str, list[Path]]:
    """Fallback: small multi-person set if sklearn LFW is unavailable."""
    url = "https://github.com/ageitgey/face_recognition/raw/master/examples/knn_examples.zip"
    # That zip may not exist; use a known small alternative — download individual LFW-style
    # images from a curated list is fragile. Prefer insightface test images + duplicate structure.
    print("sklearn LFW unavailable; downloading OpenCV sample faces + synthetic multi-crop fallback is weak.")
    print("Attempting LFW.tgz people-A subset via official mirror...")
    # Official LFW all images is large; use funneled subset via academic torrent-less HTTP
    lfw_url = "http://vis-www.cs.umass.edu/lfw/lfw.tgz"
    tgz = FACES / "lfw.tgz"
    if not tgz.exists():
        print(f"Downloading {lfw_url} (this is ~170MB)...")
        urllib.request.urlretrieve(lfw_url, tgz)
    import tarfile

    extract = FACES / "_lfw_raw"
    if not extract.exists():
        extract.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz, "r:gz") as tar:
            tar.extractall(extract)
    # structure: lfw/Person_Name/*.jpg
    root = extract / "lfw"
    if not root.exists():
        # sometimes extracts as faces/_lfw_raw/Person
        candidates = [p for p in extract.rglob("*") if p.is_dir() and list(p.glob("*.jpg"))]
        by_person: dict[str, list[Path]] = {}
        for p in candidates:
            jpgs = sorted(p.glob("*.jpg"))
            if len(jpgs) >= 5:
                by_person[p.name] = jpgs
        return by_person
    by_person = {}
    for person_dir in sorted(root.iterdir()):
        if not person_dir.is_dir():
            continue
        jpgs = sorted(person_dir.glob("*.jpg"))
        if len(jpgs) >= 5:
            by_person[person_dir.name] = jpgs
    return by_person


def _split_people(by_person: dict[str, list[Path]]) -> None:
    rng = random.Random(SEED)
    # Prefer people with enough images
    eligible = [(n, imgs) for n, imgs in by_person.items() if len(imgs) >= (N_ENROLL_IMGS + N_TEST_IMGS + N_VAL_IMGS)]
    if len(eligible) < N_ENROLLED + N_UNKNOWN:
        # relax: enrollment+test only, validation from enrollment leftover
        eligible = [(n, imgs) for n, imgs in by_person.items() if len(imgs) >= (N_ENROLL_IMGS + max(5, N_TEST_IMGS // 2))]
    if len(eligible) < N_ENROLLED + 5:
        raise RuntimeError(
            f"Not enough people with enough images: have {len(eligible)}, "
            f"need ~{N_ENROLLED + N_UNKNOWN}. Populate faces/ manually."
        )

    rng.shuffle(eligible)
    enrolled = eligible[:N_ENROLLED]
    unknown = eligible[N_ENROLLED : N_ENROLLED + N_UNKNOWN]
    if len(unknown) < N_UNKNOWN:
        # use remaining shorter people as unknowns
        used = {n for n, _ in enrolled + unknown}
        extras = [(n, imgs) for n, imgs in by_person.items() if n not in used and len(imgs) >= 3]
        rng.shuffle(extras)
        unknown = unknown + extras[: max(0, N_UNKNOWN - len(unknown))]

    for split in ("enrollment", "test", "unknown", "validation"):
        _clear_tree(FACES / split)

    manifest = {"seed": SEED, "enrolled": [], "unknown": [], "counts": {}}

    for idx, (name, imgs) in enumerate(enrolled, start=1):
        imgs = list(imgs)
        rng.shuffle(imgs)
        need = N_ENROLL_IMGS + N_TEST_IMGS + N_VAL_IMGS
        if len(imgs) < need:
            # shrink test
            n_test = max(5, len(imgs) - N_ENROLL_IMGS - 2)
            n_val = max(1, len(imgs) - N_ENROLL_IMGS - n_test)
        else:
            n_test, n_val = N_TEST_IMGS, N_VAL_IMGS
        enroll_imgs = imgs[:N_ENROLL_IMGS]
        test_imgs = imgs[N_ENROLL_IMGS : N_ENROLL_IMGS + n_test]
        val_imgs = imgs[N_ENROLL_IMGS + n_test : N_ENROLL_IMGS + n_test + n_val]
        pid = f"person_{idx:03d}"
        for dest_split, batch in (
            ("enrollment", enroll_imgs),
            ("test", test_imgs),
            ("validation", val_imgs),
        ):
            dest = FACES / dest_split / pid
            dest.mkdir(parents=True, exist_ok=True)
            for j, src in enumerate(batch):
                shutil.copy2(src, dest / f"{j:03d}{src.suffix.lower()}")
        manifest["enrolled"].append(
            {
                "id": pid,
                "source_name": name,
                "enrollment": len(enroll_imgs),
                "test": len(test_imgs),
                "validation": len(val_imgs),
            }
        )

    for idx, (name, imgs) in enumerate(unknown, start=101):
        imgs = list(imgs)
        rng.shuffle(imgs)
        pid = f"person_{idx:03d}"
        dest = FACES / "unknown" / pid
        dest.mkdir(parents=True, exist_ok=True)
        for j, src in enumerate(imgs[: max(5, min(20, len(imgs)))]):
            shutil.copy2(src, dest / f"{j:03d}{src.suffix.lower()}")
        manifest["unknown"].append({"id": pid, "source_name": name, "images": len(list(dest.glob('*')))})

    manifest["counts"] = {
        "enrolled_people": len(manifest["enrolled"]),
        "unknown_people": len(manifest["unknown"]),
        "enrollment_images": sum(p["enrollment"] for p in manifest["enrolled"]),
        "test_images": sum(p["test"] for p in manifest["enrolled"]),
        "validation_images": sum(p["validation"] for p in manifest["enrolled"]),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["counts"], indent=2))
    print(f"Wrote {RESULTS / 'dataset_manifest.json'}")


def main() -> None:
    FACES.mkdir(parents=True, exist_ok=True)
    by_person = _from_sklearn_lfw()
    if not by_person:
        by_person = _from_github_sample()
    _split_people(by_person)
    # cleanup staging optionally kept for reruns
    print("Dataset ready under", FACES)


if __name__ == "__main__":
    main()
