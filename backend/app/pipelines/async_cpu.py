from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from app.concurrency.pools import cpu_thread_pool

T = TypeVar("T")


async def run_cpu_bound(fn: Callable[..., T], *args, **kwargs) -> T:
    loop = asyncio.get_running_loop()
    pool = cpu_thread_pool()

    def _run() -> T:
        return fn(*args, **kwargs)

    return await loop.run_in_executor(pool, _run)


async def run_cpu_bound_many(
    jobs: list[tuple[Callable[..., T], tuple, dict]],
    *,
    max_concurrent: int | None = None,
) -> list[T]:
    """Run CPU-bound callables concurrently via the shared thread pool."""
    from app.concurrency.pools import effective_cpu_workers
    from app.config import get_settings

    limit = max_concurrent
    if limit is None:
        limit = effective_cpu_workers(get_settings().cpu_thread_pool_size)

    sem = asyncio.Semaphore(max(1, limit))

    async def _one(job: tuple[Callable[..., T], tuple, dict]) -> T:
        fn, args, kwargs = job
        async with sem:
            return await run_cpu_bound(fn, *args, **kwargs)

    return list(await asyncio.gather(*[_one(job) for job in jobs]))
