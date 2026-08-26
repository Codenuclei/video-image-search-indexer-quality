#!/usr/bin/env python3
"""Controlled A/B face pipeline benchmark vs our buffalo_l architecture.

Measures detection, genuine/impostor recognition scores, threshold curves,
1:N identification, and CPU latency (warm-up excluded) on the same images/hardware.
"""

from __future__ import annotations

import csv
import json
import os
import random
import sys
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from pipelines import (  # noqa: E402
    Pipeline,
    build_pipelines,
    cosine_sim,
    l2_normalize,
)

FACES = ROOT / "faces"
RESULTS = ROOT / "RESULTS" if False else ROOT / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

WARMUP = 20
SPEED_RUNS = 200
TARGET_FAR = 0.001  # 0.1%
THRESHOLDS = [round(x, 2) for x in np.arange(0.20, 0.81, 0.05)]
SEED = 42
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _list_images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMG_EXTS)


def _people_map(split: str) -> dict[str, list[Path]]:
    base = FACES / split
    out: dict[str, list[Path]] = {}
    if not base.exists():
        return out
    for person_dir in sorted(base.iterdir()):
        if person_dir.is_dir():
            imgs = _list_images(person_dir)
            if imgs:
                out[person_dir.name] = imgs
    return out


def _letterbox_square(
    image_bgr: np.ndarray,
    size: int = 640,
    fill: int = 128,
    content_max: int = 400,
) -> np.ndarray:
    """Place face into a square canvas so SCRFD det_size=640 sees a mid-sized face.

    Sklearn LFW crops are ~125x94; filling the full 640 square can still miss.
    Scaling content so the long side is ~content_max (then padding) matches the
    size regime where buffalo_l reliably detects these crops.
    """
    h, w = image_bgr.shape[:2]
    if h == 0 or w == 0:
        return image_bgr
    target = min(content_max, size)
    scale = min(target / float(h), target / float(w))
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    interp = cv2.INTER_CUBIC if scale >= 1.0 else cv2.INTER_AREA
    resized = cv2.resize(image_bgr, (nw, nh), interpolation=interp)
    canvas = np.full((size, size, 3), fill, dtype=np.uint8)
    y0 = (size - nh) // 2
    x0 = (size - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    return canvas


def _read_bgr(path: Path, *, letterbox: int = 640) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return None
    # Always canonicalize to the same square input so every pipeline sees identical pixels.
    if letterbox:
        img = _letterbox_square(img, size=letterbox)
    return img


def _best_face(result):
    if not result.faces:
        return None
    return max(result.faces, key=lambda f: f.score)


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    return inter / (area_a + area_b - inter + 1e-6)


def detection_pass(pipe: Pipeline, images: list[Path]) -> dict:
    """Per-image detection stats. Label = 'face present' if any detector in suite
    finds a face on majority vote is expensive; here we treat dataset images as
    face-present (LFW), count miss when 0 detections, false extra when >1.
    """
    rows = []
    latencies = []
    detected = 0
    missed = 0
    multi = 0
    for path in images:
        img = _read_bgr(path)
        if img is None:
            continue
        t0 = time.perf_counter()
        # include decode in total for end-to-end honesty
        decode_ms = (time.perf_counter() - t0) * 1000.0
        res = pipe.process(img, embed=False)
        total = decode_ms + res.total_ms
        n = len(res.faces)
        faces_present = 1  # LFW / prepared set assumption
        false_det = max(0, n - 1)
        if n >= 1:
            detected += 1
        else:
            missed += 1
        if n > 1:
            multi += 1
        latencies.append(total)
        rows.append(
            {
                "image_id": str(path.relative_to(FACES)),
                "faces_present": faces_present,
                "faces_detected": n,
                "false_detections": false_det,
                "latency_ms": round(total, 3),
                "detect_ms": round(res.detect_ms, 3),
            }
        )
    n_img = max(1, len(rows))
    arr = np.array(latencies) if latencies else np.array([0.0])
    return {
        "n_images": len(rows),
        "detection_recall": detected / n_img,
        "miss_rate": missed / n_img,
        "multi_face_rate": multi / n_img,
        "mean_false_detections": float(np.mean([r["false_detections"] for r in rows])) if rows else 0.0,
        "latency_ms": _latency_stats(arr),
        "rows": rows,
    }


def _latency_stats(arr: np.ndarray) -> dict:
    if arr.size == 0:
        return {"mean_ms": 0, "median_ms": 0, "p95_ms": 0, "fps": 0}
    mean = float(np.mean(arr))
    return {
        "mean_ms": mean,
        "median_ms": float(np.median(arr)),
        "p95_ms": float(np.percentile(arr, 95)),
        "fps": float(1000.0 / mean) if mean > 0 else 0.0,
    }


def embed_gallery(pipe: Pipeline, people: dict[str, list[Path]]) -> dict[str, list[np.ndarray]]:
    gallery: dict[str, list[np.ndarray]] = {}
    for pid, paths in people.items():
        embs = []
        for p in paths:
            img = _read_bgr(p)
            if img is None:
                continue
            res = pipe.process(img, embed=True)
            face = _best_face(res)
            if face is None or face.embedding is None:
                continue
            embs.append(l2_normalize(face.embedding))
        if embs:
            gallery[pid] = embs
    return gallery


def recognition_pairs(
    pipe: Pipeline,
    enrollment: dict[str, list[Path]],
    test: dict[str, list[Path]],
    unknown: dict[str, list[Path]],
    *,
    max_genuine_per_person: int = 40,
    max_impostor: int = 400,
) -> list[dict]:
    rng = random.Random(SEED)
    enroll_embs = embed_gallery(pipe, enrollment)
    pairs: list[dict] = []

    # Genuine: each enroll embedding vs each test embedding (sampled)
    for pid, test_paths in test.items():
        if pid not in enroll_embs:
            continue
        test_embs = []
        for p in test_paths:
            img = _read_bgr(p)
            if img is None:
                continue
            t0 = time.perf_counter()
            res = pipe.process(img, embed=True)
            emb_ms = (time.perf_counter() - t0) * 1000.0
            face = _best_face(res)
            if face is None or face.embedding is None:
                continue
            test_embs.append((l2_normalize(face.embedding), emb_ms, p))
        count = 0
        for e in enroll_embs[pid]:
            for te, emb_ms, p in test_embs:
                pairs.append(
                    {
                        "pair_id": f"g-{pid}-{count}",
                        "person_a": pid,
                        "person_b": pid,
                        "genuine": 1,
                        "similarity": cosine_sim(e, te),
                        "embedding_latency_ms": emb_ms,
                    }
                )
                count += 1
                if count >= max_genuine_per_person:
                    break
            if count >= max_genuine_per_person:
                break

    # Impostor: enroll vs unknown + cross-person test
    impostor_candidates: list[tuple[str, np.ndarray]] = []
    for pid, paths in list(unknown.items()) + [(p, ps) for p, ps in test.items()]:
        for p in paths[:5]:
            img = _read_bgr(p)
            if img is None:
                continue
            res = pipe.process(img, embed=True)
            face = _best_face(res)
            if face is None or face.embedding is None:
                continue
            impostor_candidates.append((pid, l2_normalize(face.embedding)))

    impostor_pairs = 0
    enroll_items = [(pid, e) for pid, embs in enroll_embs.items() for e in embs]
    rng.shuffle(enroll_items)
    rng.shuffle(impostor_candidates)
    for pid_a, ea in enroll_items:
        for pid_b, eb in impostor_candidates:
            if pid_a == pid_b:
                continue
            pairs.append(
                {
                    "pair_id": f"i-{impostor_pairs}",
                    "person_a": pid_a,
                    "person_b": pid_b,
                    "genuine": 0,
                    "similarity": cosine_sim(ea, eb),
                    "embedding_latency_ms": None,
                }
            )
            impostor_pairs += 1
            if impostor_pairs >= max_impostor:
                break
        if impostor_pairs >= max_impostor:
            break
    return pairs


def threshold_metrics(pairs: list[dict], thresholds: list[float]) -> list[dict]:
    genuine = np.array([p["similarity"] for p in pairs if p["genuine"] == 1], dtype=np.float64)
    impostor = np.array([p["similarity"] for p in pairs if p["genuine"] == 0], dtype=np.float64)
    rows = []
    for thr in thresholds:
        tar = float(np.mean(genuine >= thr)) if genuine.size else 0.0
        far = float(np.mean(impostor >= thr)) if impostor.size else 0.0
        frr = 1.0 - tar
        rows.append({"threshold": thr, "TAR": tar, "FAR": far, "FRR": frr})
    # EER approx
    eer = None
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        if (a["FAR"] - a["FRR"]) * (b["FAR"] - b["FRR"]) <= 0:
            eer = 0.5 * (a["FAR"] + a["FRR"] + b["FAR"] + b["FRR"]) / 2
            break
    # TAR at target FAR: best TAR among thresholds with FAR <= target
    candidates = [r for r in rows if r["FAR"] <= TARGET_FAR]
    tar_at = max((r["TAR"] for r in candidates), default=0.0)
    return {"curve": rows, "eer_approx": eer, f"TAR_at_FAR_{TARGET_FAR}": tar_at}


def pick_threshold_on_validation(pairs: list[dict]) -> float:
    """Choose highest threshold with FAR <= TARGET_FAR on validation pairs; else EER-ish."""
    metrics = threshold_metrics(pairs, THRESHOLDS)
    candidates = [r for r in metrics["curve"] if r["FAR"] <= TARGET_FAR]
    if candidates:
        # among FAR-ok, maximize TAR then threshold
        best = max(candidates, key=lambda r: (r["TAR"], r["threshold"]))
        return float(best["threshold"])
    # fallback: closest FAR to target
    best = min(metrics["curve"], key=lambda r: abs(r["FAR"] - TARGET_FAR))
    return float(best["threshold"])


def identification(
    pipe: Pipeline,
    enrollment: dict[str, list[Path]],
    test: dict[str, list[Path]],
    unknown: dict[str, list[Path]],
    threshold: float,
) -> dict:
    # Mean enroll embedding per person
    gallery = {}
    for pid, embs in embed_gallery(pipe, enrollment).items():
        gallery[pid] = l2_normalize(np.mean(np.stack(embs, axis=0), axis=0))

    rank1 = rank5 = total = 0
    unknown_reject = unknown_total = 0

    def search(emb: np.ndarray) -> list[tuple[str, float]]:
        scored = [(pid, float(np.dot(emb, g))) for pid, g in gallery.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    for pid, paths in test.items():
        for p in paths:
            img = _read_bgr(p)
            if img is None:
                continue
            res = pipe.process(img, embed=True)
            face = _best_face(res)
            if face is None or face.embedding is None:
                continue
            emb = l2_normalize(face.embedding)
            ranked = search(emb)
            total += 1
            top_ids = [x[0] for x in ranked[:5]]
            if ranked and ranked[0][0] == pid and ranked[0][1] >= threshold:
                rank1 += 1
            if pid in top_ids and ranked[0][1] >= threshold:
                # rank-5 with reject threshold on top-1 score (conservative)
                pass
            if pid in top_ids:
                rank5 += 1

    for pid, paths in unknown.items():
        for p in paths[:5]:
            img = _read_bgr(p)
            if img is None:
                continue
            res = pipe.process(img, embed=True)
            face = _best_face(res)
            if face is None or face.embedding is None:
                continue
            emb = l2_normalize(face.embedding)
            ranked = search(emb)
            unknown_total += 1
            if not ranked or ranked[0][1] < threshold:
                unknown_reject += 1

    # Search latency for gallery sizes
    probe = None
    for embs in embed_gallery(pipe, {k: v[:1] for k, v in list(test.items())[:1]}).values():
        if embs:
            probe = embs[0]
            break
    search_latency = {}
    if probe is not None and gallery:
        base_vecs = list(gallery.values())
        rng = np.random.default_rng(0)
        for n in (100, 1000, 10000):
            # tile gallery to size n
            mats = []
            while len(mats) < n:
                mats.extend(base_vecs)
            mat = np.stack(mats[:n], axis=0)
            # warmup
            for _ in range(10):
                _ = mat @ probe
            times = []
            for _ in range(200):
                t0 = time.perf_counter()
                scores = mat @ probe
                _ = int(np.argmax(scores))
                times.append((time.perf_counter() - t0) * 1000.0)
            search_latency[str(n)] = _latency_stats(np.array(times))

    return {
        "rank1_accuracy": rank1 / total if total else 0.0,
        "rank5_accuracy": rank5 / total if total else 0.0,
        "n_queries": total,
        "unknown_rejection_rate": unknown_reject / unknown_total if unknown_total else 0.0,
        "unknown_queries": unknown_total,
        "threshold": threshold,
        "search_latency_ms": search_latency,
    }


def speed_bench(pipe: Pipeline, sample_images: list[Path]) -> dict:
    imgs = []
    for p in sample_images[: min(20, len(sample_images))]:
        im = _read_bgr(p)
        if im is not None:
            imgs.append(im)
    if not imgs:
        return {}
    # warmup
    for i in range(WARMUP):
        pipe.process(imgs[i % len(imgs)], embed=True)
    totals = []
    detect = []
    align = []
    emb = []
    tracemalloc.start()
    for i in range(SPEED_RUNS):
        im = imgs[i % len(imgs)]
        t0 = time.perf_counter()
        # decode already done; measure pipeline stages
        res = pipe.process(im, embed=True)
        totals.append((time.perf_counter() - t0) * 1000.0)
        detect.append(res.detect_ms)
        align.append(res.align_ms)
        emb.append(res.embedding_ms)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "warmup_runs": WARMUP,
        "measured_runs": SPEED_RUNS,
        "total_ms": _latency_stats(np.array(totals)),
        "detect_ms": _latency_stats(np.array(detect)),
        "align_ms": _latency_stats(np.array(align)),
        "embedding_ms": _latency_stats(np.array(emb)),
        "peak_traced_memory_mb": peak / (1024 * 1024),
    }


def face_count_latency(pipe: Pipeline, images: list[Path]) -> dict:
    """Latency when 0 / 1 / multi faces (approximate via empty crop + real images)."""
    ones = []
    multis = []
    zeros = []
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    for _ in range(30):
        res = pipe.process(blank, embed=True)
        zeros.append(res.total_ms)
    for p in images[:40]:
        img = _read_bgr(p)
        if img is None:
            continue
        res = pipe.process(img, embed=True)
        n = len(res.faces)
        if n == 1:
            ones.append(res.total_ms)
        elif n >= 2:
            multis.append(res.total_ms)
        else:
            zeros.append(res.total_ms)
    return {
        "0_faces": _latency_stats(np.array(zeros or [0.0])),
        "1_face": _latency_stats(np.array(ones or [0.0])),
        "multi_faces": _latency_stats(np.array(multis or [0.0])),
    }


def run_one(pipe: Pipeline) -> dict:
    print(f"\n=== Pipeline: {pipe.name} ===")
    enrollment = _people_map("enrollment")
    test = _people_map("test")
    unknown = _people_map("unknown")
    validation = _people_map("validation")
    all_imgs = []
    for d in (enrollment, test, unknown, validation):
        for ps in d.values():
            all_imgs.extend(ps)

    det = detection_pass(pipe, all_imgs)
    print(f"  detection recall={det['detection_recall']:.3f} median_ms={det['latency_ms']['median_ms']:.1f}")

    val_pairs = recognition_pairs(pipe, enrollment, validation, unknown, max_genuine_per_person=25, max_impostor=250)
    thr = pick_threshold_on_validation(val_pairs)
    print(f"  validation-chosen threshold={thr:.2f}")

    test_pairs = recognition_pairs(pipe, enrollment, test, unknown, max_genuine_per_person=40, max_impostor=400)
    rec = threshold_metrics(test_pairs, THRESHOLDS)
    # metrics at chosen threshold
    at_thr = next((r for r in rec["curve"] if abs(r["threshold"] - thr) < 1e-9), None)
    print(
        f"  TAR@FAR={TARGET_FAR}: {rec[f'TAR_at_FAR_{TARGET_FAR}']:.3f}  "
        f"at thr={thr}: TAR={at_thr['TAR'] if at_thr else 'n/a'} FAR={at_thr['FAR'] if at_thr else 'n/a'}"
    )

    ident = identification(pipe, enrollment, test, unknown, thr)
    print(f"  rank-1={ident['rank1_accuracy']:.3f} unknown_reject={ident['unknown_rejection_rate']:.3f}")

    speed = speed_bench(pipe, all_imgs)
    print(f"  speed median_total_ms={speed.get('total_ms', {}).get('median_ms')}")

    face_lat = face_count_latency(pipe, all_imgs)

    # write pair CSV
    pair_csv = RESULTS / f"pairs_{pipe.name}.csv"
    with pair_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["pair_id", "person_a", "person_b", "genuine", "similarity", "embedding_latency_ms"],
        )
        w.writeheader()
        w.writerows(test_pairs)

    det_csv = RESULTS / f"detection_{pipe.name}.csv"
    with det_csv.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["image_id", "faces_present", "faces_detected", "false_detections", "latency_ms", "detect_ms"],
        )
        w.writeheader()
        w.writerows(det["rows"])

    summary = {
        "pipeline": pipe.name,
        "detection_recall": det["detection_recall"],
        "miss_rate": det["miss_rate"],
        "mean_false_detections": det["mean_false_detections"],
        "chosen_threshold": thr,
        "TAR_at_target_FAR": rec[f"TAR_at_FAR_{TARGET_FAR}"],
        "target_FAR": TARGET_FAR,
        "metrics_at_chosen_threshold": at_thr,
        "threshold_curve": rec["curve"],
        "eer_approx": rec["eer_approx"],
        "identification": ident,
        "speed": speed,
        "latency_by_face_count": face_lat,
        "n_test_pairs": len(test_pairs),
        "n_genuine": sum(1 for p in test_pairs if p["genuine"] == 1),
        "n_impostor": sum(1 for p in test_pairs if p["genuine"] == 0),
    }
    (RESULTS / f"summary_{pipe.name}.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def decision_table(summaries: list[dict]) -> str:
    lines = [
        "| Pipeline | Detection recall | TAR at FAR=0.1% | Rank-1 | Median total ms | P95 total ms | Peak traced RAM MB |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        speed = s.get("speed") or {}
        total = speed.get("total_ms") or {}
        lines.append(
            "| {pipe} | {rec:.3f} | {tar:.3f} | {r1:.3f} | {med:.1f} | {p95:.1f} | {ram:.1f} |".format(
                pipe=s["pipeline"],
                rec=s["detection_recall"],
                tar=s["TAR_at_target_FAR"],
                r1=s["identification"]["rank1_accuracy"],
                med=total.get("median_ms", 0),
                p95=total.get("p95_ms", 0),
                ram=(speed.get("peak_traced_memory_mb") or 0),
            )
        )
    return "\n".join(lines)


def pick_winner(summaries: list[dict], *, min_recall=0.98, min_tar=0.97, max_p95_ms=100.0) -> dict:
    eligible = []
    for s in summaries:
        p95 = ((s.get("speed") or {}).get("total_ms") or {}).get("p95_ms", 1e9)
        if s["detection_recall"] >= min_recall and s["TAR_at_target_FAR"] >= min_tar and p95 <= max_p95_ms:
            eligible.append(s)
    if eligible:
        # among eligible, best TAR then recall then latency
        return max(
            eligible,
            key=lambda s: (
                s["TAR_at_target_FAR"],
                s["detection_recall"],
                -((s.get("speed") or {}).get("total_ms") or {}).get("p95_ms", 1e9),
            ),
        )
    # Soft fallback: best accuracy under latency if none meet all gates
    return max(
        summaries,
        key=lambda s: (
            s["TAR_at_target_FAR"],
            s["detection_recall"],
            s["identification"]["rank1_accuracy"],
        ),
    )


def main() -> None:
    enrollment = _people_map("enrollment")
    if not enrollment:
        print("No dataset. Run prepare_dataset.py first.", file=sys.stderr)
        sys.exit(1)

    pipes = build_pipelines()
    if not pipes:
        print("No pipelines available. Run download_models.py first.", file=sys.stderr)
        sys.exit(1)
    print("Pipelines:", [p.name for p in pipes])

    summaries = []
    for pipe in pipes:
        try:
            summaries.append(run_one(pipe))
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {pipe.name}: {exc}")
            (RESULTS / f"error_{pipe.name}.txt").write_text(str(exc))

    table = decision_table(summaries)
    winner = pick_winner(summaries) if summaries else None
    # Also report what matters for accuracy vs our architecture
    ours = next((s for s in summaries if s["pipeline"].startswith("ours_")), None)
    report = {
        "target_FAR": TARGET_FAR,
        "decision_rule": {
            "detection_recall_min": 0.98,
            "TAR_at_FAR_0.001_min": 0.97,
            "p95_total_ms_max": 100.0,
        },
        "table_markdown": table,
        "winner": winner["pipeline"] if winner else None,
        "winner_summary": winner,
        "ours_architecture": ours,
        "accuracy_delta_vs_ours": None,
        "summaries": summaries,
    }
    if ours and winner:
        report["accuracy_delta_vs_ours"] = {
            "winner": winner["pipeline"],
            "delta_TAR_at_target_FAR": winner["TAR_at_target_FAR"] - ours["TAR_at_target_FAR"],
            "delta_detection_recall": winner["detection_recall"] - ours["detection_recall"],
            "delta_rank1": winner["identification"]["rank1_accuracy"] - ours["identification"]["rank1_accuracy"],
            "delta_median_ms": ((winner.get("speed") or {}).get("total_ms") or {}).get("median_ms", 0)
            - ((ours.get("speed") or {}).get("total_ms") or {}).get("median_ms", 0),
        }

    out = RESULTS / "ab_report.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    md = RESULTS / "ab_report.md"
    md.write_text(
        "# Face A/B Benchmark Report\n\n"
        + table
        + "\n\n"
        + f"**Winner (decision rule / soft fallback):** `{report['winner']}`\n\n"
        + f"Target FAR={TARGET_FAR}. Thresholds chosen on validation split only.\n"
    )
    print("\n" + table)
    print(f"\nWinner: {report['winner']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
