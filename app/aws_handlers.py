"""AWS entry points.

Thin adapters only: every handler builds the configured backends and calls the
same services the local CLI uses, so there is one implementation of the
behaviour and the AWS wiring cannot drift from what is tested locally.

Mapping to the state machine in ARCHITECTURE.md:

``start_run``        StartRun          create the run record
``export_pages``     ExportPages       usually the ECS task (can exceed 15 min)
``enqueue_batches``  EnqueueBatches    one SQS message per stored raw page
``deliver_batches``  DeliveryWorker    SQS-triggered, writes the ledger
``check_progress``   WaitForDelivery   poll until the queue is drained
``finalize_run``     Finalize          decide COMPLETED / PARTIAL / FAILED
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.clients.product_api import ProductApiClient
from app.clients.warehouse_api import WarehouseApiClient
from app.config import load_settings
from app.models import BatchStatus, RunStage
from app.observability import Metrics, get_logger, log_context, setup_logging
from app.services.catalogue_export import CatalogueExporter, raw_page_key
from app.services.runner import SyncRunner, new_run_id
from app.services.warehouse_sync import DeliveryPipeline
from app.state.store import build_state_store
from app.storage.s3 import build_object_store

log = get_logger("app.aws")


def _bootstrap():
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_format)
    metrics = Metrics(settings.metrics_namespace, emit_emf=True)
    return settings, metrics


def start_run(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    settings, metrics = _bootstrap()
    run_id = event.get("run_id") or new_run_id()
    state = build_state_store(settings)
    state.create_run(run_id, metadata={"trigger": event.get("trigger", "schedule")})
    log.info("run created", extra={"run_id": run_id})
    return {"run_id": run_id, "status": "RUNNING"}


def export_pages(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    settings, metrics = _bootstrap()
    run_id = event["run_id"]
    state = build_state_store(settings)
    store = build_object_store(settings)
    with log_context(run_id=run_id), ProductApiClient(settings, metrics=metrics) as client:
        state.update_run(run_id, stage=RunStage.EXPORT)
        result = CatalogueExporter(
            settings, store, state, client, metrics=metrics
        ).export(run_id)
    metrics.flush("export")
    return {"run_id": run_id, **result.as_dict()}


def enqueue_batches(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Fan the stored pages out onto the delivery queue.

    One message per raw page keeps each message tiny and makes redelivery cheap;
    the ledger, not the queue, is what guarantees a redelivered message cannot
    send a product twice.
    """
    import boto3

    settings, metrics = _bootstrap()
    run_id = event["run_id"]
    if not settings.batch_queue_url:
        raise ValueError("BATCH_QUEUE_URL is not configured")
    state = build_state_store(settings)
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    pages = sorted(state.completed_pages(run_id))
    entries: List[Dict[str, Any]] = []
    queued = 0
    for page in pages:
        entries.append(
            {
                "Id": str(page),
                "MessageBody": json.dumps({"run_id": run_id, "page": page}),
                "MessageAttributes": {
                    "run_id": {"DataType": "String", "StringValue": run_id}
                },
            }
        )
        if len(entries) == 10:  # SQS SendMessageBatch limit
            sqs.send_message_batch(QueueUrl=settings.batch_queue_url, Entries=entries)
            queued += len(entries)
            entries = []
    if entries:
        sqs.send_message_batch(QueueUrl=settings.batch_queue_url, Entries=entries)
        queued += len(entries)
    state.update_run(run_id, stage=RunStage.DELIVER, counters={"pages_queued": queued})
    log.info("pages queued for delivery", extra={"run_id": run_id, "pages": queued})
    metrics.incr("PagesQueued", queued)
    metrics.flush("enqueue")
    return {"run_id": run_id, "pages_queued": queued}


def deliver_batches(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """SQS-triggered delivery worker.

    Partial batch failure is reported back to SQS with
    ``batchItemFailures`` so only the messages that genuinely need another
    attempt are redelivered, instead of the whole receive batch.
    """
    settings, metrics = _bootstrap()
    state = build_state_store(settings)
    store = build_object_store(settings)
    failures: List[Dict[str, str]] = []
    with WarehouseApiClient(settings, metrics=metrics) as client:
        pipeline = DeliveryPipeline(settings, state, client, metrics=metrics)
        for record in event.get("Records", []):
            message = json.loads(record["body"])
            run_id, page = message["run_id"], int(message["page"])
            try:
                with log_context(run_id=run_id, page=page):
                    envelope = json.loads(
                        store.get_bytes(raw_page_key(run_id, page)).decode("utf-8")
                    )
                    report = pipeline.deliver_page(
                        run_id,
                        page,
                        envelope.get("products", []),
                        page_size=int(envelope.get("page_size", settings.page_size)),
                    )
                if report.batches_failed:
                    # Transient WMS failures: let SQS redeliver this message.
                    failures.append({"itemIdentifier": record["messageId"]})
            except Exception:
                log.exception(
                    "failed to deliver page", extra={"run_id": run_id, "page": page}
                )
                failures.append({"itemIdentifier": record["messageId"]})
    metrics.flush("deliver")
    return {"batchItemFailures": failures}


def check_progress(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Report whether delivery still has outstanding batches."""
    settings, _ = _bootstrap()
    run_id = event["run_id"]
    state = build_state_store(settings)
    counts = state.batch_counts(run_id)
    outstanding = sum(
        counts.get(status.value, 0)
        for status in (BatchStatus.PENDING, BatchStatus.SENDING, BatchStatus.FAILED_TRANSIENT)
    )
    return {"run_id": run_id, "outstanding_batches": outstanding, "batches": counts}


def finalize_run(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    settings, metrics = _bootstrap()
    run_id = event["run_id"]
    runner = SyncRunner(settings, metrics=metrics)
    outcome = runner.finalize(run_id)
    return {"run_id": run_id, "status": outcome.status.value, "reason": outcome.reason}
