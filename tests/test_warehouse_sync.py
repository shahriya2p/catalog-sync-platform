"""Delivery stage: batching, partial success, recovery and duplicate protection.

The central property under test is the warehouse team's requirement, stated as
precisely as the implementation can honour it: within a run, a product is sent
to the WMS at most once, and the only way a product is ever sent again after an
ambiguous outcome is an explicit operator action.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List

import httpx
import pytest

from app.clients.warehouse_api import WarehouseApiClient
from app.models import BatchStatus, SkuState
from app.services.warehouse_sync import (
    DeliveryPipeline,
    TransformError,
    iter_batches,
    transform,
    transform_row,
)

HEADER = ["id", "name", "category", "price", "currency", "updated_at"]


def _write_csv(path: Path, count: int, *, start: int = 1) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for index in range(start, start + count):
            writer.writerow(
                {
                    "id": f"P{index:07d}",
                    "name": f"Product {index}",
                    "category": "SPICES",
                    "price": f"{10 + index}.50",
                    "currency": "INR",
                    "updated_at": "2026-09-25T08:30:00Z",
                }
            )
    return str(path)


def _pipeline(settings, state, http, **kwargs) -> DeliveryPipeline:
    client = WarehouseApiClient(settings, client=http, **kwargs)
    return DeliveryPipeline(settings, state, client)


def _sent_skus(request: httpx.Request) -> List[str]:
    return [item["sku"] for item in json.loads(request.content)["products"]]


# --- transformation ---------------------------------------------------------


def test_transform_maps_the_agreed_wms_fields():
    row = {
        "id": "P0000001",
        "name": "Test Product",
        "category": "SPICES",
        "price": "249.50",
        "currency": "INR",
        "updated_at": "2026-09-25T08:30:00Z",
    }
    assert transform(row) == {
        "sku": "P0000001",
        "description": "Test Product",
        "selling_price": 249.50,
        "currency": "INR",
        "category_code": "SPICES",
        "source_updated_at": "2026-09-25T08:30:00Z",
    }


@pytest.mark.parametrize(
    "row",
    [
        {"id": "", "name": "x", "category": "C", "price": "1", "currency": "INR", "updated_at": "t"},
        {"id": "P1", "name": "x", "category": "C", "price": "abc", "currency": "INR", "updated_at": "t"},
        {"id": "P1", "name": "x", "category": "C", "currency": "INR", "updated_at": "t"},
    ],
)
def test_invalid_rows_are_rejected_locally(row):
    """Catching these locally keeps every failure attributable to a product."""
    with pytest.raises(TransformError):
        transform_row(row)


# --- batching ---------------------------------------------------------------


def test_batches_never_exceed_the_wms_limit(tmp_path):
    path = _write_csv(tmp_path / "c.csv", 250)
    batches = list(iter_batches(path, 100))

    assert [len(batch.rows) for batch in batches] == [100, 100, 50]
    assert [batch.batch_no for batch in batches] == [1, 2, 3]
    assert [batch.first_row for batch in batches] == [1, 101, 201]


def test_an_empty_export_produces_no_batches(tmp_path):
    path = _write_csv(tmp_path / "c.csv", 0)
    assert list(iter_batches(path, 100)) == []


# --- delivery against the supplied mock ------------------------------------


def test_delivery_records_accepted_and_rejected_products(
    settings, state, recording_wms, tmp_path
):
    settings = settings.with_overrides(batch_size=100)
    path = _write_csv(tmp_path / "c.csv", 1000)
    state.create_run("run-1")

    report = _pipeline(settings, state, recording_wms).deliver("run-1", path)

    # The mock rejects any SKU ending in 999: P0000999 only, in this range.
    assert report.accepted == 999
    assert report.rejected == 1
    assert report.unknown == 0
    assert report.batches_sent == 10
    assert state.ledger_counts("run-1") == {"ACCEPTED": 999, "REJECTED": 1}
    assert ("P0000999", "Invalid warehouse product") in report.rejected_samples


def test_every_product_is_sent_exactly_once(settings, state, recording_wms, tmp_path):
    path = _write_csv(tmp_path / "c.csv", 600)
    state.create_run("run-1")

    _pipeline(settings, state, recording_wms).deliver("run-1", path)

    assert len(recording_wms.sent_skus) == 600
    assert len(set(recording_wms.sent_skus)) == 600
    assert all(recording_wms.count_for(sku) == 1 for sku in set(recording_wms.sent_skus))


def test_retried_batches_do_not_duplicate_accepted_products(
    settings, state, recording_wms, tmp_path
):
    """The mock fails every 11th and 17th request, so retries really happen.

    A retry resends the batch, which is correct because the failing response
    means the batch was not processed. What must never happen is a product being
    sent twice *after* the WMS accepted it.
    """
    path = _write_csv(tmp_path / "c.csv", 2000)
    state.create_run("run-1")

    report = _pipeline(settings, state, recording_wms).deliver("run-1", path)

    assert report.accepted + report.rejected == 2000
    assert report.unknown == 0
    assert recording_wms.send_count > 20, "the mock's injected failures were retried"
    assert len(set(recording_wms.sent_skus)) == 2000, "every product was sent"
    assert state.ledger_counts("run-1")["ACCEPTED"] == report.accepted

    # The claim being made: a batch may go out again after the WMS answered
    # "not processed", but no product is ever written again once accepted.
    assert recording_wms.resent_after_acceptance() == []
    for sku in set(recording_wms.sent_skus):
        assert recording_wms.count_for(sku) <= settings.retry.max_attempts


def test_a_second_delivery_pass_sends_nothing(settings, state, recording_wms, tmp_path):
    path = _write_csv(tmp_path / "c.csv", 300)
    state.create_run("run-1")
    pipeline = _pipeline(settings, state, recording_wms)
    pipeline.deliver("run-1", path)
    sent_first = len(recording_wms.sent_skus)

    report = pipeline.deliver("run-1", path)

    assert len(recording_wms.sent_skus) == sent_first, "nothing was sent again"
    assert report.accepted == 0
    assert report.batches_skipped == 3
    assert report.skipped_already_delivered == 300


# --- partial and ambiguous outcomes ----------------------------------------


def test_rejected_products_are_not_retried(settings, state, tmp_path):
    attempts: List[List[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skus = _sent_skus(request)
        attempts.append(skus)
        return httpx.Response(
            200,
            json={
                "accepted": skus[1:],
                "rejected": [{"sku": skus[0], "reason": "Invalid warehouse product"}],
            },
        )

    path = _write_csv(tmp_path / "c.csv", 100)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    state.create_run("run-1")
    pipeline = _pipeline(settings, state, http)
    pipeline.deliver("run-1", path)
    pipeline.deliver("run-1", path)

    assert len(attempts) == 1, "a validation failure is not a transient error"
    assert state.ledger_counts("run-1") == {"ACCEPTED": 99, "REJECTED": 1}


def test_an_ambiguous_outcome_is_recorded_and_never_resent(settings, state, tmp_path):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    path = _write_csv(tmp_path / "c.csv", 100)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    state.create_run("run-1")
    pipeline = _pipeline(settings, state, http)

    report = pipeline.deliver("run-1", path)
    assert report.unknown == 100
    assert report.batches_unknown == 1
    assert state.ledger_counts("run-1") == {"UNKNOWN": 100}
    assert len(calls) == 1

    # A resume must leave them alone: they may already be in the warehouse.
    second = pipeline.deliver("run-1", path)
    assert len(calls) == 1, "an unknown product is never resent automatically"
    assert second.accepted == 0
    assert state.get_batch("run-1", 1).status is BatchStatus.UNKNOWN


def test_unknown_products_are_resent_only_when_asked(settings, state, tmp_path):
    """The reconcile path: explicit, targeted, and nothing else moves."""
    # The first batch times out (ambiguous); the second succeeds. After the
    # operator confirms, only the ambiguous batch is sent again.
    timeout_first_batch = {"enabled": True}
    sent: List[List[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skus = _sent_skus(request)
        if timeout_first_batch["enabled"] and skus[0] == "P0000001":
            raise httpx.ReadTimeout("timed out", request=request)
        sent.append(skus)
        return httpx.Response(200, json={"accepted": skus, "rejected": []})

    path = _write_csv(tmp_path / "c.csv", 200)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    state.create_run("run-1")
    pipeline = _pipeline(settings, state, http)

    report = pipeline.deliver("run-1", path)
    assert report.unknown == 100
    assert report.accepted == 100
    assert [batch[0] for batch in sent] == ["P0000101"]

    unknown = [sku for sku, _ in state.entries_in_state("run-1", SkuState.UNKNOWN)]
    assert len(unknown) == 100

    # Reconcile: reopen only the unknown entries and the batches holding them.
    timeout_first_batch["enabled"] = False
    state.reopen_unknown("run-1")
    state.reopen_batches("run-1", {1})
    recovered = pipeline.deliver("run-1", path, only_skus=set(unknown))

    assert recovered.accepted == 100
    assert state.ledger_counts("run-1") == {"ACCEPTED": 200}
    # The batch that had already been accepted was not sent a second time.
    assert [batch[0] for batch in sent] == ["P0000101", "P0000001"]


def test_a_permanent_rejection_fails_the_batch_without_retrying(settings, state, tmp_path):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="products must be an array")

    path = _write_csv(tmp_path / "c.csv", 100)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    state.create_run("run-1")

    report = _pipeline(settings, state, http).deliver("run-1", path)

    assert len(calls) == 1
    assert report.batches_failed == 1
    assert report.failed_validation == 100
    assert state.get_batch("run-1", 1).status is BatchStatus.FAILED_PERMANENT


def test_a_transient_failure_leaves_the_batch_retryable(settings, state, tmp_path):
    failing = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:

        if failing["on"]:
            return httpx.Response(503, text="unavailable")
        skus = [item["sku"] for item in json.loads(request.content)["products"]]
        return httpx.Response(200, json={"accepted": skus, "rejected": []})

    path = _write_csv(tmp_path / "c.csv", 100)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    state.create_run("run-1")
    pipeline = _pipeline(settings, state, http)

    first = pipeline.deliver("run-1", path)
    assert first.batches_failed == 1
    assert state.ledger_counts("run-1") == {"PENDING": 100}

    failing["on"] = False
    second = pipeline.deliver("run-1", path)

    assert second.accepted == 100
    assert state.ledger_counts("run-1") == {"ACCEPTED": 100}


def test_rows_that_cannot_be_transformed_are_recorded_and_not_sent(
    settings, state, recording_wms, tmp_path
):
    path = tmp_path / "c.csv"
    _write_csv(path, 2)
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writerow(
            {
                "id": "P0000003",
                "name": "Bad price",
                "category": "HOME",
                "price": "not-a-number",
                "currency": "INR",
                "updated_at": "2026-09-25T08:30:00Z",
            }
        )

    report = _pipeline(settings, state, recording_wms).deliver("run-1", str(path))

    assert report.failed_validation == 1
    assert report.accepted == 2
    assert "P0000003" not in recording_wms.sent_skus
    assert state.ledger_counts("run-1")["FAILED_VALIDATION"] == 1


def test_a_batch_interrupted_mid_flight_becomes_unknown(settings, state, tmp_path):
    """Crash recovery: SENT with no outcome must not be silently resent."""
    path = _write_csv(tmp_path / "c.csv", 100)
    state.create_run("run-1")

    # Simulate a worker that claimed the batch, wrote the ledger and died.
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    state.claim_batch("run-1", 1)
    state.mark_sent("run-1", [(f"P{i:07d}", "hash", 1) for i in range(1, 101)])

    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        
        calls.append(1)
        return httpx.Response(200, json={"accepted": [], "rejected": []})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    report = _pipeline(settings, state, http).deliver("run-1", path)

    assert calls == [], "a batch that may already be applied is not resent"
    assert report.unknown == 100
    assert state.ledger_counts("run-1") == {"UNKNOWN": 100}
