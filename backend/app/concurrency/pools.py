"""Process-wide thread pools sized to available CPU cores."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

logger = logging.getLogger(__name__)


def effective_cpu_workers(requested: int) -> int:
    cores = os.cpu_count() or 4
    if requested <= 0:
        return max(2, cores)
    return max(1, min(requested, cores * 2))


@lru_cache(maxsize=1)
def cpu_thread_pool() -> ThreadPoolExecutor:
    from app.config import get_settings

    workers = effective_cpu_workers(get_settings().cpu_thread_pool_size)
    logger.info("CPU thread pool: %d workers (%d cores detected)", workers, os.cpu_count() or 0)
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dfi-cpu")


@lru_cache(maxsize=1)
def gemini_embed_thread_pool() -> ThreadPoolExecutor:
    """I/O-bound pool for batchEmbedContents — must NOT share the tiny CPU pool.

    On 1-vCPU Railway hosts the CPU pool is ~2 threads; routing Gemini through it
    caps embed RPS at ~5/s even when IMAGE_EMBED_BACKFILL_PARALLEL=20.
    """
    from app.config import get_settings

    n = max(8, get_settings().gemini_embed_max_concurrent)
    logger.info("Gemini embed I/O thread pool: %d workers", n)
    return ThreadPoolExecutor(max_workers=n, thread_name_prefix="dfi-embed-io")
