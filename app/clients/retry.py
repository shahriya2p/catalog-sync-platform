"""Error classification and bounded retry with exponential backoff and jitter.

The classification matters more than the backoff maths. Three outcomes must be
kept apart, because only the first two are safe to repeat for a write:

``TransientApiError``
    The server produced a complete HTTP response telling us it did not process
    the request (429, or a 5xx). Safe to retry under assumptions A1/A2 in
    ARCHITECTURE.md section 8.3.

``PermanentApiError``
    The server rejected the request and will reject it again (400, 401, 413).
    Retrying wastes the rate budget and can never succeed.

``AmbiguousOutcome``
    The request was written to the socket but no response came back (read
    timeout, connection reset mid-flight). For a read this is harmless; for a
    write to the WMS it means the batch may already have been stored. These are
    never retried automatically.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

import httpx

from app.config import RetrySettings

T = TypeVar("T")

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 509})
"""Status codes we treat as 'not processed, try again'.

Note on the supplied mocks: both of them intend to return 429 and 503 but
construct ``JSONResponse(429, {...})`` with the arguments transposed, so the
throttling paths actually surface as HTTP 500 with no ``Retry-After`` header.
Including the whole 5xx family here means the client copes with the mocks and
with the documented behaviour of the real APIs. ``Retry-After`` handling is
exercised by unit tests with an injected transport.
"""


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.body = body


class TransientApiError(ApiError):
    """The request was not processed; retrying is safe."""


class PermanentApiError(ApiError):
    """The request will fail the same way again; do not retry."""


class AmbiguousOutcome(ApiError):
    """No response was received after the request was sent."""


class RetriesExhausted(TransientApiError):
    """All attempts failed with transient errors."""


# Network failures where the request provably never reached the server, so a
# retry cannot duplicate anything.
_SAFE_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

# Network failures that occur after the request bytes were sent. For a GET this
# is still safe; for a POST the server may have processed the request.
_AMBIGUOUS_NETWORK_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header expressed in seconds.

    HTTP also permits an absolute date. We ignore that form deliberately: the
    jittered backoff is a safe fallback, and a misparsed date could produce a
    very long sleep inside a 30-minute run budget.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def classify_response(response: httpx.Response, *, context: str = "") -> None:
    """Raise the appropriate error for a non-2xx response, else return None."""
    if 200 <= response.status_code < 300:
        return None
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    body = response.text[:500]
    message = f"{context} failed with HTTP {response.status_code}".strip()
    if response.status_code in RETRYABLE_STATUS_CODES:
        raise TransientApiError(
            message, status_code=response.status_code, retry_after=retry_after, body=body
        )
    raise PermanentApiError(
        message, status_code=response.status_code, retry_after=retry_after, body=body
    )


def classify_transport_error(
    error: Exception, *, idempotent: bool, context: str = ""
) -> ApiError:
    """Map an httpx transport error onto our three outcome classes.

    ``idempotent`` is True for reads (PIM GETs), where repeating the call has no
    side effect, and False for writes (WMS POSTs).
    """
    message = f"{context} transport error: {type(error).__name__}: {error}".strip()
    if isinstance(error, _SAFE_NETWORK_ERRORS):
        return TransientApiError(message)
    if isinstance(error, _AMBIGUOUS_NETWORK_ERRORS):
        if idempotent:
            return TransientApiError(message)
        return AmbiguousOutcome(message)
    if isinstance(error, httpx.TimeoutException):
        return TransientApiError(message) if idempotent else AmbiguousOutcome(message)
    return TransientApiError(message)


@dataclass
class RetryPolicy:
    """Bounded exponential backoff with full jitter."""

    settings: RetrySettings
    rng: random.Random = None  # type: ignore[assignment]
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random()

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Seconds to wait before ``attempt`` + 1.

        Full jitter (``uniform(0, backoff)``) rather than a fixed sleep: the
        mocks fail on a deterministic request cadence, and the real APIs throttle
        many callers at once, so synchronised retries would collide repeatedly.
        A server-supplied ``Retry-After`` wins when it asks for a longer wait,
        but is still capped so one header cannot stall the run.
        """
        exponential = self.settings.base_delay_seconds * (2 ** max(0, attempt - 1))
        capped = min(self.settings.max_delay_seconds, exponential)
        delay = self.rng.uniform(0, capped)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.settings.max_delay_seconds))
        return delay


def execute_with_retries(
    operation: Callable[[int], T],
    *,
    policy: RetryPolicy,
    context: str,
    logger: Optional[logging.Logger] = None,
    on_retry: Optional[Callable[[int, ApiError, float], None]] = None,
    retry_ambiguous: bool = False,
) -> T:
    """Call ``operation(attempt)`` until it succeeds or the policy is exhausted.

    ``operation`` is expected to raise :class:`TransientApiError`,
    :class:`PermanentApiError` or :class:`AmbiguousOutcome`. Permanent errors
    propagate immediately; ambiguous outcomes propagate unless the caller opts
    in to retrying them.
    """
    log = logger or logging.getLogger(__name__)
    max_attempts = policy.settings.max_attempts
    last_error: Optional[ApiError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation(attempt)
        except PermanentApiError:
            raise
        except (AmbiguousOutcome, TransientApiError) as error:
            if isinstance(error, AmbiguousOutcome) and not retry_ambiguous:
                raise
            last_error = error
            if attempt >= max_attempts:
                break
            delay = policy.delay_for(attempt, error.retry_after)
            log.warning(
                "retrying after transient error",
                extra={
                    "context": context,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "status_code": error.status_code,
                    "retry_after": error.retry_after,
                    "sleep_seconds": round(delay, 3),
                    "error": str(error),
                },
            )
            if on_retry is not None:
                on_retry(attempt, error, delay)
            policy.sleep(delay)
    assert last_error is not None  # loop only breaks after a transient error
    raise RetriesExhausted(
        f"{context} failed after {max_attempts} attempts: {last_error}",
        status_code=last_error.status_code,
        retry_after=last_error.retry_after,
        body=last_error.body,
    )
