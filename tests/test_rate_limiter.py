"""The rate limiter is the component that keeps us inside the documented API
limits, so it is tested against a fake clock rather than wall-clock timing:
the assertions are then exact instead of flaky."""

from __future__ import annotations

import threading
from typing import List

from app.clients.rate_limiter import TokenBucket


class FakeClock:
    """A clock that only advances when someone sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_burst_up_to_capacity_is_not_delayed():
    clock = FakeClock()
    bucket = TokenBucket(10, clock=clock.time, sleep=clock.sleep)

    waits = [bucket.acquire() for _ in range(10)]

    assert waits == [0.0] * 10
    assert clock.sleeps == []


def test_sustained_rate_never_exceeds_the_limit():
    clock = FakeClock()
    bucket = TokenBucket(10, clock=clock.time, sleep=clock.sleep)

    requests = 30
    for _ in range(requests):
        bucket.acquire()

    # The invariant a token bucket actually promises: everything issued is
    # covered by the initial burst plus the refill over the elapsed time. Here
    # that means 10 immediately and 20 more at 10/s, so >= 2 seconds passed.
    assert clock.now >= 2.0
    assert requests <= bucket.capacity + bucket.rate * clock.now + 1e-9


def test_refill_allows_another_burst_after_idling():
    clock = FakeClock()
    bucket = TokenBucket(10, clock=clock.time, sleep=clock.sleep)
    for _ in range(10):
        bucket.acquire()

    clock.now += 1.0  # idle for a second

    assert bucket.acquire() == 0.0
    assert bucket.available() <= 10.0  # capacity is never exceeded


def test_capacity_caps_the_burst_even_after_a_long_idle():
    clock = FakeClock()
    bucket = TokenBucket(10, capacity=10, clock=clock.time, sleep=clock.sleep)
    clock.now += 3600.0

    waits = [bucket.acquire() for _ in range(11)]

    assert waits[:10] == [0.0] * 10
    assert waits[10] > 0, "the eleventh request in a burst must wait"


def test_concurrent_callers_share_one_budget():
    """Many threads must not each get their own allowance."""
    clock = FakeClock()
    lock = threading.Lock()

    def sleep(seconds: float) -> None:
        with lock:
            clock.sleep(seconds)

    bucket = TokenBucket(10, clock=clock.time, sleep=sleep)
    results: List[float] = []

    def worker() -> None:
        results.append(bucket.acquire())

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 40
    assert bucket.total_acquired == 40
    # 40 requests at 10/s with a burst of 10 cannot take less than 3 seconds of
    # waiting in total.
    assert bucket.total_wait_seconds >= 3.0


def test_rate_must_be_positive():
    try:
        TokenBucket(0)
    except ValueError:
        return
    raise AssertionError("a non-positive rate must be rejected")
