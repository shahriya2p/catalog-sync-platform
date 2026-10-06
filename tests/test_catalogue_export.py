"""Export stage: pagination, checkpointing, partial failure and the manifest."""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, List

import httpx
import pytest

from app.clients.product_api import ProductApiClient
from app.services.catalogue_export import (
    CatalogueExporter,
    ExportAborted,
    csv_key,
    manifest_key,
    raw_page_key,
)
from app.state.store import RunStateStore


def _exporter(settings, store, state, http) -> CatalogueExporter:
    client = ProductApiClient(settings, client=http)
    return CatalogueExporter(settings, store, state, client)


def _manifest(store, run_id: str) -> Dict[str, Any]:
    return json.loads(store.get_bytes(manifest_key(run_id)).decode("utf-8"))


def _csv_rows(store, run_id: str) -> List[Dict[str, str]]:
    body = store.get_bytes(csv_key(run_id)).decode("utf-8").splitlines()
    return list(csv.DictReader(body))


# --- happy path against the supplied mock ----------------------------------


def test_full_export_against_the_mock(settings, store, state, pim_http):
    state.create_run("run-1")
    result = _exporter(settings, store, state, pim_http).export("run-1")

    assert result.product_count == 12000
    assert result.total_reported == 12000
    assert result.page_count == 24
    assert result.pages_failed == []
    assert result.complete is True

    rows = _csv_rows(store, "run-1")
    assert len(rows) == 12000
    assert rows[0]["id"] == "P0000001"
    assert rows[-1]["id"] == "P0012000"
    assert list(rows[0]) == ["id", "name", "category", "price", "currency", "updated_at"]


def test_raw_pages_are_retained_separately_from_the_csv(settings, store, state, pim_http):
    state.create_run("run-1")
    _exporter(settings, store, state, pim_http).export("run-1")

    keys = store.list_keys("raw/run-1/")
    assert len(keys) == 24
    page = json.loads(store.get_bytes(raw_page_key("run-1", 1)).decode("utf-8"))
    assert page["page"] == 1
    assert len(page["products"]) == 500
    assert page["total"] == 12000
    assert "fetched_at" in page


def test_manifest_records_completeness_and_a_checksum(settings, store, state, pim_http):
    state.create_run("run-1")
    result = _exporter(settings, store, state, pim_http).export("run-1")
    manifest = _manifest(store, "run-1")

    assert manifest["product_count"] == 12000
    assert manifest["pim_total"] == 12000
    assert manifest["complete"] is True
    assert manifest["csv_sha256"] == result.checksum
    assert manifest["retention_days"] == 90
    assert manifest["pages_failed"] == []


def test_pages_are_checkpointed_so_a_resume_refetches_nothing(
    settings, store, state, pim_http
):
    state.create_run("run-1")
    exporter = _exporter(settings, store, state, pim_http)
    exporter.export("run-1")

    assert exporter.metrics.snapshot()["PagesFetched"] == 24

    exporter.export("run-1")  # resume: everything is already stored
    snapshot = exporter.metrics.snapshot()

    assert snapshot["PagesFetched"] == 24, "no page was fetched a second time"
    assert snapshot["PagesSkippedAlreadyStored"] == 24


# --- failure handling -------------------------------------------------------


def _page_payload(page: int, page_size: int, total: int) -> Dict[str, Any]:
    start = (page - 1) * page_size + 1
    items = [
        {
            "id": f"P{i:07d}",
            "name": f"Product {i}",
            "category": "HOME",
            "price": 1.5,
            "currency": "INR",
            "updated_at": "2026-09-25T08:30:00Z",
        }
        for i in range(start, min(start + page_size, total + 1))
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


def _handler_failing_page(failing_page: int, page_size: int = 10, total: int = 50):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == failing_page:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_page_payload(page, page_size, total))

    return handler


def test_one_unrecoverable_page_does_not_lose_the_rest(settings, store, state):
    """The requirement: a few failing products must not block the others."""
    settings = settings.with_overrides(page_size=10)
    http = httpx.Client(
        transport=httpx.MockTransport(_handler_failing_page(3)),
        base_url=settings.product_api_url,
    )
    state.create_run("run-1")

    result = _exporter(settings, store, state, http).export("run-1")

    assert result.pages_failed == [3]
    assert result.product_count == 40, "the other four pages are still exported"
    assert result.total_reported == 50
    assert result.complete is False
    assert state.failed_pages("run-1") == [3]
    assert _manifest(store, "run-1")["complete"] is False
    # The gap is visible in the data, not silently filled.
    ids = {row["id"] for row in _csv_rows(store, "run-1")}
    assert "P0000021" not in ids


def test_a_failed_page_is_retried_on_resume_and_completes_the_export(
    settings, store, state
):
    settings = settings.with_overrides(page_size=10)
    state.create_run("run-1")
    flaky = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 3 and flaky["fail"]:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_page_payload(page, 10, 50))

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.product_api_url
    )
    exporter = _exporter(settings, store, state, http)
    first = exporter.export("run-1")
    assert first.pages_failed == [3]

    flaky["fail"] = False
    second = exporter.export("run-1")

    assert second.pages_failed == []
    assert second.product_count == 50
    assert second.complete is True


def test_authentication_failure_aborts_instead_of_retrying_every_page(
    settings, store, state
):
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"detail": "Invalid API key"})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.product_api_url
    )
    state.create_run("run-1")

    with pytest.raises(ExportAborted):
        _exporter(settings, store, state, http).export("run-1")

    assert len(calls) == 1


def test_a_half_written_page_is_not_treated_as_complete(
    settings, store, state: RunStateStore, pim_http
):
    """Storage is written before the checkpoint, never the other way round."""
    state.create_run("run-1")
    exporter = _exporter(settings, store, state, pim_http)
    exporter.export("run-1")

    # Simulate storage loss for one page while its checkpoint still exists.
    from pathlib import Path

    Path(store._path(raw_page_key("run-1", 5))).unlink()
    exporter.export("run-1")

    assert store.exists(raw_page_key("run-1", 5)), "the missing page is fetched again"
    assert exporter.metrics.snapshot()["PagesFetched"] == 25
