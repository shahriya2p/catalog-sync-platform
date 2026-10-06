"""Whole-run tests against both supplied mocks.

These are the tests that would catch a regression in the behaviour the business
asked for: the full catalogue arrives, the run reports a definite status,
interrupted runs continue where they stopped, and a completed run does nothing
when it is run again.
"""

from __future__ import annotations

import json

import pytest

from app.clients.product_api import ProductApiClient
from app.clients.warehouse_api import WarehouseApiClient
from app.models import RunStatus
from app.services.catalogue_export import csv_key, manifest_key
from app.services.runner import EXIT_OK, EXIT_PARTIAL, SyncRunner, new_run_id
from app.state.store import RunAlreadyExists, RunNotFound

CATALOGUE_SIZE = 12000
# The mock rejects any SKU ending in 999: P0000999 .. P0011999.
EXPECTED_REJECTS = 12


@pytest.fixture
def runner(settings, store, state, pim_http, recording_wms) -> SyncRunner:
    return SyncRunner(
        settings,
        store=store,
        state=state,
        product_client=ProductApiClient(settings, client=pim_http),
        warehouse_client=WarehouseApiClient(settings, client=recording_wms),
    )


def test_a_full_run_delivers_the_whole_catalogue_once(runner, store, recording_wms):
    outcome = runner.run("run-e2e")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.exit_code == EXIT_OK
    assert outcome.export.product_count == CATALOGUE_SIZE
    assert outcome.export.complete is True

    assert outcome.delivery.accepted == CATALOGUE_SIZE - EXPECTED_REJECTS
    assert outcome.delivery.rejected == EXPECTED_REJECTS
    assert outcome.delivery.unknown == 0

    # Every product was offered to the WMS, and none was written again after the
    # WMS had accepted it. (The mock injects 5xx responses, so some batches are
    # legitimately re-sent: a 5xx means the WMS did not process them.)
    assert len(set(recording_wms.sent_skus)) == CATALOGUE_SIZE
    assert recording_wms.resent_after_acceptance() == []

    # The export is retained alongside a manifest describing it.
    assert store.exists(csv_key("run-e2e"))
    manifest = json.loads(store.get_bytes(manifest_key("run-e2e")).decode("utf-8"))
    assert manifest["product_count"] == CATALOGUE_SIZE
    assert manifest["retention_days"] == 90


def test_counts_reconcile_exactly(runner, state):
    runner.run("run-e2e")
    counts = state.ledger_counts("run-e2e")

    assert sum(counts.values()) == CATALOGUE_SIZE
    assert counts == {
        "ACCEPTED": CATALOGUE_SIZE - EXPECTED_REJECTS,
        "REJECTED": EXPECTED_REJECTS,
    }


def test_rerunning_a_finished_run_sends_nothing(runner, recording_wms):
    runner.run("run-e2e")
    sent = len(recording_wms.sent_skus)

    outcome = runner.resume("run-e2e")

    assert len(recording_wms.sent_skus) == sent, "a resume must be a no-op when done"
    assert outcome.delivery.accepted == 0
    assert outcome.delivery.skipped_already_delivered == CATALOGUE_SIZE
    assert outcome.status is RunStatus.COMPLETED


def test_an_interrupted_run_continues_from_where_it_stopped(
    settings, store, state, pim_http, recording_wms
):
    """Simulates a crash after part of the catalogue was delivered."""
    partial_settings = settings.with_overrides(wms_concurrency=1)
    runner = SyncRunner(
        partial_settings,
        store=store,
        state=state,
        product_client=ProductApiClient(partial_settings, client=pim_http),
        warehouse_client=WarehouseApiClient(partial_settings, client=recording_wms),
    )
    # Export everything, then deliver nothing by stopping after the export.
    export_outcome = runner.export_only("run-crash")
    assert export_outcome.export.product_count == CATALOGUE_SIZE
    assert recording_wms.sent_skus == []

    outcome = runner.resume("run-crash")

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.delivery.accepted == CATALOGUE_SIZE - EXPECTED_REJECTS
    # The PIM was not called again for pages that were already stored.
    assert outcome.metrics.get("PagesSkippedAlreadyStored") == 24
    assert len(set(recording_wms.sent_skus)) == CATALOGUE_SIZE


def test_the_same_run_id_cannot_be_started_twice(runner):
    runner.run("run-e2e")
    with pytest.raises(RunAlreadyExists):
        runner.run("run-e2e")


def test_resuming_an_unknown_run_is_refused(runner):
    with pytest.raises(RunNotFound):
        runner.resume("never-existed")


def test_status_answers_the_operational_questions(runner):
    runner.run("run-e2e")
    status = runner.status("run-e2e")

    assert status["status"] == "COMPLETED"
    assert status["stage"] == "FINALIZE"
    assert status["pages"] == {"complete": 24, "failed": []}
    assert status["batches"] == {"COMPLETED": 120}
    assert status["products"]["ACCEPTED"] == CATALOGUE_SIZE - EXPECTED_REJECTS
    assert status["finished_at"]
    assert status["unknown_sample"] == []


def test_status_lists_recent_runs(runner):
    runner.run("run-e2e")
    listing = runner.status()
    assert [entry["run_id"] for entry in listing["runs"]] == ["run-e2e"]


def test_a_run_with_unknown_products_is_partial_and_not_auto_resent(
    settings, store, state, pim_http
):
    """Ambiguity must surface as PARTIAL, with a non-zero exit code."""
    import httpx

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        skus = [item["sku"] for item in json.loads(request.content)["products"]]
        return httpx.Response(200, json={"accepted": skus, "rejected": []})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=settings.warehouse_api_url
    )
    runner = SyncRunner(
        settings,
        store=store,
        state=state,
        product_client=ProductApiClient(settings, client=pim_http),
        warehouse_client=WarehouseApiClient(settings, client=http),
    )

    outcome = runner.run("run-unknown")

    assert outcome.status is RunStatus.PARTIAL
    assert outcome.exit_code == EXIT_PARTIAL
    assert outcome.delivery.unknown == 100
    assert "unknown outcome" in outcome.reason

    # Reconcile is refused without explicit confirmation.
    refused = runner.reconcile("run-unknown")
    assert refused.status is RunStatus.PARTIAL
    assert "--confirm-resend-unknown" in refused.reason
    assert state.ledger_counts("run-unknown")["UNKNOWN"] == 100

    # With confirmation it resends only those products.
    confirmed = runner.reconcile("run-unknown", confirm=True)
    assert confirmed.status is RunStatus.COMPLETED
    assert state.ledger_counts("run-unknown") == {"ACCEPTED": CATALOGUE_SIZE}


def test_run_ids_are_unique_and_sortable():
    from datetime import datetime, timezone

    moment = datetime(2026, 10, 5, 9, 31, tzinfo=timezone.utc)
    first = new_run_id(moment)
    second = new_run_id(moment)

    assert first.startswith("20261005T0931Z-")
    assert first != second, "a manual re-run must not collide with the scheduled one"


def test_products_from_a_page_missing_in_the_first_attempt_are_delivered_on_resume(
    settings, store, state, pim_http, recording_wms
):
    """Regression: the first attempt's CSV had a gap, so batch numbers drifted.

    Previously the resume skipped the products of page 3 (their positional batch
    was already COMPLETED) and still reported COMPLETED. Every exported product
    must now reach the warehouse, or the run must say it did not.
    """

    import httpx

    class FlakyPim:
        def __init__(self, inner):
            self.inner = inner
            self.fail_page_3 = True

        def get(self, url, **kwargs):
            if self.fail_page_3 and kwargs.get("params", {}).get("page") == 3:
                return httpx.Response(503, text="down", request=httpx.Request("GET", url))
            return self.inner.get(url, **kwargs)

    flaky = FlakyPim(pim_http)
    runner = SyncRunner(
        settings,
        store=store,
        state=state,
        product_client=ProductApiClient(settings, client=flaky),
        warehouse_client=WarehouseApiClient(settings, client=recording_wms),
    )

    first = runner.run("run-gap")
    assert first.status is RunStatus.PARTIAL
    assert first.export.pages_failed == [3]

    flaky.fail_page_3 = False
    second = runner.resume("run-gap")

    assert second.status is RunStatus.COMPLETED
    assert second.delivery.accepted == 500, "all 500 products of page 3 were delivered"
    assert sum(state.ledger_counts("run-gap").values()) == CATALOGUE_SIZE
    assert len(set(recording_wms.sent_skus)) == CATALOGUE_SIZE


def test_a_run_is_never_completed_while_exported_products_are_unaccounted_for(
    settings, store, state, pim_http, recording_wms
):
    runner = SyncRunner(
        settings,
        store=store,
        state=state,
        product_client=ProductApiClient(settings, client=pim_http),
        warehouse_client=WarehouseApiClient(settings, client=recording_wms),
    )
    runner.run("run-accounted")
    # Simulate ledger loss for part of the run.
    import sqlite3

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute("DELETE FROM ledger WHERE run_id = ? AND sku LIKE 'P00001%'", ("run-accounted",))

    outcome = runner._finalize("run-accounted", export=None, delivery=None)

    assert outcome.status is RunStatus.PARTIAL
    assert "never delivered" in outcome.reason
