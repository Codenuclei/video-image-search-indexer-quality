"""Dedicated executor used by latency-sensitive Library reader requests."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import get_settings

T = TypeVar("T")


class LibraryReaderRuntime:
    """Own a small executor that indexing work never uses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._started_mono = time.monotonic()
        self._thread_id: int | None = None
        self._engine: Engine | None = None
        self._executor = self._new_executor()

    def _new_executor(self) -> ThreadPoolExecutor:
        self._generation += 1
        self._started_mono = time.monotonic()
        self._thread_id = None
        return ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"library-reader-{self._generation}",
        )

    def _mark_thread(self) -> int:
        self._thread_id = threading.get_ident()
        return self._thread_id

    async def warm(self) -> int:
        return await self.run(self._mark_thread)

    async def run(self, func: Callable[..., T], *args: Any) -> T:
        with self._lock:
            executor = self._executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, func, *args)

    def _get_engine(self) -> Engine:
        if self._engine is None:
            url = get_settings().database_url
            if url.startswith("postgresql+asyncpg://"):
                url = "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
            elif url.startswith("postgres://"):
                url = "postgresql+psycopg://" + url[len("postgres://") :]
            elif url.startswith("postgresql://"):
                url = "postgresql+psycopg://" + url[len("postgresql://") :]
            self._engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
                pool_timeout=5,
                connect_args={"connect_timeout": 5},
            )
        return self._engine

    @contextmanager
    def session(self):
        """One sync connection pool used only by the Library reader thread."""
        with Session(self._get_engine(), expire_on_commit=False) as session:
            yield session

    async def restart(self) -> dict[str, object]:
        """Swap the executor without interrupting an in-flight read."""
        with self._lock:
            previous = self._executor
            previous_engine = self._engine
            self._engine = None
            self._executor = self._new_executor()
        if previous_engine is not None:
            previous_engine.dispose()
        previous.shutdown(wait=False, cancel_futures=False)
        await self.warm()
        return self.status()

    def status(self) -> dict[str, object]:
        return {
            "generation": self._generation,
            "thread_alive": self._thread_id is not None,
            "thread_id": self._thread_id,
            "uptime_seconds": round(time.monotonic() - self._started_mono, 3),
            "max_workers": 1,
            "dedicated_db_pool": True,
        }

    def shutdown(self) -> None:
        with self._lock:
            executor = self._executor
            engine = self._engine
            self._engine = None
        if engine is not None:
            engine.dispose()
        executor.shutdown(wait=False, cancel_futures=False)


_runtime = LibraryReaderRuntime()


def get_library_reader_runtime() -> LibraryReaderRuntime:
    return _runtime
