"""Structured logging and metrics.

Two requirements drive this module:

* operations must be able to answer "is this run running, completed or failed?"
  and investigate a failure from the logs alone, so every line carries the
  ``run_id`` and the stage;
* the same counters must work locally and in CloudWatch, so metrics are
  accumulated in process and flushed either as a plain JSON line (local) or in
  CloudWatch Embedded Metric Format (in AWS), which turns a log line into a
  metric without an extra API call.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import time
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

_context: ContextVar[Dict[str, Any]] = ContextVar("log_context", default={})

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON including contextual fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Install a single stderr handler. Safe to call more than once.

    Logs go to stderr so that stdout carries only the command's result
    document. Mixing the two would make ``python -m app.main status | jq``
    fail the moment a retry warning was emitted, which is exactly when an
    operator is most likely to be piping the output somewhere.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Keep third-party HTTP chatter out of the run log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields (``run_id``, ``stage``, ...) to every log line in scope."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class Metrics:
    """Thread-safe counters flushed as log lines.

    Metric names are deliberately low cardinality (no ``run_id`` dimension) so
    CloudWatch cost stays flat as the catalogue grows; the ``run_id`` is kept as
    an EMF property for correlation instead of a dimension.
    """

    def __init__(
        self,
        namespace: str = "CatalogueSync",
        *,
        emit_emf: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.namespace = namespace
        self.emit_emf = emit_emf
        self._logger = logger or get_logger("app.metrics")
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = {}

    def incr(self, name: str, value: float = 1) -> None:
        
        if value == 0:
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        """Record a latest-value style metric (durations, depths)."""
        with self._lock:
            self._counters[name] = value

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._counters)

    def flush(self, stage: str = "run") -> Dict[str, float]:
        """Emit the accumulated metrics and return them."""
        values = self.snapshot()
        if not values:
            return values
        if self.emit_emf:
            emf = {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self.namespace,
                            "Dimensions": [["Stage"]],
                            "Metrics": [{"Name": name} for name in values],
                        }
                    ],
                },
                "Stage": stage,
                **values,
                **_context.get(),
            }
            # EMF has to reach the log stream, which in Lambda and ECS is
            # stdout; it is not part of the CLI's result document.
            print(json.dumps(emf, default=str, separators=(",", ":")), flush=True)
        else:
            self._logger.info("metrics", extra={"stage": stage, "metrics": values})
        return values


class Timer:
    """Context manager that records elapsed seconds into a metric."""

    def __init__(self, metrics: Metrics, name: str) -> None:
        self._metrics = metrics
        self._name = name
        self.elapsed = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed = time.monotonic() - self._start
        self._metrics.observe(self._name, round(self.elapsed, 3))
