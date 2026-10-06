"""Token bucket rate limiting.

The PIM allows 10 req/s and the WMS 20 req/s. With request latency between
100 ms and 3 s, a single-threaded client cannot reach those rates, so work runs
concurrently. That makes a per-worker ``sleep`` wrong: N workers each sleeping
1/rate seconds produce N times the allowed rate.

One bucket is therefore shared by every worker that talks to a given API. A
caller that finds the bucket empty takes its slot anyway (the balance goes
negative) and waits for exactly its own deficit. That keeps the long-run rate at
``rate`` requests per second, admits a short burst up to ``capacity``, and
serves callers roughly in arrival order instead of making them all wake at the
same moment.

The clock and sleep function are injected so tests can verify the rate with a
fake clock instead of real time.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class TokenBucket:
    def __init__(
        self,
        rate_per_second: float,
        capacity: Optional[float] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        name: str = "",
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self.rate = float(rate_per_second)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_second))
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = self.capacity
        self._updated = clock()
        self.total_acquired = 0
        self.total_wait_seconds = 0.0

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available. Returns the seconds waited."""
        if tokens <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now
            if self._tokens >= tokens:
                wait = 0.0
            else:
                wait = (tokens - self._tokens) / self.rate
            # Reserve unconditionally: the balance may go negative, which is what
            # makes the next caller wait for its own slot rather than racing.
            self._tokens -= tokens
            self.total_acquired += 1
            self.total_wait_seconds += wait
        if wait > 0:
            self._sleep(wait)
        return wait

    def available(self) -> float:
        with self._lock:
            now = self._clock()
            return min(self.capacity, self._tokens + max(0.0, now - self._updated) * self.rate)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TokenBucket(name={self.name!r}, rate={self.rate}, capacity={self.capacity})"
