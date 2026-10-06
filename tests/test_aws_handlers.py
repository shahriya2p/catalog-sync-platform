"""AWS entry points.

The handlers are thin adapters over the same services the CLI uses, so these
tests check the adapter layer: that each handler is importable, reads its
configuration, and talks to the configured backends. They run against the local
backends, which is what makes them runnable without an AWS account; the queue
fan-out is checked against moto.
"""

from __future__ import annotations

import json

import pytest

from app import aws_handlers
from app.clients.warehouse_api import WarehouseApiClient
from app.models import PageStatus


@pytest.fixture(autouse=True)
def local_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "s3"))
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")


def test_start_run_creates_a_run(state, monkeypatch, tmp_path):
    result = aws_handlers.start_run({"run_id": "run-aws-1"})

    assert result == {"run_id": "run-aws-1", "status": "RUNNING"}
    from app.state.sqlite_store import SqliteRunStateStore

    store = SqliteRunStateStore(str(tmp_path / "state.db"))
    try:
        assert store.get_run("run-aws-1").metadata["trigger"] == "schedule"
    finally:
        store.close()


def test_start_run_generates_a_run_id_when_none_is_given():
    result = aws_handlers.start_run({})
    assert result["run_id"]
    assert result["status"] == "RUNNING"


def test_check_progress_reports_outstanding_work(tmp_path):
    from app.models import BatchStatus
    from app.state.sqlite_store import SqliteRunStateStore

    store = SqliteRunStateStore(str(tmp_path / "state.db"))
    store.create_run("run-aws-2")
    store.register_batch("run-aws-2", 1, sku_count=100, first_row=1)
    store.register_batch("run-aws-2", 2, sku_count=100, first_row=101)
    store.claim_batch("run-aws-2", 2)
    store.finish_batch("run-aws-2", 2, BatchStatus.COMPLETED)
    store.close()

    result = aws_handlers.check_progress({"run_id": "run-aws-2"})

    assert result["outstanding_batches"] == 1
    assert result["batches"] == {"PENDING": 1, "COMPLETED": 1}


def test_finalize_run_decides_the_final_status(tmp_path):
    from app.models import SkuOutcome, SkuState
    from app.state.sqlite_store import SqliteRunStateStore

    store = SqliteRunStateStore(str(tmp_path / "state.db"))
    store.create_run("run-aws-3")
    store.record_page("run-aws-3", 1, PageStatus.COMPLETE, product_count=2)
    store.mark_sent("run-aws-3", [("A1", "h", 1), ("A2", "h", 1)])
    store.record_outcomes(
        "run-aws-3",
        [SkuOutcome("A1", SkuState.ACCEPTED), SkuOutcome("A2", SkuState.UNKNOWN, "timeout")],
    )
    store.close()

    result = aws_handlers.finalize_run({"run_id": "run-aws-3"})

    assert result["status"] == "PARTIAL"
    assert "unknown outcome" in result["reason"]


def test_enqueue_batches_requires_a_queue(tmp_path):
    from app.state.sqlite_store import SqliteRunStateStore

    store = SqliteRunStateStore(str(tmp_path / "state.db"))
    store.create_run("run-aws-4")
    store.close()

    with pytest.raises(ValueError, match="BATCH_QUEUE_URL"):
        aws_handlers.enqueue_batches({"run_id": "run-aws-4"})


def test_enqueue_batches_sends_one_message_per_stored_page(monkeypatch, tmp_path):
    moto = pytest.importorskip("moto", reason="moto is only in requirements-dev.txt")
    import boto3

    from app.state.sqlite_store import SqliteRunStateStore

    store = SqliteRunStateStore(str(tmp_path / "state.db"))
    store.create_run("run-aws-5")
    for page in range(1, 26):  # 25 pages exercises the 10-message batch limit
        store.record_page("run-aws-5", page, PageStatus.COMPLETE, product_count=500)
    store.close()

    with moto.mock_aws():
        sqs = boto3.client("sqs", region_name="eu-west-1")
        queue_url = sqs.create_queue(QueueName="delivery")["QueueUrl"]
        monkeypatch.setenv("BATCH_QUEUE_URL", queue_url)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")

        result = aws_handlers.enqueue_batches({"run_id": "run-aws-5"})

        assert result["pages_queued"] == 25
        attributes = sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        assert int(attributes["Attributes"]["ApproximateNumberOfMessages"]) == 25
        message = sqs.receive_message(QueueUrl=queue_url)["Messages"][0]
        body = json.loads(message["Body"])
        assert body["run_id"] == "run-aws-5"
        assert 1 <= body["page"] <= 25


def test_deliver_batches_reports_per_message_failures(monkeypatch, tmp_path):
    """A message whose page cannot be read must be redelivered, not lost."""
    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps({"run_id": "run-aws-6", "page": 99})}
        ]
    }

    result = aws_handlers.deliver_batches(event)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_deliver_batches_delivers_a_stored_page(monkeypatch, tmp_path, wms_http):
    """End to end through the queue-shaped entry point, local backends."""
    from app.services.catalogue_export import raw_page_key
    from app.state.sqlite_store import SqliteRunStateStore
    from app.storage.object_store import LocalObjectStore

    products = [
        {
            "id": f"P{index:07d}",
            "name": f"Product {index}",
            "category": "HOME",
            "price": 10.0,
            "currency": "INR",
            "updated_at": "2026-09-25T08:30:00Z",
        }
        for index in range(1, 251)
    ]
    store = LocalObjectStore(str(tmp_path / "s3"))
    store.put_bytes(
        raw_page_key("run-aws-7", 1),
        json.dumps({"page": 1, "page_size": 500, "products": products}).encode(),
    )
    state = SqliteRunStateStore(str(tmp_path / "state.db"))
    state.create_run("run-aws-7")
    state.close()

    # Point the handler's client at the in-process mock instead of a network
    # address; everything else in the handler is exercised for real.
    def mock_backed_client(settings, **kwargs):
        return WarehouseApiClient(settings, client=wms_http, **kwargs)

    monkeypatch.setattr(aws_handlers, "WarehouseApiClient", mock_backed_client)

    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps({"run_id": "run-aws-7", "page": 1})}
        ]
    }
    result = aws_handlers.deliver_batches(event)

    assert result == {"batchItemFailures": []}
    state = SqliteRunStateStore(str(tmp_path / "state.db"))
    try:
        counts = state.ledger_counts("run-aws-7")
        assert sum(counts.values()) == 250
        assert counts["ACCEPTED"] == 250
        # Batch numbers are derived from the page offset, so the CSV path and
        # the queue path address the same batches.
        assert {1, 2, 3} <= set(
            batch for batch in range(1, 4) if state.get_batch("run-aws-7", batch)
        )
    finally:
        state.close()
