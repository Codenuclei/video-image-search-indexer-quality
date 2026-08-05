#!/usr/bin/env python3
"""Production reliability probe: live Drive images → batchEmbedContents.

Pulls N real IMAGE files from Postgres ``drive_files`` (diverse parent paths under
the connected Prospectus / selected Drive root), downloads full bytes via
DriveDirectClient (NOT thumbnails), converts to JPEG the same way production
indexing does (``decode_image_bgr`` + cv2 JPEG q=85), optionally downscales
longest edge (``--max-edge``, default 1280) for embed payload size, then embeds
via google-genai ``models.embed_content`` → REST ``:batchEmbedContents``.

Compares a sample against production ``embed_frame_sync`` (same vectors).

Does NOT reindex, wipe, upsert Qdrant, or deploy.

Usage (from backend/ with venv + backend/.env):
  .venv/bin/python scripts/test_batch_embed_drive_images.py
  .venv/bin/python scripts/test_batch_embed_drive_images.py --n 100 --batch-size 5 --parallel 20 --max-edge 1024
  .venv/bin/python scripts/test_batch_embed_drive_images.py --reuse-dir /tmp/batch_embed_drive_100 --max-edge 1024
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import json
import math
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.chdir(BACKEND_DIR)

MODEL = "gemini-embedding-2"
DIM = 3072
# Match production image.py JPEG encode quality.
JPEG_QUALITY = 85


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _max_abs_diff(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return float("inf")
    return max(abs(x - y) for x, y in zip(a, b))


async def select_diverse_images(
    n: int,
    *,
    max_bytes: int,
    path_ilike: str,
) -> list[dict]:
    """Pick IMAGE rows from diverse parent folders under the connected Drive root."""
    from app.db.models import DriveUser
    from app.db.session import get_session_factory
    from app.pipelines.common import INDEXABLE_IMAGE_TYPES, is_image_mime

    sf = get_session_factory()
    async with sf() as session:
        user = (await session.execute(select(DriveUser).limit(1))).scalar_one_or_none()
        root_id = user.selected_folder_id if user else None
        root_name = user.selected_folder_name if user else None
        print(f"drive_user={user.email if user else None} root={root_name!r} id={root_id}")

        # Prefer common photo mimes; skip SVG / huge RAW for a reliable probe.
        mime_prefer = (
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/webp",
            "image/heic",
            "image/heif",
            "image/avif",
        )
        # Connected root is "Prospectus - Internal"; paths rarely contain that
        # string — prefer root_folder_id, with optional path ILIKE overlay.
        params: dict = {
            "mimes": list(mime_prefer),
            "max_bytes": max_bytes,
        }
        where = [
            "mime_type = ANY(:mimes)",
            "(size IS NULL OR (size > 2000 AND size <= :max_bytes))",
            "source = 'drive'",
        ]
        if root_id:
            where.append("(root_folder_id = :root_id OR root_folder_id IS NULL)")
            params["root_id"] = root_id
        if path_ilike and path_ilike not in ("%", "%%"):
            where.append("path ILIKE :path_pat")
            params["path_pat"] = path_ilike

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, name, mime_type, path, size, root_folder_id
                    FROM drive_files
                    WHERE {' AND '.join(where)}
                    ORDER BY path, id
                    """
                ),
                params,
            )
        ).mappings().all()

    by_parent: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = r["name"] or ""
        # Skip macOS AppleDouble / resource-fork junk that can't decode.
        if name.startswith("._") or name.startswith(".DS_Store"):
            continue
        path = r["path"] or "/"
        parent = path.rsplit("/", 1)[0] if "/" in path else path
        if not is_image_mime(r["mime_type"], name):
            continue
        mime = (r["mime_type"] or "").lower().split(";", 1)[0].strip()
        if mime not in INDEXABLE_IMAGE_TYPES and not mime.startswith("image/"):
            continue
        by_parent[parent].append(dict(r))

    if not by_parent:
        raise SystemExit(
            f"No IMAGE rows matched root={root_id!r} path_ilike={path_ilike!r} "
            f"(mime in jpeg/png/webp/heic/heif/avif, size<={max_bytes})"
        )

    # Round-robin across parent folders for diversity.
    parents = sorted(by_parent.keys(), key=lambda p: (-len(by_parent[p]), p))
    selected: list[dict] = []
    seen_ids: set[str] = set()
    idx = 0
    while len(selected) < n and parents:
        parent = parents[idx % len(parents)]
        bucket = by_parent[parent]
        if bucket:
            item = bucket.pop(0)
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                selected.append(item)
        if not bucket:
            parents = [p for p in parents if by_parent[p]]
            if not parents:
                break
            idx = 0
            continue
        idx += 1

    if len(selected) < n:
        print(
            f"WARN: only {len(selected)} diverse images available (wanted {n})",
            file=sys.stderr,
        )
    return selected


def _bgr_to_jpeg_bytes(image_bgr: np.ndarray, *, max_edge: int) -> bytes:
    """Encode BGR → JPEG; optionally downscale longest edge for embed payload size."""
    h, w = image_bgr.shape[:2]
    if max_edge > 0 and max(h, w) > max_edge:
        scale = max_edge / float(max(h, w))
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        image_bgr = cv2.resize(image_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(
        ".jpg",
        image_bgr,
        [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
    )
    if not ok:
        raise ValueError("cv2.imencode failed")
    return buf.tobytes()


async def download_and_convert(
    items: list[dict],
    out_dir: Path,
    *,
    download_parallel: int,
    max_edge: int,
) -> tuple[list[Path], list[dict], list[str]]:
    """Download full Drive bytes and write production-style JPEGs."""
    from app.dependencies import get_drive_client
    from app.pipelines.common import decode_image_bgr, download_to_memory

    client = get_drive_client()
    sem = asyncio.Semaphore(max(1, download_parallel))
    results_meta: list[dict | None] = [None] * len(items)
    results_path: list[Path | None] = [None] * len(items)
    results_err: list[str | None] = [None] * len(items)

    async def one_indexed(i: int, item: dict) -> None:
        fid = item["id"]
        name = item["name"] or fid
        async with sem:
            t0 = time.perf_counter()
            try:
                raw = await download_to_memory(client, fid)
                image_bgr = await asyncio.to_thread(
                    decode_image_bgr, raw, file_name=name
                )
                jpeg = await asyncio.to_thread(
                    _bgr_to_jpeg_bytes, image_bgr, max_edge=max_edge
                )
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in fid)[:40]
                dest = out_dir / f"{safe}.jpg"
                dest.write_bytes(jpeg)
                wall = time.perf_counter() - t0
                results_path[i] = dest
                results_meta[i] = {
                    "id": fid,
                    "name": name,
                    "path": item.get("path"),
                    "mime_type": item.get("mime_type"),
                    "raw_bytes": len(raw),
                    "jpeg_bytes": len(jpeg),
                    "download_wall_s": round(wall, 3),
                    "local_path": str(dest),
                }
                print(
                    f"  dl OK [{i+1}/{len(items)}] {name[:50]!r} "
                    f"raw={len(raw):,} jpeg={len(jpeg):,} in {wall:.1f}s"
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"{fid} ({name}): {exc}"[:400]
                results_err[i] = msg
                print(f"  dl FAIL [{i+1}/{len(items)}] {msg}", file=sys.stderr)

    await asyncio.gather(*(one_indexed(i, it) for i, it in enumerate(items)))

    paths = [p for p in results_path if p is not None]
    metas = [m for m in results_meta if m is not None]
    errs = [e for e in results_err if e is not None]
    return paths, metas, errs


def embed_batch(client, paths: list[Path], task_type: str = "RETRIEVAL_DOCUMENT"):
    """One batchEmbedContents call: N images → N embeddings."""
    from google.genai import types

    contents = []
    for p in paths:
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        contents.append(
            types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(mime_type="image/jpeg", data=b64)
                    )
                ]
            )
        )
    return client.models.embed_content(
        model=MODEL,
        contents=contents,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=DIM,
        ),
    )


def run_batch_embed(
    paths: list[Path],
    *,
    batch_size: int,
    parallel: int,
) -> tuple[dict[str, list[float]], dict]:
    from google import genai
    from app.config import get_settings

    key = get_settings().gemini_api_key
    if not key:
        raise SystemExit("GEMINI_API_KEY missing in backend/.env")
    client = genai.Client(api_key=key)

    batch_size = max(1, min(batch_size, len(paths)))
    parallel = max(1, parallel)
    batches = [paths[i : i + batch_size] for i in range(0, len(paths), batch_size)]

    def run_one(bi_batch: tuple[int, list[Path]]) -> dict:
        bi, batch = bi_batch
        b0 = time.perf_counter()
        try:
            result = embed_batch(client, batch)
            emb = list(result.embeddings or [])
            vectors = [list(e.values or []) for e in emb]
            dims = [len(v) for v in vectors]
            return {
                "bi": bi,
                "paths": batch,
                "vectors": vectors,
                "got": len(vectors),
                "want": len(batch),
                "dims": dims,
                "wall": time.perf_counter() - b0,
                "err": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "bi": bi,
                "paths": batch,
                "vectors": [],
                "got": 0,
                "want": len(batch),
                "dims": [],
                "wall": time.perf_counter() - b0,
                "err": str(exc)[:400],
            }

    work = list(enumerate(batches, start=1))
    t0 = time.perf_counter()
    if parallel == 1:
        results = [run_one(item) for item in work]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
            results = list(ex.map(run_one, work))
    wall = time.perf_counter() - t0

    by_path: dict[str, list[float]] = {}
    ok = 0
    dims_ok = 0
    errors: list[str] = []
    rate_limits = 0
    batch_walls: list[float] = []

    for r in sorted(results, key=lambda x: x["bi"]):
        batch_walls.append(r["wall"])
        if r["err"]:
            msg = r["err"]
            if any(c in msg for c in ("429", "RESOURCE_EXHAUSTED")):
                rate_limits += 1
            errors.append(f"batch {r['bi']}: {msg}")
            print(f"  batch {r['bi']}/{len(batches)} FAILED: {msg[:200]}")
            continue
        for p, vec in zip(r["paths"], r["vectors"]):
            by_path[str(p)] = vec
            if vec:
                ok += 1
            if len(vec) == DIM:
                dims_ok += 1
        print(
            f"  batch {r['bi']}/{len(batches)}: got={r['got']}/{r['want']} "
            f"wall={r['wall']:.3f}s rps={r['got'] / r['wall']:.1f}"
        )
        if r["got"] != r["want"]:
            errors.append(
                f"batch {r['bi']}: expected {r['want']} embeddings, got {r['got']}"
            )

    rps = ok / wall if wall > 0 else 0.0
    stats = {
        "n_ok": ok,
        "n_dim_3072": dims_ok,
        "n_requested": len(paths),
        "wall_seconds": round(wall, 3),
        "rps": round(rps, 2),
        "batch_size": batch_size,
        "parallel": parallel,
        "num_batches": len(batches),
        "rate_limit_errors": rate_limits,
        "errors": errors,
        "batch_wall_p50": round(float(np.percentile(batch_walls, 50)), 3)
        if batch_walls
        else None,
        "batch_wall_p95": round(float(np.percentile(batch_walls, 95)), 3)
        if batch_walls
        else None,
    }
    return by_path, stats


def compare_with_production(
    paths: list[Path],
    batch_vectors: dict[str, list[float]],
    *,
    compare_n: int,
    cosine_min: float,
    max_abs_tol: float,
) -> dict:
    from app.gemini.video_embeddings import embed_frame_sync

    sample = [p for p in paths if str(p) in batch_vectors][:compare_n]
    comparisons: list[dict] = []
    all_ok = True
    for p in sample:
        t0 = time.perf_counter()
        prod = embed_frame_sync(str(p))
        wall = time.perf_counter() - t0
        batch = batch_vectors[str(p)]
        cos = _cosine(batch, prod)
        mad = _max_abs_diff(batch, prod)
        same_len = len(batch) == len(prod) == DIM
        match = same_len and cos >= cosine_min and mad <= max_abs_tol
        if not match:
            all_ok = False
        comparisons.append(
            {
                "path": p.name,
                "cosine": round(cos, 8),
                "max_abs_diff": mad,
                "prod_wall_s": round(wall, 3),
                "match": match,
                "batch_dim": len(batch),
                "prod_dim": len(prod),
            }
        )
        print(
            f"  compare {p.name}: cosine={cos:.6f} max_abs={mad:.3e} "
            f"match={match} prod_wall={wall:.2f}s"
        )

    return {
        "n_compared": len(comparisons),
        "all_match": all_ok and len(comparisons) == compare_n and compare_n > 0,
        "cosine_min": cosine_min,
        "max_abs_tol": max_abs_tol,
        "comparisons": comparisons,
    }


async def async_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.temp_dir) if args.temp_dir else Path(
        tempfile.mkdtemp(prefix="batch_embed_drive_")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    keep_temp = args.keep_temp or bool(args.temp_dir) or bool(args.reuse_dir)

    print(f"temp_dir={out_dir} keep={keep_temp} max_edge={args.max_edge}")
    sys.stdout.flush()

    items: list[dict] = []
    parents: list[str] = []
    metas: list[dict] = []
    dl_errors: list[str] = []
    dl_wall = 0.0

    if args.reuse_dir:
        src = Path(args.reuse_dir)
        if not src.is_dir():
            raise SystemExit(f"--reuse-dir not a directory: {src}")
        src_paths = sorted(src.glob("*.jpg"))[: args.n]
        if not src_paths:
            raise SystemExit(f"No *.jpg in {src}")
        print(f"\n=== Reuse local JPEGs from {src} (n={len(src_paths)}) ===")
        # Optionally re-encode with max_edge into out_dir for throughput.
        paths = []
        for i, p in enumerate(src_paths):
            raw = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
            if raw is None:
                dl_errors.append(f"reuse decode fail: {p.name}")
                continue
            jpeg = _bgr_to_jpeg_bytes(raw, max_edge=args.max_edge)
            dest = out_dir / p.name
            if dest.resolve() != p.resolve():
                dest.write_bytes(jpeg)
            else:
                # Same dir: overwrite with possibly downscaled bytes.
                dest.write_bytes(jpeg)
            paths.append(dest)
            metas.append(
                {
                    "id": p.stem,
                    "name": p.name,
                    "path": str(p),
                    "mime_type": "image/jpeg",
                    "raw_bytes": p.stat().st_size,
                    "jpeg_bytes": len(jpeg),
                    "download_wall_s": 0.0,
                    "local_path": str(dest),
                }
            )
            if (i + 1) % 20 == 0 or i + 1 == len(src_paths):
                print(f"  reuse [{i+1}/{len(src_paths)}] jpeg={len(jpeg):,}")
        print(f"reuse_ok={len(paths)}/{len(src_paths)}")
    else:
        print(
            f"selecting n={args.n} path_ilike={args.path_ilike!r} "
            f"max_download_bytes={args.max_download_bytes:,}"
        )
        sys.stdout.flush()

        items = await select_diverse_images(
            args.n,
            max_bytes=args.max_download_bytes,
            path_ilike=args.path_ilike,
        )
        parents = sorted(
            {
                (it.get("path") or "/").rsplit("/", 1)[0]
                for it in items
            }
        )
        print(f"selected={len(items)} unique_parent_folders={len(parents)}")
        for p in parents[:15]:
            print(f"  parent: {p}")
        if len(parents) > 15:
            print(f"  ... +{len(parents) - 15} more")
        sys.stdout.flush()

        print("\n=== Download + JPEG convert (production decode path) ===")
        t_dl0 = time.perf_counter()
        paths, metas, dl_errors = await download_and_convert(
            items,
            out_dir,
            download_parallel=args.download_parallel,
            max_edge=args.max_edge,
        )
        dl_wall = time.perf_counter() - t_dl0
        print(
            f"download_ok={len(paths)}/{len(items)} wall={dl_wall:.1f}s "
            f"fail={len(dl_errors)}"
        )
        sys.stdout.flush()

    if len(paths) < max(1, args.min_ok):
        print(
            f"FAIL: only {len(paths)} images ready (min_ok={args.min_ok})",
            file=sys.stderr,
        )
        summary = {
            "success": False,
            "phase": "download",
            "download_ok": len(paths),
            "download_requested": len(items) or args.n,
            "download_errors": dl_errors,
        }
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return 1

    print(
        f"\n=== Batch embed (batch_size={args.batch_size} parallel={args.parallel}) ==="
    )
    print(f"model={MODEL} dim={DIM} api=embed_content → batchEmbedContents")
    sys.stdout.flush()
    batch_vectors, embed_stats = run_batch_embed(
        paths, batch_size=args.batch_size, parallel=args.parallel
    )

    print(f"\n=== Compare vs embed_frame_sync (n={args.compare}) ===")
    compare = compare_with_production(
        paths,
        batch_vectors,
        compare_n=args.compare,
        cosine_min=args.cosine_min,
        max_abs_tol=args.max_abs_tol,
    )

    rps = embed_stats["rps"]
    est_6k_s = 6000 / rps if rps > 0 else None
    est_8k_s = 8000 / rps if rps > 0 else None

    embed_pass = (
        embed_stats["n_ok"] == len(paths)
        and embed_stats["n_dim_3072"] == len(paths)
        and embed_stats["rate_limit_errors"] == 0
        and not embed_stats["errors"]
    )
    rps_ok = rps >= args.min_rps
    compare_pass = compare["all_match"]
    # Reliability pass: downloads enough + all embeds OK + vectors match prod + no 429s.
    # RPS is reported separately (full-res photos are payload-bound; use --max-edge).
    overall = (
        embed_pass
        and compare_pass
        and len(paths) >= args.min_ok
        and (rps_ok or not args.require_rps)
    )
    if overall and not rps_ok:
        overall_note = "pass_rps_below_target"
    elif overall and dl_errors:
        overall_note = "pass_with_download_skips"
    elif overall:
        overall_note = "pass"
    else:
        overall_note = "fail"

    summary = {
        "success": overall,
        "verdict": overall_note.upper(),
        "model": MODEL,
        "api_shape": "embed_content → batchEmbedContents (Content-per-image)",
        "download": {
            "requested": len(items) or len(paths),
            "ok": len(paths),
            "failed": len(dl_errors),
            "wall_seconds": round(dl_wall, 3),
            "unique_parent_folders": len(parents),
            "errors": dl_errors[:20],
            "sample": [
                {
                    "name": m["name"],
                    "path": m["path"],
                    "mime_type": m["mime_type"],
                    "jpeg_bytes": m["jpeg_bytes"],
                }
                for m in metas[:5]
            ],
        },
        "embed": embed_stats,
        "compare": compare,
        "estimate_at_measured_rps": {
            "rps": rps,
            "images_6000_seconds": round(est_6k_s, 1) if est_6k_s else None,
            "images_6000_minutes": round(est_6k_s / 60, 1) if est_6k_s else None,
            "images_8000_seconds": round(est_8k_s, 1) if est_8k_s else None,
            "images_8000_minutes": round(est_8k_s / 60, 1) if est_8k_s else None,
        },
        "temp_dir": str(out_dir),
        "jpeg_quality": JPEG_QUALITY,
        "max_edge": args.max_edge,
        "rps_ok": rps_ok,
        "thresholds": {
            "min_rps": args.min_rps,
            "require_rps": args.require_rps,
            "min_ok_downloads": args.min_ok,
            "cosine_min": args.cosine_min,
            "max_abs_tol": args.max_abs_tol,
        },
    }

    print()
    print(json.dumps(summary, indent=2, default=str))
    try:
        args.out.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    except OSError as exc:
        print(f"(could not write {args.out}: {exc})", file=sys.stderr)

    if not keep_temp:
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"cleaned temp_dir={out_dir}")

    print()
    print(f"VERDICT: {summary['verdict']}")
    print(
        f"download_ok={len(paths)}/{len(items) or len(paths)} embed_ok={embed_stats['n_ok']}/{len(paths)} "
        f"rps={rps:.1f} 429s={embed_stats['rate_limit_errors']} "
        f"compare={compare['all_match']} "
        f"est_6k={summary['estimate_at_measured_rps']['images_6000_minutes']}min "
        f"est_8k={summary['estimate_at_measured_rps']['images_8000_minutes']}min"
    )
    return 0 if overall else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100, help="Images to pull from Drive")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Images per batchEmbedContents call (~50 RPS: 5 × parallel 10)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=10,
        help="Concurrent batchEmbedContents calls",
    )
    parser.add_argument(
        "--download-parallel",
        type=int,
        default=6,
        help="Concurrent Drive downloads",
    )
    parser.add_argument(
        "--compare",
        type=int,
        default=8,
        help="Sample size for batch vs embed_frame_sync compare",
    )
    parser.add_argument(
        "--path-ilike",
        default="%",
        help="Optional SQL ILIKE on drive_files.path (default: %% = whole connected tree)",
    )
    parser.add_argument(
        "--max-download-bytes",
        type=int,
        default=25_000_000,
        help="Skip Drive files larger than this (raw size)",
    )
    parser.add_argument(
        "--min-ok",
        type=int,
        default=80,
        help="Minimum successful downloads required to continue",
    )
    parser.add_argument(
        "--min-rps",
        type=float,
        default=20.0,
        help="Target measured RPS (informational unless --require-rps)",
    )
    parser.add_argument(
        "--require-rps",
        action="store_true",
        help="Fail the run if measured RPS is below --min-rps",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=1280,
        help="Downscale longest edge before JPEG encode (0=full res; default 1280 for ~50 RPS payloads)",
    )
    parser.add_argument(
        "--reuse-dir",
        type=str,
        default="",
        help="Skip Drive download; embed existing *.jpg in this directory",
    )
    parser.add_argument(
        "--cosine-min",
        type=float,
        default=0.999,
        help="Min cosine similarity batch vs embed_frame_sync",
    )
    parser.add_argument(
        "--max-abs-tol",
        type=float,
        default=1e-3,
        help="Max abs component diff for vector match",
    )
    parser.add_argument(
        "--temp-dir",
        type=str,
        default="",
        help="Optional fixed temp dir (implies keep)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep downloaded JPEGs after run",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/batch_embed_drive_probe.json"),
        help="Write summary JSON here (no full vectors)",
    )
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
