#!/usr/bin/env python3
"""Caption concurrency probe: N images × semaphore-bound Gemini batches.

Uses Gemini 3.5 Flash-Lite (``gemini-3.5-flash-lite``) with production-style
``describe_images_batch`` multi-image prompts. Prefer reusing cached Drive
JPEGs from a prior ``test_batch_embed_drive_images.py`` run.

Does NOT reindex, wipe Qdrant, upsert captions, or deploy.

Usage (from backend/ with venv + backend/.env):
  .venv/bin/python scripts/test_caption_concurrency.py
  .venv/bin/python scripts/test_caption_concurrency.py \\
      --reuse-dir /tmp/batch_embed_drive_100 --n 50 --batch-size 10 --parallel 5
  .venv/bin/python scripts/test_caption_concurrency.py \\
      --reuse-dir /tmp/batch_embed_drive_100 --n 400 --batch-size 10 --parallel 40 \\
      --allow-repeat --skip-token-est --quality-n 0
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import random
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.chdir(BACKEND_DIR)

# Closest real ID matching "Gemini 3.5 Flash-Lite" (verified via models.list).
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_REUSE = Path("/tmp/batch_embed_drive_100")
TARGET_IMAGES_PER_SEC = 50.0


def _downscale_jpeg(jpeg_bytes: bytes, max_dim: int) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg_bytes))
    img = img.convert("RGB")
    w, h = img.size
    if max_dim > 0 and max(w, h) > max_dim:
        scale = max(w, h) / float(max_dim)
        img = img.resize((max(1, int(w / scale)), max(1, int(h / scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()


def _parse_string_array(text: str, expected: int) -> list[str] | None:
    import re

    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != expected:
        return None
    return [str(v).strip() for v in arr]


_DESCRIBE_INSTRUCTION = (
    "You are an image cataloguer. For EACH image below, write ONE concise, factual "
    "description (1-2 sentences) capturing: the main subjects, what they are doing, "
    "the setting/scene type, notable objects, and any clearly legible text/signage. "
    "Be literal and specific; do not speculate or add commentary. "
    "Reply with ONLY a JSON array of strings, one description per image, in order. "
    "No markdown, no extra keys."
)


def caption_batch_sync(
    images: list[bytes],
    *,
    model: str,
    max_dim: int,
    api_key: str,
) -> dict:
    """One multi-image generateContent call. Returns captions + usage."""
    from google import genai
    from google.genai import types

    t0 = time.perf_counter()
    client = genai.Client(api_key=api_key)
    small = [_downscale_jpeg(b, max_dim) for b in images] if max_dim > 0 else list(images)

    parts: list = [types.Part(text=_DESCRIBE_INSTRUCTION)]
    for i, b in enumerate(small, start=1):
        parts.append(types.Part(text=f"Image {i}:"))
        parts.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))

    err: str | None = None
    captions = [""] * len(images)
    usage: dict = {}
    retries_429 = 0
    retries_other = 0
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            um = getattr(resp, "usage_metadata", None)
            if um is not None:
                usage = {
                    "prompt_token_count": getattr(um, "prompt_token_count", None),
                    "candidates_token_count": getattr(um, "candidates_token_count", None),
                    "total_token_count": getattr(um, "total_token_count", None),
                }
            parsed = _parse_string_array(resp.text or "", len(images))
            if parsed is not None:
                captions = parsed
                err = None
                break
            err = "unparseable/mismatched response"
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            err = msg[:400]
            is_429 = any(c in msg for c in ("429", "RESOURCE_EXHAUSTED"))
            is_retryable = is_429 or any(
                c in msg for c in ("503", "UNAVAILABLE", "500")
            )
            if is_429:
                retries_429 += 1
            elif is_retryable:
                retries_other += 1
            if is_retryable:
                time.sleep(2 * (attempt + 1))
                continue
            break

    return {
        "captions": captions,
        "wall_s": time.perf_counter() - t0,
        "err": err,
        "usage": usage,
        "input_bytes": sum(len(b) for b in small),
        "n": len(images),
        "retries_429": retries_429,
        "retries_other": retries_other,
    }


async def run_concurrent_captions(
    paths: list[Path],
    *,
    batch_size: int,
    parallel: int,
    model: str,
    max_dim: int,
    api_key: str,
) -> dict:
    import concurrent.futures

    batches = [paths[i : i + batch_size] for i in range(0, len(paths), batch_size)]
    sem = asyncio.Semaphore(max(1, parallel))
    results: list[dict | None] = [None] * len(batches)
    t0 = time.perf_counter()
    # Default to_thread pool is ~min(32, cpus+4); that starves parallel>32 and
    # queues work so measured concurrency never reaches the semaphore.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(32, parallel + 8))
    loop = asyncio.get_running_loop()

    async def one(bi: int, batch_paths: list[Path]) -> None:
        async with sem:
            # Load inside semaphore so we don't hold all images in RAM at once.
            images = [p.read_bytes() for p in batch_paths]
            t_start = time.perf_counter() - t0
            print(
                f"  batch {bi+1}/{len(batches)} start t={t_start:.2f}s n={len(batch_paths)}",
                flush=True,
            )
            r = await loop.run_in_executor(
                executor,
                lambda: caption_batch_sync(
                    images,
                    model=model,
                    max_dim=max_dim,
                    api_key=api_key,
                ),
            )
            t_end = time.perf_counter() - t0
            r["bi"] = bi
            r["t_start_s"] = t_start
            r["t_end_s"] = t_end
            r["paths"] = [str(p) for p in batch_paths]
            r["names"] = [p.name for p in batch_paths]
            ok = sum(1 for c in r["captions"] if c.strip())
            print(
                f"  batch {bi+1}/{len(batches)} done t={t_end:.2f}s wall={r['wall_s']:.2f}s "
                f"ok={ok}/{r['n']} retries_429={r.get('retries_429', 0)} "
                f"tokens={r['usage']} err={r['err']!r}",
                flush=True,
            )
            results[bi] = r

    try:
        await asyncio.gather(*(one(i, b) for i, b in enumerate(batches)))
    finally:
        executor.shutdown(wait=False)
    wall = time.perf_counter() - t0

    flat: list[dict] = []
    prompt_tokens = 0
    candidate_tokens = 0
    errors: list[str] = []
    batch_walls: list[float] = []
    retries_429 = 0
    retries_other = 0
    n_429_batches = 0

    for r in results:
        assert r is not None
        batch_walls.append(r["wall_s"])
        retries_429 += int(r.get("retries_429") or 0)
        retries_other += int(r.get("retries_other") or 0)
        if r["err"] and any(c in (r["err"] or "") for c in ("429", "RESOURCE_EXHAUSTED")):
            n_429_batches += 1
        if r["err"]:
            errors.append(f"batch{r['bi']+1}: {r['err']}")
        pt = r["usage"].get("prompt_token_count") or 0
        ct = r["usage"].get("candidates_token_count") or 0
        prompt_tokens += int(pt)
        candidate_tokens += int(ct)
        for name, path, cap in zip(r["names"], r["paths"], r["captions"]):
            flat.append(
                {
                    "name": name,
                    "path": path,
                    "caption": cap,
                    "batch": r["bi"] + 1,
                }
            )

    n_ok = sum(1 for x in flat if (x["caption"] or "").strip())
    n_img = len(flat)
    n_req = len(batches)

    # Steady-state: exclude first wave (first `parallel` completions by end time).
    ordered = sorted(
        [r for r in results if r is not None],
        key=lambda r: float(r["t_end_s"]),
    )
    wave_n = min(max(1, parallel), len(ordered))
    first_wave = ordered[:wave_n]
    rest = ordered[wave_n:]
    first_wave_end = max(float(r["t_end_s"]) for r in first_wave) if first_wave else 0.0
    first_wave_ok = sum(
        sum(1 for c in r["captions"] if (c or "").strip()) for r in first_wave
    )
    rest_ok = sum(sum(1 for c in r["captions"] if (c or "").strip()) for r in rest)
    rest_wall = max(0.0, wall - first_wave_end)
    # Also: throughput after pipe is full — from first completion to last,
    # counting only completions after first_wave_end (same as rest_ok / rest_wall).
    steady_ips = (rest_ok / rest_wall) if rest_wall > 0 and rest else None
    latencies = [float(r["wall_s"]) for r in ordered]
    mean_lat = sum(latencies) / len(latencies) if latencies else 0.0
    theoretical_ips = (parallel * batch_size / mean_lat) if mean_lat > 0 else 0.0

    return {
        "wall_s": wall,
        "n_images": n_img,
        "n_ok": n_ok,
        "n_requests": n_req,
        "images_per_sec": (n_ok / wall) if wall > 0 else 0.0,
        "images_per_sec_including_warmup": (n_ok / wall) if wall > 0 else 0.0,
        "images_per_sec_excluding_first_wave": steady_ips,
        "first_wave_batches": wave_n,
        "first_wave_end_s": round(first_wave_end, 3),
        "first_wave_ok_images": first_wave_ok,
        "steady_ok_images": rest_ok,
        "steady_wall_s": round(rest_wall, 3),
        "mean_batch_latency_s": round(mean_lat, 3),
        "theoretical_ips_at_parallel": round(theoretical_ips, 2),
        "requests_per_sec": (n_req / wall) if wall > 0 else 0.0,
        "batch_walls_s": batch_walls,
        "prompt_tokens_total": prompt_tokens,
        "candidates_tokens_total": candidate_tokens,
        "total_tokens": prompt_tokens + candidate_tokens,
        "retries_429": retries_429,
        "retries_other": retries_other,
        "n_429_error_batches": n_429_batches,
        "errors": errors,
        "results": flat,
        "batch_results": [
            {
                "bi": r["bi"] + 1,
                "n": r["n"],
                "wall_s": round(r["wall_s"], 3),
                "t_start_s": round(float(r["t_start_s"]), 3),
                "t_end_s": round(float(r["t_end_s"]), 3),
                "usage": r["usage"],
                "err": r["err"],
                "retries_429": r.get("retries_429", 0),
                "retries_other": r.get("retries_other", 0),
                "input_bytes": r["input_bytes"],
            }
            for r in results
            if r is not None
        ],
    }


def estimate_tokens_14(
    sample_paths: list[Path],
    *,
    model: str,
    api_key: str,
    max_dim_down: int,
) -> dict:
    """Measure prompt tokens for a 14-image batch: full-ish vs downscaled."""
    from google import genai
    from google.genai import types

    # Use up to 14 distinct images; repeat if fewer available.
    if not sample_paths:
        return {"error": "no sample paths"}
    paths = list(sample_paths[:14])
    while len(paths) < 14:
        paths.append(sample_paths[len(paths) % len(sample_paths)])

    client = genai.Client(api_key=api_key)

    def _one(label: str, max_dim: int) -> dict:
        raws = [p.read_bytes() for p in paths]
        imgs = [_downscale_jpeg(b, max_dim) for b in raws] if max_dim > 0 else raws
        parts: list = [types.Part(text=_DESCRIBE_INSTRUCTION)]
        for i, b in enumerate(imgs, start=1):
            parts.append(types.Part(text=f"Image {i}:"))
            parts.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))
        # count_tokens if available; else generate with max tiny output
        try:
            ct = client.models.count_tokens(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
            )
            total = getattr(ct, "total_tokens", None) or getattr(ct, "total_token_count", None)
            return {
                "label": label,
                "max_dim": max_dim,
                "n_images": 14,
                "jpeg_bytes_total": sum(len(b) for b in imgs),
                "jpeg_bytes_avg": int(sum(len(b) for b in imgs) / 14),
                "prompt_tokens": int(total) if total is not None else None,
                "method": "count_tokens",
            }
        except Exception as exc:  # noqa: BLE001
            # Fallback: one generate_content and read usage_metadata
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        max_output_tokens=64,
                    ),
                )
                um = getattr(resp, "usage_metadata", None)
                pt = getattr(um, "prompt_token_count", None) if um else None
                return {
                    "label": label,
                    "max_dim": max_dim,
                    "n_images": 14,
                    "jpeg_bytes_total": sum(len(b) for b in imgs),
                    "jpeg_bytes_avg": int(sum(len(b) for b in imgs) / 14),
                    "prompt_tokens": int(pt) if pt is not None else None,
                    "method": "generate_usage",
                    "note": f"count_tokens failed: {str(exc)[:120]}",
                }
            except Exception as exc2:  # noqa: BLE001
                return {
                    "label": label,
                    "max_dim": max_dim,
                    "n_images": 14,
                    "jpeg_bytes_total": sum(len(b) for b in imgs),
                    "error": str(exc2)[:300],
                }

    # "full-ish": no extra caption downscale (images may already be ~1024–full from cache)
    full = _one("full_ish_no_caption_downscale", 0)
    down_512 = _one("downscale_max_edge_512", max_dim_down)
    down_768 = _one("downscale_max_edge_768", 768)

    # Extrapolate 50-image run at batch=10, parallel=5 (5 calls)
    def _extrap(probe: dict, images_per_call: int = 10, n_images: int = 50) -> dict:
        pt = probe.get("prompt_tokens")
        if not pt:
            return {"error": "no prompt_tokens"}
        per_img = pt / 14.0
        # instruction is shared; rough: scale image portion
        # Better: tokens ≈ instruction + n * (pt - instruction)/14
        # Without instruction split, linear scale by image count is fine upper-bound.
        per_call = per_img * images_per_call
        n_calls = max(1, (n_images + images_per_call - 1) // images_per_call)
        run_prompt = per_call * n_calls
        return {
            "tokens_per_image_approx": round(per_img, 1),
            "tokens_per_10img_call_approx": round(per_call, 1),
            "tokens_for_50img_run_approx": round(run_prompt, 1),
            "well_under_1M": run_prompt < 1_000_000,
            "vs_tier_4M_tpm": round(100.0 * run_prompt / 4_000_000, 3),
        }

    return {
        "probes": [full, down_512, down_768],
        "extrapolate_50img_batch10": {
            "full_ish": _extrap(full),
            "down_512": _extrap(down_512),
            "down_768": _extrap(down_768),
        },
    }


def judge_caption_vs_image(path: Path, caption: str, *, model: str, api_key: str) -> dict:
    """Second-pass judge: does caption match image content?"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    jpeg = _downscale_jpeg(path.read_bytes(), 768)
    prompt = (
        "You are a strict QA judge. Given an IMAGE and a CAPTION, decide if the caption "
        "semantically matches the visible content (main subjects, scene, notable objects). "
        "Reply ONLY with JSON: {\"match\": true|false, \"score\": 0-5, \"reason\": \"...\"}. "
        "score 5 = excellent match, 0 = totally wrong.\n\n"
        f"CAPTION: {caption}"
    )
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=prompt),
                        types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = resp.text or ""
        m = __import__("re").search(r"\{[\s\S]*\}", text)
        data = json.loads(m.group()) if m else {}
        return {
            "name": path.name,
            "caption": caption,
            "match": bool(data.get("match")),
            "score": data.get("score"),
            "reason": data.get("reason"),
            "raw": text[:500],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": path.name,
            "caption": caption,
            "match": None,
            "score": None,
            "reason": f"judge failed: {exc}"[:200],
        }


def load_paths(reuse_dir: Path, n: int, *, allow_repeat: bool = False) -> list[Path]:
    paths = sorted(reuse_dir.glob("*.jpg"))
    if not paths:
        raise SystemExit(f"No *.jpg in {reuse_dir}")
    # Prefer mid-size files (skip tiny junk / huge outliers for stable probe)
    scored = []
    for p in paths:
        sz = p.stat().st_size
        if sz < 5_000:
            continue
        scored.append(p)
    if len(scored) < min(n, len(paths)):
        scored = paths
    # diversify: stride sample
    if len(scored) >= n:
        step = max(1, len(scored) // n)
        picked = [scored[(i * step) % len(scored)] for i in range(n)]
        # de-dupe preserving order
        seen: set[str] = set()
        out: list[Path] = []
        for p in picked:
            if p.name in seen:
                continue
            seen.add(p.name)
            out.append(p)
        for p in scored:
            if len(out) >= n:
                break
            if p.name not in seen:
                seen.add(p.name)
                out.append(p)
        return out[:n]
    if allow_repeat and scored:
        # Cycle unique cache for steady-state throughput (API cost, not uniqueness).
        out = []
        i = 0
        while len(out) < n:
            out.append(scored[i % len(scored)])
            i += 1
        print(f"allow_repeat: cycling {len(scored)} cached jpgs → {n} slots")
        return out
    return scored[:n]


async def async_main(args: argparse.Namespace) -> int:
    from app.config import get_settings

    settings = get_settings()
    api_key = settings.gemini_api_key
    if not api_key:
        raise SystemExit("GEMINI_API_KEY missing in backend/.env")

    model = args.model
    max_dim = args.max_dim
    reuse = Path(args.reuse_dir)
    print(f"model={model}")
    print(f"reuse_dir={reuse} n={args.n} batch_size={args.batch_size} parallel={args.parallel}")
    print(f"caption_max_dim={max_dim} (0=no downscale)")
    print(
        f"tier_note: Tier ~2000 RPM / 4M TPM — parallel={args.parallel} concurrent calls; "
        "RPM ok if <<2000; TPM checked via tokens"
    )
    sys.stdout.flush()

    # Resolve model: try requested, then fallbacks
    from google import genai

    client = genai.Client(api_key=api_key)
    tried = [model]
    working = None
    for mid in [model, "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]:
        if mid in tried and mid != model and working:
            continue
        if mid not in tried:
            tried.append(mid)
        try:
            r = client.models.generate_content(model=mid, contents="Reply with OK only.")
            if (r.text or "").strip():
                working = mid
                print(f"model_ok={mid}")
                break
        except Exception as e:
            print(f"model_fail={mid}: {str(e)[:120]}")
    if not working:
        raise SystemExit(f"No working Flash-Lite model among {tried}")
    model = working

    paths = load_paths(reuse, args.n, allow_repeat=args.allow_repeat)
    print(f"loaded={len(paths)} images from {reuse}")
    for i, p in enumerate(paths[:5]):
        print(f"  sample[{i}] {p.name} bytes={p.stat().st_size:,}")
    sys.stdout.flush()

    token_est: dict = {}
    if not args.skip_token_est:
        print("\n=== Token estimate: 14-image prompt (full-ish vs downscale) ===")
        token_est = estimate_tokens_14(
            paths,
            model=model,
            api_key=api_key,
            max_dim_down=max_dim if max_dim > 0 else 512,
        )
        print(json.dumps(token_est, indent=2))
        sys.stdout.flush()
    else:
        print("\n=== Token estimate: skipped (--skip-token-est) ===")

    print(
        f"\n=== Caption run: {len(paths)} images, "
        f"batch={args.batch_size}, parallel={args.parallel} ==="
    )
    run = await run_concurrent_captions(
        paths,
        batch_size=args.batch_size,
        parallel=args.parallel,
        model=model,
        max_dim=max_dim,
        api_key=api_key,
    )

    if args.print_captions:
        print("\n=== ALL CAPTIONS ===")
        for i, row in enumerate(run["results"], start=1):
            cap = (row["caption"] or "").replace("\n", " ").strip()
            print(f"{i:02d}. [{row['name']}] {cap or '(EMPTY)'}")

    # Quality: N random non-empty captions (0 = skip)
    quality: list[dict] = []
    q_ok = q_fail = q_unk = 0
    if args.quality_n > 0:
        ok_rows = [r for r in run["results"] if (r["caption"] or "").strip()]
        rng = random.Random(args.seed)
        sample = ok_rows[:] if len(ok_rows) <= args.quality_n else rng.sample(ok_rows, args.quality_n)
        print(f"\n=== Quality checks ({len(sample)} random) ===")
        for row in sample:
            q = await asyncio.to_thread(
                judge_caption_vs_image,
                Path(row["path"]),
                row["caption"],
                model=model,
                api_key=api_key,
            )
            quality.append(q)
            print(
                f"  {q['name']}: match={q.get('match')} score={q.get('score')} "
                f"reason={q.get('reason')!r}"
            )
            print(f"    caption: {row['caption'][:160]}")

        q_ok = sum(1 for q in quality if q.get("match") is True)
        q_fail = sum(1 for q in quality if q.get("match") is False)
        q_unk = len(quality) - q_ok - q_fail
    else:
        print("\n=== Quality checks: skipped (--quality-n 0) ===")

    ips = run["images_per_sec"]
    ips_steady = run.get("images_per_sec_excluding_first_wave")
    rps = run["requests_per_sec"]
    # YES if steady-state (preferred) or overall hits 50
    best_ips = ips_steady if ips_steady is not None else ips
    hit_50 = best_ips >= TARGET_IMAGES_PER_SEC * 0.8  # 80% soft band
    hard_yes = best_ips >= TARGET_IMAGES_PER_SEC

    # Pass/fail criteria
    captions_ok = run["n_ok"] >= int(args.n * 0.9)
    if args.quality_n > 0 and quality:
        quality_ok = q_ok >= max(1, int(len(quality) * 0.7)) and q_fail == 0
    else:
        quality_ok = True
    tokens_ok = True
    for key in ("down_512", "full_ish"):
        ex = token_est.get("extrapolate_50img_batch10", {}).get(key, {})
        if isinstance(ex, dict) and "well_under_1M" in ex:
            tokens_ok = tokens_ok and bool(ex["well_under_1M"])

    # TPM / RPM vs Tier 2
    wall = run["wall_s"]
    tpm_equiv = (run["total_tokens"] / wall * 60.0) if wall > 0 else 0.0
    rpm_equiv = (run["n_requests"] / wall * 60.0) if wall > 0 else 0.0
    tier_tpm = 4_000_000
    tier_rpm = 2_000

    overall = captions_ok and quality_ok and tokens_ok and (run["n_ok"] > 0)

    summary = {
        "success": overall,
        "model_used": model,
        "models_tried": tried,
        "n_images": run["n_images"],
        "n_ok": run["n_ok"],
        "n_requests": run["n_requests"],
        "wall_s": round(run["wall_s"], 3),
        "images_per_sec_including_warmup": round(ips, 2),
        "images_per_sec_excluding_first_wave": (
            round(ips_steady, 2) if ips_steady is not None else None
        ),
        "images_per_sec": round(best_ips, 2),
        "first_wave_batches": run.get("first_wave_batches"),
        "first_wave_end_s": run.get("first_wave_end_s"),
        "first_wave_ok_images": run.get("first_wave_ok_images"),
        "steady_ok_images": run.get("steady_ok_images"),
        "steady_wall_s": run.get("steady_wall_s"),
        "mean_batch_latency_s": run.get("mean_batch_latency_s"),
        "theoretical_ips_at_parallel": run.get("theoretical_ips_at_parallel"),
        "requests_per_sec": round(rps, 3),
        "target_images_per_sec": TARGET_IMAGES_PER_SEC,
        "can_do_approx_50_images_per_sec": "YES" if hard_yes else ("NEAR" if hit_50 else "NO"),
        "batch_size": args.batch_size,
        "parallel": args.parallel,
        "caption_max_dim": max_dim,
        "prompt_tokens_total_run": run["prompt_tokens_total"],
        "candidates_tokens_total_run": run["candidates_tokens_total"],
        "total_tokens_run": run["total_tokens"],
        "retries_429": run.get("retries_429", 0),
        "retries_other": run.get("retries_other", 0),
        "n_429_error_batches": run.get("n_429_error_batches", 0),
        "tpm_equiv": round(tpm_equiv, 1),
        "rpm_equiv": round(rpm_equiv, 2),
        "tier2_tpm_pct": round(100.0 * tpm_equiv / tier_tpm, 3),
        "tier2_rpm_pct": round(100.0 * rpm_equiv / tier_rpm, 3),
        "batch_walls_s": [round(x, 3) for x in run["batch_walls_s"]],
        "batch_results": run["batch_results"],
        "errors": run["errors"],
        "token_estimate_14": token_est,
        "quality_checks": quality,
        "quality_pass": f"{q_ok}/{len(quality)} match (fail={q_fail} unk={q_unk})",
        "captions": run["results"] if args.print_captions else None,
        "pass_fail": {
            "captions_90pct": captions_ok,
            "quality_majority_match": quality_ok,
            "tokens_under_1M": tokens_ok,
            "overall": overall,
        },
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n=== SUMMARY ===")
    print(f"model_used: {model}")
    print(f"wall: {run['wall_s']:.2f}s | requests/sec: {rps:.3f}")
    print(f"imgs/s INCLUDING warmup: {ips:.2f}")
    if ips_steady is not None:
        print(
            f"imgs/s EXCLUDING first wave ({run.get('first_wave_batches')} batches, "
            f"end={run.get('first_wave_end_s')}s): {ips_steady:.2f} "
            f"({run.get('steady_ok_images')} imgs / {run.get('steady_wall_s')}s)"
        )
    print(
        f"mean_latency: {run.get('mean_batch_latency_s')}s | "
        f"theoretical@parallel: {run.get('theoretical_ips_at_parallel')} imgs/s"
    )
    print(f"ok_captions: {run['n_ok']}/{run['n_images']} | tokens_run: {run['total_tokens']}")
    print(
        f"429 retries: {run.get('retries_429', 0)} | "
        f"429 hard-fail batches: {run.get('n_429_error_batches', 0)} | "
        f"other retries: {run.get('retries_other', 0)}"
    )
    print(
        f"TPM equiv: {tpm_equiv:,.0f}/min ({summary['tier2_tpm_pct']}% of 4M) | "
        f"RPM equiv: {rpm_equiv:.1f}/min ({summary['tier2_rpm_pct']}% of 2k)"
    )
    print(f"~50 images/sec?: {summary['can_do_approx_50_images_per_sec']} (best={best_ips:.2f})")
    print(f"quality: {summary['quality_pass']}")
    print(f"PASS/FAIL overall: {'PASS' if overall else 'FAIL'} {summary['pass_fail']}")
    print(f"wrote {out_path}")
    return 0 if overall else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Caption concurrency probe (Flash-Lite)")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--parallel", type=int, default=5)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument(
        "--max-dim",
        type=int,
        default=512,
        help="Caption downscale longest edge (production image_caption_max_dim=512)",
    )
    p.add_argument("--reuse-dir", default=str(DEFAULT_REUSE))
    p.add_argument("--quality-n", type=int, default=7)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="/tmp/caption_concurrency_probe.json")
    p.add_argument(
        "--allow-repeat",
        action="store_true",
        help="Cycle cached JPEGs when --n exceeds unique files (throughput probe)",
    )
    p.add_argument(
        "--skip-token-est",
        action="store_true",
        help="Skip 14-image count_tokens probe (faster for large concurrency runs)",
    )
    p.add_argument(
        "--print-captions",
        action="store_true",
        help="Print every caption (default off for large n)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
