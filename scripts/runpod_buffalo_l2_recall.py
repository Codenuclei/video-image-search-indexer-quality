#!/usr/bin/env python3
"""Compare RunPod buffalo_l embeddings to Postgres faces with closed-set L2 recall.

No Postgres writes. Samples images that already have a named person_id.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))
os.chdir(BACKEND)

from app.config import get_settings  # noqa: E402
from app.db.models import DriveFile, Face, Media, MediaType  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.faces.l2_recall import (  # noqa: E402
    gallery_matrix,
    hit_at_k,
    l2_normalize,
    person_proto,
    rank_person_ids,
)
from app.faces.runpod_gpu import runpod_face_configured, set_face_workers_max  # noqa: E402
from runpod_buffalo_embed_smoke import (  # noqa: E402
    BATCH_SIZE,
    FETCH_CONCURRENCY,
    MAX_BATCH_BYTES,
    RESULTS_DIR,
    _endpoint_id,
    _fetch_jpeg,
    _load_repo_env,
    _match_faces,
    _run_job,
)


async def _wait_face_ready(http: httpx.AsyncClient, endpoint_id: str, timeout_s: float = 1200.0) -> dict:
    key = os.environ["RUNPOD_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        resp = await http.get(f"https://api.runpod.ai/v2/{endpoint_id}/health", headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"health HTTP {resp.status_code}: {resp.text[:400]}")
        last = resp.json() or {}
        workers = last.get("workers") or {}
        ready = int(workers.get("ready") or 0) + int(workers.get("idle") or 0)
        running = int(workers.get("running") or 0)
        initializing = int(workers.get("initializing") or 0)
        queued = int((last.get("jobs") or {}).get("inQueue") or 0)
        print(
            f"workers ready={ready} running={running} initializing={initializing} queue={queued}",
            flush=True,
        )
        if ready >= 1 or running >= 1:
            return last
        await asyncio.sleep(10.0)
    raise RuntimeError(f"face worker not ready after {timeout_s:.0f}s: {last}")

LIMIT = 40


async def _ids_from_named_people(limit: int) -> list[tuple[str, str, str]]:
    factory = get_session_factory()
    per_person = max(2, limit // 10)
    picked: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    async with factory() as session:
        person_ids = (
            await session.execute(
                select(Face.person_id)
                .where(Face.person_id.is_not(None))
                .group_by(Face.person_id)
                .order_by(func.count(Face.id).desc())
            )
        ).scalars().all()
        for pid in person_ids:
            rows = (
                await session.execute(
                    select(DriveFile.id, DriveFile.name, DriveFile.mime_type)
                    .join(Media, Media.drive_file_id == DriveFile.id)
                    .join(Face, Face.media_id == Media.id)
                    .where(
                        Media.type == MediaType.IMAGE,
                        Face.person_id == pid,
                        DriveFile.mime_type.startswith("image/"),
                    )
                    .distinct()
                    .limit(per_person)
                )
            ).all()
            for drive_id, name, mime in rows:
                if drive_id in seen:
                    continue
                seen.add(drive_id)
                picked.append((drive_id, name, mime or "image/jpeg"))
                if len(picked) >= limit:
                    return picked
    return picked


async def _load_named_faces(drive_ids: list[str]) -> dict[str, list[dict]]:
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
                if emb is None or face.person_id is None:
                    continue
                faces.append(
                    {
                        "id": face.id,
                        "person_id": int(face.person_id),
                        "bbox_x": face.bbox_x,
                        "bbox_y": face.bbox_y,
                        "bbox_width": face.bbox_width,
                        "bbox_height": face.bbox_height,
                        "detection_confidence": face.detection_confidence,
                        "embedding": list(emb),
                    }
                )
            out[media.drive_file_id] = faces
    return out


def _all_named(db_faces: dict[str, list[dict]]) -> list[dict]:
    rows = []
    for did, faces in db_faces.items():
        for face in faces:
            item = dict(face)
            item["drive_file_id"] = did
            rows.append(item)
    return rows


def _protos(named: list[dict], *, exclude_face_id: int | None = None) -> dict[int, np.ndarray]:
    by_person: dict[int, list[np.ndarray]] = defaultdict(list)
    for face in named:
        if exclude_face_id is not None and int(face["id"]) == exclude_face_id:
            continue
        by_person[int(face["person_id"])].append(np.asarray(face["embedding"], dtype=np.float64))
    out: dict[int, np.ndarray] = {}
    for pid, embs in by_person.items():
        proto = person_proto(embs)
        if proto is not None:
            out[pid] = proto
    return out


def _tally(hits1: list[bool], hits5: list[bool]) -> dict[str, float | int | None]:
    n = len(hits1)
    return {
        "n": n,
        "recall_at_1": round(sum(hits1) / n, 4) if n else None,
        "recall_at_5": round(sum(hits5) / n, 4) if n else None,
    }


async def main() -> None:
    _load_repo_env()
    settings = get_settings()
    if not os.environ.get("RUNPOD_API_KEY", "").strip():
        raise SystemExit("RUNPOD_API_KEY missing")
    if not runpod_face_configured(settings):
        raise SystemExit("RunPod face endpoint is not configured (or matches Qwen)")
    endpoint_id = _endpoint_id()
    if endpoint_id == (settings.runpod_qwen_endpoint_id or "").strip():
        raise SystemExit("Refusing to use the Qwen identify endpoint for face recall")

    picked = await _ids_from_named_people(LIMIT)
    if len(picked) < 4:
        raise SystemExit("Not enough named-person images in Postgres")
    drive_ids = [row[0] for row in picked]
    db_faces = await _load_named_faces(drive_ids)
    named = _all_named(db_faces)
    people = {int(face["person_id"]) for face in named}
    print(
        f"L2 recall vs Postgres. images={len(picked)} named_faces={len(named)} "
        f"people={len(people)} endpoint={endpoint_id}",
        flush=True,
    )
    if len(people) < 2:
        raise SystemExit("Need at least two named people in the sample")

    timeout = httpx.Timeout(600.0, connect=30.0)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    gpu_by_id: dict[str, dict] = {}

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
                row["error"] = str(exc)[:240]
        return row

    await set_face_workers_max(settings, 1)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            fetch_task = asyncio.gather(
                *[_prefetch(i, did, name, mime) for i, (did, name, mime) in enumerate(picked, start=1)]
            )
            await _wait_face_ready(http, endpoint_id)
            health = None
            last_err: Exception | None = None
            for _attempt in range(8):
                try:
                    health = await _run_job(
                        http, endpoint_id, {"healthcheck": True}, timeout_s=1800.0
                    )
                    break
                except RuntimeError as exc:
                    last_err = exc
                    if "409" not in str(exc) and "paused" not in str(exc).lower():
                        raise
                    await asyncio.sleep(2.0)
            if health is None:
                raise last_err or RuntimeError("face healthcheck failed")
            print(
                f"health model={health.get('model')} providers={health.get('providers')}",
                flush=True,
            )
            fetched = await fetch_task
            ready = [row for row in fetched if "jpeg" in row]
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
            print(f"GPU batches={len(batches)} images={len(ready)}", flush=True)
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
                output = await _run_job(http, endpoint_id, payload, timeout_s=1800.0)
                elapsed = time.perf_counter() - t0
                by_id = {item.get("drive_file_id"): item for item in (output.get("images") or [])}
                print(f"batch {b_i}/{len(batches)} {elapsed:.1f}s", flush=True)
                for row in batch:
                    item = by_id.get(row["drive_file_id"]) or {}
                    row.pop("jpeg", None)
                    row.update(item)
                    gpu_by_id[row["drive_file_id"]] = row
    finally:
        await set_face_workers_max(settings, 0)

    gpu_hits1: list[bool] = []
    gpu_hits5: list[bool] = []
    db_hits1: list[bool] = []
    db_hits5: list[bool] = []
    pairwise: list[float] = []
    queries = 0
    skipped = 0

    for did, db_list in db_faces.items():
        gpu_row = gpu_by_id.get(did) or {}
        gpu_faces = [face for face in (gpu_row.get("faces") or []) if face.get("embedding")]
        orig_wh = tuple(gpu_row.get("orig_wh") or gpu_row.get("sent_wh") or (0, 0))
        sent_wh = tuple(gpu_row.get("sent_wh") or orig_wh)
        if not orig_wh[0] or not sent_wh[0] or not gpu_faces:
            skipped += len(db_list)
            continue
        matches = _match_faces(gpu_faces, db_list, sent_wh, orig_wh)
        db_by_id = {int(face["id"]): face for face in db_list}
        for match in matches:
            db_face = db_by_id.get(int(match["db_face_id"]))
            gi = match.get("gpu_index")
            if db_face is None or gi is None or int(gi) >= len(gpu_faces):
                continue
            true_id = int(db_face["person_id"])
            proto_map = _protos(named, exclude_face_id=int(db_face["id"]))
            if true_id not in proto_map or len(proto_map) < 2:
                skipped += 1
                continue
            ids, mat = gallery_matrix(sorted(proto_map), proto_map)
            db_vec = l2_normalize(np.asarray(db_face["embedding"], dtype=np.float64))
            gpu_emb = l2_normalize(np.asarray(gpu_faces[int(gi)]["embedding"], dtype=np.float64))
            pairwise.append(float(np.dot(gpu_emb, db_vec)))
            gpu_rank = rank_person_ids(gpu_emb, ids, mat)
            db_rank = rank_person_ids(db_vec, ids, mat)
            gpu_hits1.append(hit_at_k(gpu_rank, true_id, 1))
            gpu_hits5.append(hit_at_k(gpu_rank, true_id, 5))
            db_hits1.append(hit_at_k(db_rank, true_id, 1))
            db_hits5.append(hit_at_k(db_rank, true_id, 5))
            queries += 1

    gpu = _tally(gpu_hits1, gpu_hits5)
    db = _tally(db_hits1, db_hits5)
    same = (
        gpu["recall_at_1"] is not None
        and db["recall_at_1"] is not None
        and abs(float(gpu["recall_at_1"]) - float(db["recall_at_1"])) <= 0.03
        and abs(float(gpu["recall_at_5"]) - float(db["recall_at_5"])) <= 0.03
    )
    summary = {
        "at": datetime.now(tz=timezone.utc).isoformat(),
        "endpoint_id": endpoint_id,
        "postgres_writes": False,
        "metric": "L2 on unit ArcFace (equiv. cosine)",
        "images": len(picked),
        "named_people": len(people),
        "queries": queries,
        "skipped": skipped,
        "pairwise_cosine_mean": round(float(np.mean(pairwise)), 6) if pairwise else None,
        "runpod": gpu,
        "postgres": db,
        "same_quality": same,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = RESULTS_DIR / f"l2_recall_{stamp}.json"
    dest.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Wrote {dest}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
