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
