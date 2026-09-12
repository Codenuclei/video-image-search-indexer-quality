"""Filesystem-backed visual preparation jobs for Choose images.

When RunPod cold-start exceeds the interactive select-images budget, persist a
job under the thumbnail tree so ``/test/studio`` can poll preparing → ready.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING: set[str] = set()


def _jobs_dir(thumbnail_dir: str, drive_file_id: str) -> Path:
    return Path(thumbnail_dir) / "video" / drive_file_id / "visual_prep_jobs"


def job_path(thumbnail_dir: str, drive_file_id: str, job_id: str) -> Path:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")[:80]
    return _jobs_dir(thumbnail_dir, drive_file_id) / f"{safe}.json"


def slides_fingerprint(slides: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        parts.append(
            f"{slide.get('timestamp_sec')}:{slide.get('end_timestamp_sec')}:"
            f"{(slide.get('transcript_text') or slide.get('hook_line') or '')[:40]}"
        )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def write_job(
    thumbnail_dir: str,
    drive_file_id: str,
    *,
    job_id: str | None = None,
    status: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    request_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jid = job_id or uuid.uuid4().hex
    path = job_path(thumbnail_dir, drive_file_id, jid)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
    data = {
        **existing,
        "job_id": jid,
        "drive_file_id": drive_file_id,
        "status": status,
        "updated_at": time.time(),
        "error": error,
    }
    if payload is not None:
        data["result"] = payload
    if request_body is not None:
        data["request"] = request_body
    if "created_at" not in data:
        data["created_at"] = time.time()
    partial = path.with_suffix(".partial.json")
    partial.write_text(json.dumps(data), encoding="utf-8")
    partial.replace(path)
    return data


def read_job(thumbnail_dir: str, drive_file_id: str, job_id: str) -> dict[str, Any] | None:
    path = job_path(thumbnail_dir, drive_file_id, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def latest_job_for_fingerprint(
    thumbnail_dir: str,
    drive_file_id: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    root = _jobs_dir(thumbnail_dir, drive_file_id)
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if ".partial." in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        req = data.get("request") or {}
        if str(req.get("slides_fingerprint") or "") == fingerprint:
            return data
    return None


def mark_running(job_id: str) -> bool:
    with _LOCK:
        if job_id in _RUNNING:
            return False
        _RUNNING.add(job_id)
        return True


def clear_running(job_id: str) -> None:
    with _LOCK:
        _RUNNING.discard(job_id)
