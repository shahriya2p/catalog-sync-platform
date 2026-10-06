"""The run-state contract, run against both backends.

The local SQLite store and the production DynamoDB store must behave
identically, because the duplicate-protection guarantees are expressed as
conditional updates and it would be worthless to prove them only against the
backend that never runs in production.

The DynamoDB cases need moto (``pip install -r requirements-dev.txt``); they
skip themselves if it is absent so the suite still passes without it.
"""

from __future__ import annotations

import pytest

from app.models import BatchStatus, PageStatus, RunStatus, SkuOutcome, SkuState
from app.state.store import RunAlreadyExists


def _create_dynamodb_tables(resource) -> dict:
    names = {
        "runs_table": "test-runs",
        "pages_table": "test-pages",
        "batches_table": "test-batches",
        "ledger_table": "test-ledger",
        "exceptions_table": "test-exceptions",
    }
    common = {"BillingMode": "PAY_PER_REQUEST"}
    resource.create_table(
        TableName=names["runs_table"],
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        **common,
    )
    resource.create_table(
        TableName=names["pages_table"],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "page", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "page", "AttributeType": "N"},
        ],
        **common,
    )
    resource.create_table(
        TableName=names["batches_table"],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "batch_no", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "batch_no", "AttributeType": "N"},
        ],
        **common,
    )
    resource.create_table(
        TableName=names["ledger_table"],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        **common,
    )
    resource.create_table(
        TableName=names["exceptions_table"],
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "sku", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "sku", "AttributeType": "S"},
        ],
        **common,
    )
    return names


@pytest.fixture(params=["sqlite", "dynamodb"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        from app.state.sqlite_store import SqliteRunStateStore

        store = SqliteRunStateStore(str(tmp_path / "state.db"))
        yield store
        store.close()
        return

    moto = pytest.importorskip("moto", reason="moto is only in requirements-dev.txt")
    import boto3

    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="eu-west-1")
        names = _create_dynamodb_tables(resource)
        from app.state.dynamodb_store import DynamoDbRunStateStore

        yield DynamoDbRunStateStore(resource=resource, **names)


# --- runs ------------------------------------------------------------------


def test_creating_the_same_run_twice_is_refused(backend):
    backend.create_run("run-1", metadata={"page_size": 500})
    with pytest.raises(RunAlreadyExists):
        backend.create_run("run-1")


def test_run_status_round_trips(backend):
    backend.create_run("run-1")
    backend.update_run("run-1", status=RunStatus.PARTIAL, counters={"pages": 24}, finished=True)

    record = backend.get_run("run-1")
    assert record.status is RunStatus.PARTIAL
    assert record.counters["pages"] == 24
    assert record.finished_at


def test_runs_can_be_listed(backend):
    backend.create_run("run-1")
    backend.create_run("run-2")
    assert {record.run_id for record in backend.list_runs()} == {"run-1", "run-2"}


# --- pages -----------------------------------------------------------------


def test_page_checkpoints_drive_resume(backend):
    backend.create_run("run-1")
    backend.record_page("run-1", 1, PageStatus.COMPLETE, product_count=500, checksum="x")
    backend.record_page("run-1", 2, PageStatus.FAILED, detail="503")

    assert set(backend.completed_pages("run-1")) == {1}
    assert backend.failed_pages("run-1") == [2]
    assert backend.get_page("run-1", 1).product_count == 500


# --- batches ---------------------------------------------------------------


def test_a_batch_is_claimable_exactly_once(backend):
    backend.create_run("run-1")
    backend.register_batch("run-1", 1, sku_count=100, first_row=1)

    assert backend.claim_batch("run-1", 1) is True
    assert backend.claim_batch("run-1", 1) is False


def test_terminal_batches_are_not_reclaimed_but_transient_ones_are(backend):
    backend.create_run("run-1")
    for batch_no, status in ((1, BatchStatus.COMPLETED), (2, BatchStatus.FAILED_TRANSIENT)):
        backend.register_batch("run-1", batch_no, sku_count=10, first_row=1)
        backend.claim_batch("run-1", batch_no)
        backend.finish_batch("run-1", batch_no, status)

    assert backend.claim_batch("run-1", 1) is False
    assert backend.claim_batch("run-1", 2) is True


def test_in_flight_batches_are_recovered_as_unknown(backend):
    backend.create_run("run-1")
    backend.register_batch("run-1", 1, sku_count=10, first_row=1)
    backend.claim_batch("run-1", 1)

    assert backend.reclaim_stale_batches("run-1") == 1
    assert backend.get_batch("run-1", 1).status is BatchStatus.UNKNOWN


# --- ledger ----------------------------------------------------------------


def test_an_accepted_sku_can_never_be_marked_sendable_again(backend):
    backend.create_run("run-1")
    backend.mark_sent("run-1", [("A1", "hash", 1)])
    backend.record_outcomes("run-1", [SkuOutcome("A1", SkuState.ACCEPTED)])

    backend.mark_sent("run-1", [("A1", "hash", 2)])  # redelivered message

    assert backend.already_terminal("run-1", ["A1"]) == {"A1"}
    assert backend.ledger_counts("run-1").get("ACCEPTED") == 1


def test_already_terminal_filters_only_undeliverable_skus(backend):
    backend.create_run("run-1")
    backend.mark_sent("run-1", [("A1", "h", 1), ("A2", "h", 1), ("A3", "h", 1)])
    backend.record_outcomes(
        "run-1",
        [
            SkuOutcome("A1", SkuState.ACCEPTED),
            SkuOutcome("A2", SkuState.REJECTED, "invalid"),
            SkuOutcome("A3", SkuState.UNKNOWN, "timeout"),
        ],
    )

    assert backend.already_terminal("run-1", ["A1", "A2", "A3", "A4"]) == {"A1", "A2", "A3"}


def test_unknown_entries_are_listed_and_reopened_only_on_request(backend):
    backend.create_run("run-1")
    backend.mark_sent("run-1", [("A1", "h", 4)])
    backend.record_outcomes("run-1", [SkuOutcome("A1", SkuState.UNKNOWN, "timeout")])

    assert [sku for sku, _ in backend.entries_in_state("run-1", SkuState.UNKNOWN)] == ["A1"]

    assert backend.reopen_unknown("run-1") == 1
    assert backend.already_terminal("run-1", ["A1"]) == set()


def test_an_outcome_without_a_prior_send_is_still_recorded(backend):
    backend.create_run("run-1")
    backend.record_outcomes(
        "run-1", [SkuOutcome("A9", SkuState.FAILED_VALIDATION, "sku is empty")]
    )

    assert backend.already_terminal("run-1", ["A9"]) == {"A9"}
    assert backend.ledger_counts("run-1").get("FAILED_VALIDATION") == 1
