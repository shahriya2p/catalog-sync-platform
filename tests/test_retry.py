"""Error classification and backoff.

The classification tests matter most: they encode the decision about which
failures are safe to repeat against a system that has no idempotency support.
"""

from __future__ import annotations

import random
from typing import List

import httpx
import pytest

from app.clients.retry import (
    AmbiguousOutcome,
    PermanentApiError,
    RetriesExhausted,
    RetryPolicy,
    TransientApiError,
    classify_response,
    classify_transport_error,
    execute_with_retries,
    parse_retry_after,
)
from app.config import RetrySettings


def _policy(**kwargs) -> RetryPolicy:
    settings = RetrySettings(
        max_attempts=kwargs.pop("max_attempts", 4),
        base_delay_seconds=kwargs.pop("base_delay_seconds", 1.0),
        max_delay_seconds=kwargs.pop("max_delay_seconds", 30.0),
    )
    slept: List[float] = []
    policy = RetryPolicy(
        settings, rng=random.Random(1234), sleep=lambda seconds: slept.append(seconds)
    )
    policy.slept = slept  # type: ignore[attr-defined]
    return policy


def _response(status: int, headers: dict = None) -> httpx.Response:
    return httpx.Response(
        status, headers=headers or {}, request=httpx.Request("GET", "http://x/products")
    )


# --- classification --------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
def test_throttling_and_server_errors_are_transient(status):
    with pytest.raises(TransientApiError):
        classify_response(_response(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_permanent(status):
    with pytest.raises(PermanentApiError):
        classify_response(_response(status))


def test_success_is_not_an_error():
    assert classify_response(_response(200)) is None


def test_retry_after_is_parsed_and_attached():
    with pytest.raises(TransientApiError) as error:
        classify_response(_response(429, {"Retry-After": "2"}))
    assert error.value.retry_after == 2.0


def test_retry_after_dates_are_ignored_rather_than_misparsed():
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("3") == 3.0


def test_connect_failures_are_safe_to_retry_even_for_writes():
    """The request never reached the server, so it cannot have been applied."""
    error = classify_transport_error(httpx.ConnectError("refused"), idempotent=False)
    assert isinstance(error, TransientApiError)


def test_read_timeout_on_a_write_is_ambiguous():
    """The request was sent; the WMS may have stored the batch."""
    error = classify_transport_error(httpx.ReadTimeout("timed out"), idempotent=False)
    assert isinstance(error, AmbiguousOutcome)


def test_read_timeout_on_a_read_is_only_transient():
    error = classify_transport_error(httpx.ReadTimeout("timed out"), idempotent=True)
    assert isinstance(error, TransientApiError)
    assert not isinstance(error, AmbiguousOutcome)


# --- backoff ---------------------------------------------------------------


def test_backoff_uses_full_jitter_within_the_exponential_bound():
    policy = _policy(base_delay_seconds=0.5, max_delay_seconds=30)
    for attempt in range(1, 7):
        bound = min(30, 0.5 * 2 ** (attempt - 1))
        for _ in range(50):
            assert 0 <= policy.delay_for(attempt) <= bound


def test_backoff_is_capped():
    policy = _policy(base_delay_seconds=1, max_delay_seconds=5)
    assert all(policy.delay_for(20) <= 5 for _ in range(50))


def test_retry_after_wins_when_it_asks_for_longer():
    policy = _policy(base_delay_seconds=0.001, max_delay_seconds=30)
    assert policy.delay_for(1, retry_after=10) >= 10


def test_retry_after_is_still_capped():
    policy = _policy(max_delay_seconds=5)
    assert policy.delay_for(1, retry_after=3600) <= 5


# --- the retry loop --------------------------------------------------------


def test_transient_failures_are_retried_then_succeed():
    policy = _policy(max_attempts=4)
    attempts: List[int] = []

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise TransientApiError("503", status_code=503)
        return "ok"

    assert execute_with_retries(operation, policy=policy, context="test") == "ok"
    assert attempts == [1, 2, 3]
    assert len(policy.slept) == 2  # type: ignore[attr-defined]


def test_permanent_failures_are_not_retried():
    policy = _policy()
    calls: List[int] = []

    def operation(attempt: int) -> str:
        calls.append(attempt)
        raise PermanentApiError("413", status_code=413)

    with pytest.raises(PermanentApiError):
        execute_with_retries(operation, policy=policy, context="test")
    assert calls == [1], "a permanent error must not be retried"
    assert policy.slept == []  # type: ignore[attr-defined]


def test_ambiguous_outcomes_are_not_retried_by_default():
    policy = _policy()
    calls: List[int] = []

    def operation(attempt: int) -> str:
        calls.append(attempt)
        raise AmbiguousOutcome("read timeout")

    with pytest.raises(AmbiguousOutcome):
        execute_with_retries(operation, policy=policy, context="test")
    assert calls == [1], "an ambiguous write must never be repeated automatically"


def test_attempts_are_bounded():
    policy = _policy(max_attempts=3)
    calls: List[int] = []

    def operation(attempt: int) -> str:
        calls.append(attempt)
        raise TransientApiError("500", status_code=500)

    with pytest.raises(RetriesExhausted):
        execute_with_retries(operation, policy=policy, context="test")
    assert calls == [1, 2, 3]
