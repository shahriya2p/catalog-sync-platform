"""WMS client behaviour.

The important assertions here are about what the client refuses to do: it never
treats an unreported SKU as delivered, never retries a batch whose outcome is
unknown, and never retries a payload the WMS has already rejected outright.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from app.clients.warehouse_api import WarehouseApiClient
from app.config import WMS_MAX_BATCH_SIZE, ConfigError, Settings
from app.models import BatchStatus, SkuState


def _products(*skus: str) -> List[Dict[str, Any]]:
    return [
        {
            "sku": sku,
            "description": f"Product {sku}",
            "selling_price": 10.0,
            "currency": "INR",
            "category_code": "SPICES",
            "source_updated_at": "2026-09-25T08:30:00Z",
        }
        for sku in skus
    ]


def _client(settings: Settings, handler, **kwargs) -> WarehouseApiClient:
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    return WarehouseApiClient(settings, client=http, **kwargs)


def test_partial_success_is_split_per_sku(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accepted": ["A1", "A2"],
                "rejected": [{"sku": "B1", "reason": "Invalid warehouse product"}],
                "message": "Processed",
            },
        )

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1", "A2", "B1"))

    assert result.status is BatchStatus.COMPLETED
    assert result.accepted == ["A1", "A2"]
    assert result.rejected == [("B1", "Invalid warehouse product")]
    assert result.unknown == []
    states = {outcome.sku: outcome.state for outcome in result.outcomes()}
    assert states == {
        "A1": SkuState.ACCEPTED,
        "A2": SkuState.ACCEPTED,
        "B1": SkuState.REJECTED,
    }


def test_a_sku_the_wms_did_not_mention_is_unknown_not_accepted(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": ["A1"], "rejected": []})

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1", "A2"))

    assert result.accepted == ["A1"]
    assert result.unknown == ["A2"]
    assert {o.sku: o.state for o in result.outcomes()}["A2"] is SkuState.UNKNOWN


def test_a_rejection_without_a_sku_is_not_attributed_to_a_product(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accepted": [], "rejected": [{"sku": None, "reason": "sku is required"}]},
        )

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert result.rejected == []
    assert result.unknown == ["A1"], "we must not guess which product was rejected"


def test_throttling_is_retried_and_eventually_succeeds(settings):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"accepted": ["A1"], "rejected": []})

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert len(calls) == 3
    assert result.status is BatchStatus.COMPLETED
    assert result.accepted == ["A1"]


def test_persistent_server_errors_are_reported_as_retryable_not_unknown(settings):
    """A complete HTTP error response means the batch was not processed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert result.status is BatchStatus.FAILED_TRANSIENT
    assert result.unknown == []
    assert result.attempts == settings.retry.max_attempts


def test_oversized_batches_are_rejected_before_the_request(settings):
    sent: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(413, text="Maximum batch size is 100")

    with _client(settings, handler) as client:
        with pytest.raises(ValueError):
            client.send_batch(_products(*[f"S{i}" for i in range(101)]))

    assert sent == [], "the client must not spend a request to learn a documented limit"


def test_a_413_is_not_retried(settings):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(413, text="Maximum batch size is 100")

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert len(calls) == 1
    assert result.status is BatchStatus.FAILED_PERMANENT
    assert result.http_status == 413


def test_a_read_timeout_is_unknown_and_sent_only_once(settings):
    """The request reached the WMS; repeating it could duplicate products."""
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1", "A2"))

    assert len(calls) == 1, "an ambiguous write must not be retried"
    assert result.status is BatchStatus.UNKNOWN
    assert result.unknown == ["A1", "A2"]


def test_a_connection_refusal_is_retried_because_nothing_was_sent(settings):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"accepted": ["A1"], "rejected": []})

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert len(calls) == 2
    assert result.accepted == ["A1"]


def test_an_unreadable_200_is_unknown(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")

    with _client(settings, handler) as client:
        result = client.send_batch(_products("A1"))

    assert result.status is BatchStatus.UNKNOWN


def test_an_idempotency_key_is_sent_for_the_day_the_wms_supports_one(settings):
    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"accepted": ["A1"], "rejected": []})

    with _client(settings, handler) as client:
        client.send_batch(_products("A1"), idempotency_key="run-1:7")

    assert seen["idempotency-key"] == "run-1:7"


def test_batch_size_above_the_wms_limit_is_rejected_by_configuration():
    with pytest.raises(ConfigError):
        Settings(batch_size=WMS_MAX_BATCH_SIZE + 1).validate()


def test_against_the_real_mock_rejects_are_reported_with_reasons(settings, wms_http):
    client = WarehouseApiClient(settings, client=wms_http)
    # The mock rejects any SKU ending in 999.
    result = client.send_batch(_products("P0000001", "P0000999"))

    assert result.accepted == ["P0000001"]
    assert result.rejected == [("P0000999", "Invalid warehouse product")]
