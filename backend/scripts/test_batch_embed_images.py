#!/usr/bin/env python3
"""Throughput probe for Gemini Embedding 2 batchEmbedContents on local JPEGs.

Selects N thumbnail JPEGs from backend/data/thumbnails (preferred) and embeds
them via google-genai ``models.embed_content``, which maps to the REST
``:batchEmbedContents`` endpoint. Each image is wrapped as its own
``types.Content`` so gemini-embedding-2 returns N vectors (not one aggregate).

Does NOT touch Qdrant, Drive downloads, or the indexer queue. Optional JSON
summary to /tmp only.

Usage (from backend/ with venv):
  .venv/bin/python scripts/test_batch_embed_images.py
  .venv/bin/python scripts/test_batch_embed_images.py --n 50 --batch-size 50
  .venv/bin/python scripts/test_batch_embed_images.py --n 50 --batch-size 5 --parallel 10
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
THUMB_DIR = BACKEND_DIR / "data" / "thumbnails"
MODEL = "gemini-embedding-2"
DIM = 3072


def load_gemini_key() -> str:
    env = BACKEND_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("GEMINI_API_KEY not found in backend/.env or environment")
    return key


def pick_jpegs(n: int, max_bytes: int) -> list[Path]:
    if not THUMB_DIR.is_dir():
        raise SystemExit(f"Missing thumbnail dir: {THUMB_DIR}")
    # Prefer numeric Drive-style thumbs; skip huge body_* crops.
    candidates: list[tuple[int, int, Path]] = []
    for p in THUMB_DIR.iterdir():
        if not p.is_file():
            continue
        name = p.name.lower()
        if not (name.endswith(".jpg") or name.endswith(".jpeg")):
            continue
        if name.startswith("body_"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 2_000 or size > max_bytes:
            continue
        # Prefer pure numeric ids (Drive media thumbs).
        stem = p.stem
        score = 0 if stem.isdigit() else 1
        candidates.append((score, size, p))
    candidates.sort(key=lambda t: (t[0], t[1]))
    paths = [t[2] for t in candidates[:n]]
    if len(paths) < n:
        raise SystemExit(f"Only found {len(paths)} suitable JPEGs (wanted {n})")
    return paths


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50, help="Total images to embed")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Images per batchEmbedContents call (default: one shot of --n)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Concurrent batchEmbedContents calls (ThreadPool). ~50/s: --batch-size 5 --parallel 10",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=150_000,
        help="Skip JPEGs larger than this (keep payload reasonable)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/batch_embed_probe.json"),
        help="Write timing summary JSON here (no vectors)",
    )
    args = parser.parse_args()

    key = load_gemini_key()
    paths = pick_jpegs(args.n, args.max_bytes)
    total_bytes = sum(p.stat().st_size for p in paths)

    from google import genai

    client = genai.Client(api_key=key)

    batch_size = max(1, min(args.batch_size, args.n))
    parallel = max(1, args.parallel)
    batches = [paths[i : i + batch_size] for i in range(0, len(paths), batch_size)]

    print(f"model={MODEL} dim={DIM}")
    print(
        f"images={len(paths)} bytes={total_bytes:,} batches={len(batches)} "
        f"batch_size={batch_size} parallel={parallel}"
    )
    print(f"api_shape=client.models.embed_content → REST :batchEmbedContents")
    print(f"content_wrap=types.Content per image (N vectors, not aggregate)")
    sys.stdout.flush()

    def run_one(bi_batch: tuple[int, list[Path]]) -> dict:
        bi, batch = bi_batch
        b0 = time.perf_counter()
        try:
            result = embed_batch(client, batch)
            emb = list(result.embeddings or [])
            dims = [len(list(e.values or [])) for e in emb]
            elapsed_b = time.perf_counter() - b0
            return {
                "bi": bi,
                "got": len(emb),
                "want": len(batch),
                "dims": dims,
                "wall": elapsed_b,
                "err": None,
            }
        except Exception as exc:
            return {
                "bi": bi,
                "got": 0,
                "want": len(batch),
                "dims": [],
                "wall": time.perf_counter() - b0,
                "err": str(exc)[:300],
            }

    ok = 0
    dims_ok = 0
    errors: list[str] = []
    rate_limits = 0
    t0 = time.perf_counter()
    work = list(enumerate(batches, start=1))
    if parallel == 1:
        results = [run_one(item) for item in work]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
            results = list(ex.map(run_one, work))
    wall = time.perf_counter() - t0

    for r in sorted(results, key=lambda x: x["bi"]):
        if r["err"]:
            msg = r["err"]
            if any(c in msg for c in ("429", "RESOURCE_EXHAUSTED")):
                rate_limits += 1
            errors.append(f"batch {r['bi']}: {msg}")
            print(f"  batch {r['bi']}/{len(batches)} FAILED: {msg[:200]}")
            continue
        for d in r["dims"]:
            if d > 0:
                ok += 1
            if d == DIM:
                dims_ok += 1
        print(
            f"  batch {r['bi']}/{len(batches)}: got={r['got']} "
            f"wall={r['wall']:.3f}s rps={r['got'] / r['wall']:.1f}"
        )
        if r["got"] != r["want"]:
            errors.append(
                f"batch {r['bi']}: expected {r['want']} embeddings, got {r['got']}"
            )
        sys.stdout.flush()

    rps = ok / wall if wall > 0 else 0.0
    success = ok == args.n and dims_ok == args.n and not errors

    summary = {
        "success": success,
        "model": MODEL,
        "api_shape": "embed_content → batchEmbedContents (Content-per-image)",
        "n_requested": args.n,
        "n_ok": ok,
        "n_dim_3072": dims_ok,
        "wall_seconds": round(wall, 3),
        "rps": round(rps, 2),
        "batch_size": batch_size,
        "parallel": parallel,
        "num_batches": len(batches),
        "rate_limit_errors": rate_limits,
        "total_jpeg_bytes": total_bytes,
        "sample_paths": [str(p.name) for p in paths[:5]],
        "errors": errors,
    }
    print()
    print(json.dumps(summary, indent=2))
    try:
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    except OSError as exc:
        print(f"(could not write {args.out}: {exc})", file=sys.stderr)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
