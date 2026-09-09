"""HMAC-signed HTTPS pull so RunPod downloads the cached video (never Drive URLs)."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote, urlencode

from app.config import Settings

_PULL_PATH = "/internal/face-gpu-video"


def sign_face_video_pull(drive_file_id: str, exp: int, secret: str) -> str:
    msg = f"{drive_file_id}:{int(exp)}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_face_video_pull(drive_file_id: str, exp: int, sig: str, secret: str) -> bool:
    if not secret or not sig:
        return False
    expected = sign_face_video_pull(drive_file_id, exp, secret)
    return hmac.compare_digest(expected, sig)


def signed_face_video_pull_url(drive_file_id: str, settings: Settings) -> str:
    base = (settings.public_base_url or "").strip().rstrip("/")
    secret = (settings.runpod_api_key or "").strip()
    if not base:
        raise ValueError("PUBLIC_BASE_URL is required so RunPod can download the cached video")
    if not secret:
        raise ValueError("RUNPOD_API_KEY is required to sign the video pull URL")
    ttl = max(60, int(settings.runpod_face_video_pull_ttl_seconds))
    exp = int(time.time()) + ttl
    sig = sign_face_video_pull(drive_file_id, exp, secret)
    query = urlencode({"exp": exp, "sig": sig})
    return f"{base}{_PULL_PATH}/{quote(drive_file_id, safe='')}?{query}"
