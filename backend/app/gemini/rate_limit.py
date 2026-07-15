"""Client-side concurrency limits for Gemini API calls.

Gemini Embedding 2 (global): up to ~40k RPM / 10M TPM on paid tiers.
Gemini Flash (multimodal): much lower RPM — keep a small pool.

These semaphores are process-wide so parallel video workers share one budget.
Tune via env without code changes.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _embed_semaphore() -> threading.Semaphore:
    from app.config import get_settings

    n = max(1, get_settings().gemini_embed_max_concurrent)
    logger.info("Gemini embed concurrency limit: %d", n)
    return threading.Semaphore(n)


@lru_cache(maxsize=1)
def _vlm_semaphore() -> threading.Semaphore:
    from app.config import get_settings

    n = max(1, get_settings().gemini_vlm_max_concurrent)
    logger.info("Gemini VLM concurrency limit: %d", n)
    return threading.Semaphore(n)


@lru_cache(maxsize=1)
def _upload_semaphore() -> threading.Semaphore:
    from app.config import get_settings

    n = max(1, get_settings().gemini_upload_max_concurrent)
    logger.info("Gemini File Search upload concurrency limit: %d", n)
    return threading.Semaphore(n)


@contextmanager
def gemini_embed_slot():
    sem = _embed_semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


@contextmanager
def gemini_vlm_slot():
    sem = _vlm_semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


@contextmanager
def gemini_upload_slot():
    sem = _upload_semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def retry_on_rate_limit(func, *args, max_attempts: int = 8, **kwargs):
    """Run *func* with exponential backoff on 429 / RESOURCE_EXHAUSTED."""
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            msg = str(exc)
            if any(code in msg for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                wait = min(120, 5 * (2 ** attempt))
                logger.warning(
                    "Gemini rate limit (attempt %d/%d) — retry in %ds: %s",
                    attempt + 1,
                    max_attempts,
                    wait,
                    msg[:160],
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Gemini call failed after {max_attempts} retries")
