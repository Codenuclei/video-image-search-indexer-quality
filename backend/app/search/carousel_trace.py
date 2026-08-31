"""Structured, filterable tracing for carousel generation API calls.

All carousel logs use the literal prefix ``[carousel]`` so Railway / journalctl
queries can isolate them from Drive search (``[drive-search]``) and other work:

    railway logs --service dfi-backend | rg '\\[carousel\\]'

Each line is key=value so hops can be grepped by ``trace=``, ``event=``,
``route=``, ``provider=``, and ``step=``.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

CAROUSEL_TAG = "[carousel]"
DRIVE_SEARCH_TAG = "[drive-search]"

_trace_id: ContextVar[str] = ContextVar("carousel_trace_id", default="-")
_route: ContextVar[str] = ContextVar("carousel_route", default="-")
_drive_file_id: ContextVar[str] = ContextVar("carousel_drive_file_id", default="-")
_surface: ContextVar[str] = ContextVar("carousel_surface", default="carousel")

logger = logging.getLogger("app.carousel.trace")


def new_trace_id(header_value: str | None = None) -> str:
    raw = (header_value or "").strip()
    if raw:
        return raw[:64]
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return _trace_id.get()


def current_route() -> str:
    return _route.get()


def bind_carousel_context(
    *,
    trace_id: str | None = None,
    route: str | None = None,
    drive_file_id: str | None = None,
) -> tuple[Any, ...]:
    """Bind request-scoped fields; returns tokens for reset."""
    tokens = (
        _trace_id.set(new_trace_id(trace_id) if trace_id is not None else new_trace_id()),
        _route.set((route or "-").strip() or "-"),
        _drive_file_id.set((drive_file_id or "-").strip() or "-"),
        _surface.set("carousel"),
    )
    return tokens


def reset_carousel_context(tokens: tuple[Any, ...]) -> None:
    vars_ = (_trace_id, _route, _drive_file_id, _surface)
    for var, token in zip(vars_, tokens, strict=False):
        try:
            var.reset(token)
        except Exception:  # noqa: BLE001
            pass


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value != value:  # NaN
            return "-"
        return f"{value:.1f}" if abs(value) >= 10 else f"{value:.3f}"
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > 240:
        text = text[:237] + "..."
    if not text:
        return "-"
    if any(ch.isspace() for ch in text):
        return f'"{text}"'
    return text


def carousel_log(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit one structured ``[carousel]`` log line."""
    parts = [
        CAROUSEL_TAG,
        f"trace={_fmt(_trace_id.get())}",
        f"route={_fmt(_route.get())}",
        f"drive={_fmt(_drive_file_id.get())}",
        f"event={_fmt(event)}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_fmt(value)}")
    logger.log(level, " ".join(parts))


def drive_search_log(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit one structured ``[drive-search]`` log line (non-carousel search)."""
    parts = [DRIVE_SEARCH_TAG, f"event={_fmt(event)}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_fmt(value)}")
    logger.log(level, " ".join(parts))


@contextmanager
def carousel_step(step: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log step start/end with elapsed_ms; re-raise after logging failures."""
    started = time.perf_counter()
    meta: dict[str, Any] = {"step": step}
    meta.update(fields)
    carousel_log("step_start", **meta)
    try:
        yield meta
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        carousel_log(
            "step_error",
            level=logging.WARNING,
            step=step,
            elapsed_ms=elapsed_ms,
            error_type=type(exc).__name__,
            error=str(exc)[:200] or type(exc).__name__,
            **{k: v for k, v in fields.items() if k != "step"},
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        extra = {k: v for k, v in meta.items() if k not in {"step", *fields.keys()}}
        carousel_log(
            "step_ok",
            step=step,
            elapsed_ms=elapsed_ms,
            **fields,
            **extra,
        )


def set_drive_file_id(drive_file_id: str | None) -> None:
    if drive_file_id:
        _drive_file_id.set(drive_file_id.strip() or "-")


def set_route(route: str | None) -> None:
    if route:
        _route.set(route.strip() or "-")
