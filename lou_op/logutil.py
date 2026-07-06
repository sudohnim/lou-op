"""Structured logging with contextvars — no more threading an ``emit``
callback through every layer to know "where in the orchestration are we".

Bind ``job_id`` / ``task`` / ``iteration`` once (with :func:`bound` or
:func:`bind`) and every subsequent ``log.info(...)`` carries them
automatically, in this thread and anything it calls.

Live streaming (the CLI drain loop and the SSE ``/logs`` endpoint) is
preserved by a processor that, when a ``job_id`` is bound, renders a compact
human line and pushes it onto that job's queue. The queue registry is keyed
by ``job_id``, so a worker thread only has to bind ``job_id`` for its output
to reach the right consumer — ThreadPoolExecutor does not copy contextvars,
so binding happens inside the worker (see orchestrator ``run_one``).
"""

from __future__ import annotations

import logging
import os
import queue
from typing import Any, Dict, Optional

import structlog
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    unbind_contextvars,
)

# -- per-job live-output queues (SSE + CLI streaming) -------------------------

_queues: Dict[str, "queue.Queue[Optional[str]]"] = {}

# keys that are context/metadata, not part of the human message body
_META_KEYS = {
    "job_id",
    "task",
    "iteration",
    "level",
    "timestamp",
    "logger",
    "phase",
}


def register_queue(job_id: str, q: "queue.Queue[Optional[str]]") -> None:
    _queues[job_id] = q


def unregister_queue(job_id: str) -> None:
    _queues.pop(job_id, None)


def _render_line(event_dict: Dict[str, Any]) -> str:
    """Compact, human-readable line for the live stream.

    ``[task/phase] event key=val`` — the bound context becomes the prefix so
    the message body stays clean.
    """
    task = event_dict.get("task")
    phase = event_dict.get("phase")
    iteration = event_dict.get("iteration")
    prefix = ""
    if task:
        inner = str(task)
        if iteration is not None:
            inner += f":{iteration}"
        if phase:
            inner += f"/{phase}"
        prefix = f"[{inner}] "
    elif phase:
        prefix = f"[{phase}] "
    body = str(event_dict.get("event", ""))
    extras = " ".join(
        f"{k}={v}"
        for k, v in event_dict.items()
        if k not in _META_KEYS and k != "event"
    )
    return f"{prefix}{body}" + (f" {extras}" if extras else "")


def _queue_sink(logger: Any, method_name: str, event_dict: Dict[str, Any]):
    """structlog processor: fan the event out to its job's live queue.

    Returns ``event_dict`` unchanged so the console/JSON renderer still runs.
    """
    job_id = event_dict.get("job_id")
    if job_id:
        q = _queues.get(job_id)
        if q is not None:
            q.put(_render_line(event_dict))
    return event_dict


_configured = False


def configure_logging(
    *, json_logs: Optional[bool] = None, level: Optional[str] = None
) -> None:
    """Idempotent global structlog setup. Safe to call from cli/api/tests."""
    global _configured
    if json_logs is None:
        json_logs = os.getenv("LOU_LOG_JSON", "").lower() in ("1", "true", "yes")
    if level is None:
        level = os.getenv("LOU_LOG_LEVEL", "info")
    log_level = getattr(logging, level.upper(), logging.INFO)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _queue_sink,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "lou_op") -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


# thin re-exports so callers import one module
bind = bind_contextvars
unbind = unbind_contextvars
bound = bound_contextvars

__all__ = [
    "configure_logging",
    "get_logger",
    "register_queue",
    "unregister_queue",
    "bind",
    "unbind",
    "bound",
]
