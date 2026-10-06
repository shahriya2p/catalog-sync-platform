"""PIM client behaviour.

Covers what the original implementation got wrong: a single fixed-delay retry,
no ``Retry-After`` handling, no distinction between transient and permanent
failures, and no rate limiting.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from app.clients.product_api import ProductApiClient
from app.clients.rate_limiter import TokenBucket
from app.clients.retry import PermanentApiError, RetriesExhausted, RetryPolicy
from app.config import PIM_MAX_PAGE_SIZE, ConfigError, RetrySettings, Settings


def _page_payload(page: int, page_size: int = 2, total: int = 5) -> Dict[str, Any]:
    start = (page - 1) * page_size + 1
    items = [
        {
            "id": f"P{index:07d}",
            "name": f"Product {index}",
            "category": "SPICES",
            "price": 10.0 + index,
            "currency": "INR",
            "updated_at": "2026-09-25T08:30:00Z",
        }
        for index in range(start, min(start + page_size, total + 1))
    ]
    has_next = start + len(items) <= total
    return {
        "products": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": has_next,
        "next_page": page + 1 if has_next else None,
    }


def _client(settings: Settings, handler, **kwargs) -> ProductApiClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=settings.product_api_url)
    return ProductApiClient(settings, client=http, **kwargs)


def test_fetches_a_page_and_reports_pagination(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == settings.product_api_key
        page = int(request.url.params["page"])
        return httpx.Response(200, json=_page_payload(page))

    with _client(settings, handler) as client:
        page = client.fetch_page(1, page_size=2)

    assert [product["id"] for product in page.products] == ["P0000001", "P0000002"]
    assert page.total == 5
    assert page.has_next is True
    assert page.next_page == 2


def test_page_count_is_derived_from_the_total():
    settings = Settings(page_size=500).validate()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = ProductApiClient(settings, client=httpx.Client(transport=transport))
    assert client.page_count(12000) == 24
    assert client.page_count(12001) == 25, "a partial last page still counts"
    assert client.page_count(0) == 0


def test_throttling_is_retried_and_respects_retry_after(settings):
    calls: List[int] = []
    slept: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={"detail": "Rate limit exceeded"}
            )
        return httpx.Response(200, json=_page_payload(1))

    # A policy whose cap is above the server's hint, so the hint is what binds.
    policy = RetryPolicy(
        RetrySettings(max_attempts=4, base_delay_seconds=0.001, max_delay_seconds=30.0),
        sleep=lambda seconds: slept.append(seconds),
    )
    with _client(settings, handler, policy=policy) as client:
        page = client.fetch_page(1, page_size=2)

    assert len(calls) == 2
    assert page.total == 5
    assert slept and slept[0] >= 7, "the server's Retry-After must be honoured"


def test_server_errors_are_retried(settings):
    """The supplied mock reports throttling as a bare 500, so 5xx must retry."""
    statuses = [500, 503, 200]
    seen: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[len(seen)]
        seen.append(status)
        if status == 200:
            return httpx.Response(200, json=_page_payload(1))
        return httpx.Response(status, text="Internal Server Error")

    with _client(settings, handler) as client:
        assert client.fetch_page(1, page_size=2).total == 5
    assert seen == [500, 503, 200]


def test_authentication_failures_are_not_retried(settings):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"detail": "Invalid API key"})

    with _client(settings, handler) as client:
        with pytest.raises(PermanentApiError) as error:
            client.fetch_page(1)

    assert len(calls) == 1, "retrying a bad API key only wastes the rate budget"
    assert error.value.status_code == 401


def test_retries_are_bounded(settings):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    with _client(settings, handler) as client:
        with pytest.raises(RetriesExhausted):
            client.fetch_page(3)

    assert len(calls) == settings.retry.max_attempts


def test_every_attempt_consumes_rate_budget(settings):
    """A retry is a request: it must take a token, or the limit is breached."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_page_payload(1))

    limiter = TokenBucket(1000, name="test")
    with _client(settings, handler, limiter=limiter) as client:
        client.fetch_page(1, page_size=2)

    assert limiter.total_acquired == 3


def test_a_non_json_200_is_permanent(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with _client(settings, handler) as client:
        with pytest.raises(PermanentApiError):
            client.fetch_page(1)


def test_page_size_above_the_pim_limit_is_rejected_by_configuration():
    with pytest.raises(ConfigError):
        Settings(page_size=PIM_MAX_PAGE_SIZE + 1).validate()


def test_real_mock_pagination_covers_the_whole_catalogue(settings, pim_http):
    """Against the supplied mock: every page, including its injected failures."""
    client = ProductApiClient(settings, client=pim_http)
    first = client.fetch_page(1)
    pages = client.page_count(first.total)
    fetched = len(first.products)
    for page in range(2, pages + 1):
        fetched += len(client.fetch_page(page).products)

    assert first.total == 12000
    assert pages == 24
    assert fetched == 12000
