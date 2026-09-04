#!/usr/bin/env python3
"""Smoke-test RunPod buffalo_l 512-d embeddings against existing Postgres vectors.

No Postgres writes. Uses the same 100 Drive images as the last dry-run when present.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import selectinload

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.db.models import Face, Media, MediaType  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.dependencies import get_drive_client  # noqa: E402
from app.pipelines.common import decode_image_bgr, download_to_temp_file  # noqa: E402

RESULTS_DIR = REPO / "runpod" / "face-buffalo" / "results"
ENDPOINT_FILE = REPO / "runpod" / "face-buffalo" / ".endpoint_id"
PREVIOUS_DRY_RUN = RESULTS_DIR / "dry_run_20260904T120651Z.json"
MAX_EDGE = 0  # 0 = keep production decode size; JPEG only for transport
MAX_JPEG_BYTES = 1_200_000
BATCH_SIZE = 16
MAX_BATCH_BYTES = 6 * 1024 * 1024
FETCH_CONCURRENCY = 8
IOU_MIN = 0.4


def _load_repo_env() -> None:
    env_path = BACKEND / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing {env_path}")
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _endpoint_id() -> str:
    env_id = os.environ.get("RUNPOD_FACE_ENDPOINT_ID", "").strip()
    if env_id:
        return env_id
    if ENDPOINT_FILE.is_file():
        return ENDPOINT_FILE.read_text().strip()
    raise SystemExit("No RUNPOD_FACE_ENDPOINT_ID or runpod/face-buffalo/.endpoint_id")


def _jpeg_bytes(image_bgr: np.ndarray) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    orig_h, orig_w = image_bgr.shape[:2]
    if MAX_EDGE > 0:
        scale = min(1.0, MAX_EDGE / max(orig_h, orig_w))
        if scale < 1.0:
            image_bgr = cv2.resize(
                image_bgr,
                (int(orig_w * scale), int(orig_h * scale)),
                interpolation=cv2.INTER_AREA,
            )
    quality = 90
    encoded = None
    while quality >= 50:
        ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise ValueError("JPEG encode failed")
        encoded = buf.tobytes()
        if len(encoded) <= MAX_JPEG_BYTES:
            h, w = image_bgr.shape[:2]
            return encoded, (orig_w, orig_h), (w, h)
        quality -= 10
    assert encoded is not None
    h, w = image_bgr.shape[:2]
    return encoded, (orig_w, orig_h), (w, h)


def _ids_from_previous() -> list[tuple[str, str, str]] | None:
    if not PREVIOUS_DRY_RUN.is_file():
        latest = sorted(RESULTS_DIR.glob("dry_run_*.json"))
        path = latest[-1] if latest else None
    else:
        path = PREVIOUS_DRY_RUN
    if path is None:
        return None
    data = json.loads(path.read_text())
    rows = []
    for item in data.get("results") or []:
        did = item.get("drive_file_id")
        if did:
            rows.append((did, item.get("name") or did, item.get("mime_type") or "image/jpeg"))
    print(f"Reusing {len(rows)} ids from {path.name}")
    return rows[:100] if rows else None


async def _fetch_jpeg(drive_file_id: str, name: str) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    settings = get_settings()
    client = get_drive_client()
    suffix = Path(name).suffix or ".bin"
    async with download_to_temp_file(client, drive_file_id, settings, suffix=suffix) as path:
        raw = Path(path).read_bytes()
    image = decode_image_bgr(raw, file_name=name)
    return _jpeg_bytes(image)


async def _run_job(client: httpx.AsyncClient, endpoint_id: str, payload: dict) -> dict:
    key = os.environ["RUNPOD_API_KEY"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    submit = await client.post(
        f"https://api.runpod.ai/v2/{endpoint_id}/run",
        headers=headers,
        json={"input": payload},
    )
    if submit.status_code >= 400:
        raise RuntimeError(f"run HTTP {submit.status_code}: {submit.text[:500]}")
    body = submit.json()
    job_id = body.get("id")
    if not job_id:
        raise RuntimeError(f"run missing id: {body}")
    status_url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    deadline = time.monotonic() + 900.0
    status = ""
    while time.monotonic() < deadline:
        status_resp = await client.get(status_url, headers=headers)
        if status_resp.status_code >= 400:
            raise RuntimeError(f"status HTTP {status_resp.status_code}: {status_resp.text[:500]}")
        data = status_resp.json()
        status = str(data.get("status") or "")
        if status in {"COMPLETED", "completed"}:
            return data.get("output") or {}
        if status in {"FAILED", "failed", "CANCELLED", "cancelled", "TIMED_OUT", "timed_out"}:
            raise RuntimeError(f"job {job_id} {status}: {data.get('error') or data}")
        await asyncio.sleep(0.5)
    raise RuntimeError(f"job {job_id} still {status} after 15m")


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb)) or 1e-8
    return float(np.dot(va, vb) / denom)


def _scale_box(face: dict, sent_wh: tuple[int, int], orig_wh: tuple[int, int]) -> tuple[float, float, float, float]:
    sw, sh = sent_wh
    ow, oh = orig_wh
    sx = ow / sw if sw else 1.0
    sy = oh / sh if sh else 1.0
    return (
        float(face["bbox_x"]) * sx,
        float(face["bbox_y"]) * sy,
        float(face["bbox_width"]) * sx,
        float(face["bbox_height"]) * sy,
    )


def _match_faces(gpu_faces: list[dict], db_faces: list[dict], sent_wh, orig_wh) -> list[dict]:
    pairs: list[tuple[float, int, int]] = []
    gpu_boxes = [_scale_box(f, sent_wh, orig_wh) for f in gpu_faces]
    db_boxes = [(f["bbox_x"], f["bbox_y"], f["bbox_width"], f["bbox_height"]) for f in db_faces]
    for gi, gb in enumerate(gpu_boxes):
        for di, db in enumerate(db_boxes):
            score = _iou(gb, db)
            if score >= IOU_MIN:
                pairs.append((score, gi, di))
    pairs.sort(reverse=True)
    used_g: set[int] = set()
    used_d: set[int] = set()
    matches = []
    for score, gi, di in pairs:
        if gi in used_g or di in used_d:
            continue
        used_g.add(gi)
        used_d.add(di)
        gpu_emb = gpu_faces[gi].get("embedding") or []
        db_emb = db_faces[di].get("embedding") or []
        cos = _cosine(gpu_emb, db_emb) if gpu_emb and db_emb else None
        matches.append(
            {
                "iou": round(score, 4),
                "cosine": None if cos is None else round(cos, 6),
                "gpu_conf": gpu_faces[gi].get("confidence"),
                "db_conf": db_faces[di].get("detection_confidence"),
                "db_face_id": db_faces[di].get("id"),
            }
        )
    return matches


async def _load_db_faces(drive_ids: list[str]) -> dict[str, list[dict]]:
    factory = get_session_factory()
    out: dict[str, list[dict]] = {did: [] for did in drive_ids}
    async with factory() as session:
        rows = (
            await session.execute(
                select(Media)
                .options(selectinload(Media.faces).selectinload(Face.embedding))
                .where(Media.drive_file_id.in_(drive_ids), Media.type == MediaType.IMAGE)
            )
        ).scalars().all()
        for media in rows:
            faces = []
            for face in media.faces:
                emb = face.embedding.embedding if face.embedding is not None else None
                faces.append(
                    {
                        "id": face.id,
                        "bbox_x": face.bbox_x,
                        "bbox_y": face.bbox_y,
                        "bbox_width": face.bbox_width,
                        "bbox_height": face.bbox_height,
                        "detection_confidence": face.detection_confidence,
                        "embedding": list(emb) if emb is not None else [],
                    }
                )
            out[media.drive_file_id] = faces
    return out


async def main() -> None:
    _load_repo_env()
    if not os.environ.get("RUNPOD_API_KEY", "").strip():
        raise SystemExit("RUNPOD_API_KEY missing")
    endpoint_id = _endpoint_id()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    picked = _ids_from_previous()
    if not picked:
        raise SystemExit("No previous dry-run ids to reuse")
    drive_ids = [r[0] for r in picked]
    print(f"Smoke {len(picked)} images. Endpoint {endpoint_id}")
    db_faces = await _load_db_faces(drive_ids)
    db_with_emb = sum(1 for faces in db_faces.values() for f in faces if f.get("embedding"))
    db_total = sum(len(v) for v in db_faces.values())
    print(f"Postgres faces={db_total} with_512d={db_with_emb}")

    timeout = httpx.Timeout(600.0, connect=30.0)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _prefetch(index: int, drive_file_id: str, name: str, mime: str) -> dict:
        row: dict = {"index": index, "drive_file_id": drive_file_id, "name": name, "mime_type": mime}
        async with sem:
            try:
                jpeg, orig_wh, sent_wh = await _fetch_jpeg(drive_file_id, name)
                row["jpeg"] = jpeg
                row["jpeg_bytes"] = len(jpeg)
                row["orig_wh"] = orig_wh
                row["sent_wh"] = sent_wh
            except Exception as exc:  # noqa: BLE001
                row["error"] = str(exc)
        return row

    gpu_rows: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout) as http:
        health = await _run_job(http, endpoint_id, {"healthcheck": True})
        print(
            "Health:",
            json.dumps(
                {
                    "model": health.get("model"),
                    "providers": health.get("providers"),
                    "gpu_workers": health.get("gpu_workers"),
                    "runtime": health.get("runtime"),
                },
                default=str,
            ),
        )
        print(f"Prefetching {len(picked)} images (no 1600 downscale)...")
        fetched = await asyncio.gather(
            *[_prefetch(i, did, name, mime) for i, (did, name, mime) in enumerate(picked, start=1)]
        )
        ready = [r for r in fetched if "jpeg" in r]
        for err in fetched:
            if "error" in err and "jpeg" not in err:
                print(f"[{err['index']}/{len(picked)}] {err['name']}: ERROR {err['error']}")
                gpu_rows.append(err)

        batches: list[list[dict]] = []
        current: list[dict] = []
        current_bytes = 0
        for row in ready:
            jpeg_len = int(row["jpeg_bytes"])
            if current and (len(current) >= BATCH_SIZE or current_bytes + jpeg_len > MAX_BATCH_BYTES):
                batches.append(current)
                current = []
                current_bytes = 0
            current.append(row)
            current_bytes += jpeg_len
        if current:
            batches.append(current)
        print(f"Sending {len(ready)} images in {len(batches)} GPU batches")

        for b_i, batch in enumerate(batches, start=1):
            t0 = time.perf_counter()
            payload = {
                "images": [
                    {
                        "drive_file_id": row["drive_file_id"],
                        "image_b64": base64.b64encode(row["jpeg"]).decode("ascii"),
                    }
                    for row in batch
                ]
            }
            try:
                output = await _run_job(http, endpoint_id, payload)
                roundtrip_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                by_id = {item.get("drive_file_id"): item for item in (output.get("images") or [])}
                print(
                    f"batch {b_i}/{len(batches)} n={output.get('batch_size')} "
                    f"gpu_batch_ms={output.get('batch_ms')} roundtrip_ms={roundtrip_ms} "
                    f"det_workers={output.get('gpu_workers')} vram={output.get('vram')}"
                )
                for row in batch:
                    item = by_id.get(row["drive_file_id"]) or {}
                    row.pop("jpeg", None)
                    row.update(item)
                    row["roundtrip_ms"] = roundtrip_ms
                    row["batch"] = b_i
                    gpu_rows.append(row)
                    print(
                        f"[{row['index']}/{len(picked)}] {row['name']}: "
                        f"gpu_faces={row.get('face_count')} db_faces={len(db_faces.get(row['drive_file_id'], []))}"
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"batch {b_i}/{len(batches)} ERROR {exc}")
                for row in batch:
                    row.pop("jpeg", None)
                    row["error"] = str(exc)
                    gpu_rows.append(row)

    all_cos: list[float] = []
    all_iou: list[float] = []
    matched = 0
    gpu_faces_n = 0
    compared = []
    for row in gpu_rows:
        did = row.get("drive_file_id")
        gpu_faces = [f for f in (row.get("faces") or []) if f.get("embedding")]
        gpu_faces_n += len(gpu_faces)
        orig_wh = tuple(row.get("orig_wh") or row.get("sent_wh") or (row.get("width"), row.get("height")))
        sent_wh = tuple(row.get("sent_wh") or (row.get("width"), row.get("height")))
        if not orig_wh[0] or not sent_wh[0]:
            continue
        matches = _match_faces(gpu_faces, db_faces.get(did, []), sent_wh, orig_wh)
        matched += len(matches)
        for m in matches:
            if m["cosine"] is not None:
                all_cos.append(float(m["cosine"]))
            all_iou.append(float(m["iou"]))
        compared.append(
            {
                "drive_file_id": did,
                "name": row.get("name"),
                "gpu_faces": len(gpu_faces),
                "db_faces": len(db_faces.get(did, [])),
                "matched": len(matches),
                "matches": matches,
            }
        )

    def _pct(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        return round(s[max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))], 6)

    summary = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "endpoint_id": endpoint_id,
        "postgres_writes": False,
        "images": len(picked),
        "db_faces": db_total,
        "db_faces_with_emb": db_with_emb,
        "gpu_faces_with_emb": gpu_faces_n,
        "matched_iou_ge": IOU_MIN,
        "matched": matched,
        "cosine_n": len(all_cos),
        "cosine_mean": round(statistics.mean(all_cos), 6) if all_cos else None,
        "cosine_p50": _pct(all_cos, 0.50),
        "cosine_p05": _pct(all_cos, 0.05),
        "cosine_min": round(min(all_cos), 6) if all_cos else None,
        "cosine_ge_0.99": sum(1 for c in all_cos if c >= 0.99),
        "cosine_ge_0.95": sum(1 for c in all_cos if c >= 0.95),
        "cosine_ge_0.90": sum(1 for c in all_cos if c >= 0.90),
        "cosine_lt_0.60": sum(1 for c in all_cos if c < 0.60),
        "iou_p50": _pct(all_iou, 0.50),
        "same_space": bool(all_cos) and (statistics.median(all_cos) >= 0.95),
    }
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"embed_smoke_{stamp}.json"
    out_path.write_text(json.dumps({"summary": summary, "images": compared}, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
