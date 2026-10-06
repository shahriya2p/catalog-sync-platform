"""Stage 2: transform the export and deliver it to the WMS.

This is where "the same product must never be sent twice" is enforced, so the
order of operations is deliberate:

1. ``register_batch`` records the batch as a unit of work (the local stand-in
   for an SQS message).
2. ``claim_batch`` conditionally takes ownership. A batch that is already
   SENDING, COMPLETED, UNKNOWN or permanently failed cannot be claimed, so a
   duplicate delivery attempt stops here.
3. SKUs already in a terminal ledger state are filtered out, so a resume
   re-sends only what was never delivered.
4. The remaining SKUs are written to the ledger as SENT **before** the request
   goes out. If the process dies mid-request, the evidence survives: the entries
   are SENT with no outcome, and recovery marks them UNKNOWN rather than
   silently resending them.
5. The response is applied per SKU. ``accepted`` and ``rejected`` are
   authoritative; a SKU the WMS did not mention is UNKNOWN, never assumed
   delivered.

Rejected products are *not* retried. A business validation failure cannot be
fixed by sending the same payload again; it needs a data correction, so it is
recorded with its reason and surfaced in the run summary.
"""

from __future__ import annotations

import csv
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from app.clients.warehouse_api import WarehouseApiClient
from app.config import Settings
from app.models import (
    BatchResult,
    BatchStatus,
    DeliveryReport,
    SkuOutcome,
    SkuState,
    content_hash,
)
from app.observability import Metrics, Timer, get_logger, log_context
from app.state.store import RunStateStore


class TransformError(ValueError):
    """A CSV row cannot be turned into a valid WMS product."""


def transform(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map an export row to the WMS product format.

    Unchanged from the original implementation: the field mapping is the
    contract agreed with the warehouse team, and there was no reason to alter
    it.
    """
    return {
        "sku": row["id"],
        "description": row["name"],
        "selling_price": float(row["price"]),
        "currency": row["currency"],
        "category_code": row["category"],
        "source_updated_at": row["updated_at"],
    }


def transform_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """``transform`` with validation, raising :class:`TransformError`.

    Catching a bad row here matters for attribution: the WMS rejects a product
    with no SKU using ``{"sku": null}``, which cannot be tied back to a specific
    product, so the row would become an untraceable failure. Validating locally
    keeps every failure attributable.
    """
    try:
        payload = transform(row)
    except (KeyError, TypeError) as error:
        raise TransformError(f"missing field: {error}") from error
    except ValueError as error:
        raise TransformError(f"invalid value: {error}") from error
    if not payload.get("sku"):
        raise TransformError("sku is empty")
    return payload


@dataclass
class _Batch:
    batch_no: int
    first_row: int
    rows: List[Dict[str, Any]]


def iter_batches(csv_path: str, batch_size: int) -> Iterator[_Batch]:
    """Stream the export into batches without loading it into memory."""
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        batch_no = 0
        first_row = 1
        rows: List[Dict[str, Any]] = []
        for index, row in enumerate(reader, start=1):
            if not rows:
                first_row = index
            rows.append(row)
            if len(rows) >= batch_size:
                batch_no += 1
                yield _Batch(batch_no, first_row, rows)
                rows = []
        if rows:
            batch_no += 1
            yield _Batch(batch_no, first_row, rows)


class DeliveryPipeline:
    def __init__(
        self,
        settings: Settings,
        state: RunStateStore,
        client: WarehouseApiClient,
        *,
        metrics: Optional[Metrics] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.client = client
        self.metrics = metrics or Metrics(settings.metrics_namespace)
        self.log = logger or get_logger("app.services.warehouse_sync")
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def deliver(
        self,
        run_id: str,
        csv_path: str,
        *,
        only_skus: Optional[Set[str]] = None,
    ) -> DeliveryReport:
        """Deliver the export to the WMS.

        ``only_skus`` restricts delivery to a specific set (used by
        ``reconcile``); everything else in the file is left untouched.
        """
        report = DeliveryReport()
        with log_context(stage="DELIVER"), Timer(self.metrics, "DeliveryDurationSeconds"):
            # A batch left SENDING by a crashed worker is ambiguous, so it is
            # moved to UNKNOWN before this pass decides what to send.
            reclaimed = self.state.reclaim_stale_batches(run_id)
            if reclaimed:
                self.log.warning(
                    "reclaimed in-flight batches from a previous attempt",
                    extra={"run_id": run_id, "batches": reclaimed},
                )
            self._run_pool(run_id, csv_path, only_skus, report)
        self.log.info("delivery finished", extra={"run_id": run_id, **report.as_dict()})
        return report

    def deliver_page(
        self,
        run_id: str,
        page: int,
        products: Sequence[Dict[str, Any]],
        *,
        page_size: int,
        only_skus: Optional[Set[str]] = None,
    ) -> DeliveryReport:
        """Deliver the products of one stored PIM page.

        This is the unit of work used by the queue-driven worker in AWS: one SQS
        message names one raw page object (a few hundred kB) instead of a row
        range in a CSV that could be hundreds of megabytes.

        Batch numbers are derived from the page's global row offset, so they are
        identical to the numbers the CSV-streaming path produces. That identity
        is what lets the two execution models share one ledger and one batch
        table. It holds because the PIM only ever returns a short page as the
        last page.
        """
        report = DeliveryReport()
        batches: List[_Batch] = []
        base_row = (page - 1) * page_size
        for index in range(0, len(products), self.settings.batch_size):
            rows = list(products[index : index + self.settings.batch_size])
            first_row = base_row + index + 1
            batches.append(
                _Batch(
                    batch_no=(first_row - 1) // self.settings.batch_size + 1,
                    first_row=first_row,
                    rows=rows,
                )
            )
        with log_context(stage="DELIVER", page=page):
            for batch in batches:
                self._process_batch(run_id, batch, only_skus, report)
        self.log.info(
            "page delivered", extra={"run_id": run_id, "page": page, **report.as_dict()}
        )
        return report

    # -- internals ---------------------------------------------------------

    def _run_pool(
        self,
        run_id: str,
        csv_path: str,
        only_skus: Optional[Set[str]],
        report: DeliveryReport,
    ) -> None:
        """Dispatch batches with a bounded window of in-flight work.

        The window keeps memory flat: at a million products there are 10,000
        batches, and building 10,000 futures up front would hold the whole
        catalogue in memory, which is exactly the problem this rewrite exists to
        remove.
        """
        max_in_flight = max(self.settings.wms_concurrency * 2, 4)
        pending = set()
        with ThreadPoolExecutor(
            max_workers=self.settings.wms_concurrency, thread_name_prefix="wms"
        ) as pool:
            for batch in iter_batches(csv_path, self.settings.batch_size):
                pending.add(
                    pool.submit(self._process_batch, run_id, batch, only_skus, report)
                )
                if len(pending) >= max_in_flight:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    self._drain(done)
            if pending:
                done, _ = wait(pending)
                self._drain(done)

    def _drain(self, done: Any) -> None:
        for future in done:
            # Surface a programming error rather than silently losing a batch;
            # API failures are already handled inside _process_batch.
            future.result()

    def _process_batch(
        self,
        run_id: str,
        batch: _Batch,
        only_skus: Optional[Set[str]],
        report: DeliveryReport,
    ) -> None:
        self.state.register_batch(
            run_id, batch.batch_no, sku_count=len(batch.rows), first_row=batch.first_row
        )

        payloads, invalid = self._transform_batch(batch)
        if invalid:
            self.state.record_outcomes(run_id, invalid)
            self._tally(report, failed_validation=len(invalid))

        if not self.state.claim_batch(run_id, batch.batch_no):
            self._handle_unclaimable(run_id, batch, payloads, report)
            leftover = self._undelivered_rows(run_id, batch, payloads, only_skus)
            if leftover:
                # The batch number is taken, but these products were not part
                # of what that batch delivered. Positional batch numbers drift
                # when an earlier export had a gap, so send them under a fresh
                # number instead of silently skipping them.
                self.log.warning(
                    "products missing from an already-terminal batch; resending under a new batch",
                    extra={"batch_no": batch.batch_no, "products": len(leftover)},
                )
                self.metrics.incr("ProductsRecoveredIntoNewBatch", len(leftover))
                self._process_batch(
                    run_id,
                    _Batch(self.state.allocate_batch_no(run_id), batch.first_row, leftover),
                    only_skus,
                    report,
                )
            return

        skus = [str(payload["sku"]) for payload in payloads]
        terminal = self.state.already_terminal(run_id, skus)
        deliverable = [
            payload
            for payload in payloads
            if str(payload["sku"]) not in terminal
            and (only_skus is None or str(payload["sku"]) in only_skus)
        ]
        skipped = len(payloads) - len(deliverable)

        if not deliverable:
            self.state.finish_batch(
                run_id,
                batch.batch_no,
                BatchStatus.SKIPPED,
                detail="nothing left to deliver",
            )
            self.metrics.incr("BatchesSkipped")
            self._tally(report, batches_skipped=1, skipped=skipped)
            return

        hashes = {str(payload["sku"]): content_hash(payload) for payload in deliverable}
        self.state.mark_sent(
            run_id,
            [(sku, hashes[sku], batch.batch_no) for sku in hashes],
        )

        result = self.client.send_batch(
            deliverable,
            idempotency_key=f"{run_id}:{batch.batch_no}",
        )
        self._apply_result(run_id, batch, deliverable, result, report, skipped)

    def _transform_batch(self, batch: _Batch) -> Tuple[List[Dict[str, Any]], List[SkuOutcome]]:
        payloads: List[Dict[str, Any]] = []
        invalid: List[SkuOutcome] = []
        for index, row in enumerate(batch.rows):
            try:
                payloads.append(transform_row(row))
            except TransformError as error:
                sku = str(row.get("id") or f"row-{batch.first_row + index}")
                self.log.warning(
                    "row failed local validation and was not sent",
                    extra={"sku": sku, "reason": str(error), "batch_no": batch.batch_no},
                )
                self.metrics.incr("ProductsFailedValidation")
                invalid.append(SkuOutcome(sku, SkuState.FAILED_VALIDATION, str(error)))
        return payloads, invalid

    def _undelivered_rows(
        self,
        run_id: str,
        batch: _Batch,
        payloads: Sequence[Dict[str, Any]],
        only_skus: Optional[Set[str]],
    ) -> List[Dict[str, Any]]:
        """Rows of this batch whose products are still owed a delivery."""
        skus = [str(payload["sku"]) for payload in payloads]
        terminal = self.state.already_terminal(run_id, skus)
        owed = {
            sku
            for sku in skus
            if sku not in terminal and (only_skus is None or sku in only_skus)
        }
        return [row for row in batch.rows if str(row.get("id")) in owed]

    def _handle_unclaimable(
        self,
        run_id: str,
        batch: _Batch,
        payloads: Sequence[Dict[str, Any]],
        report: DeliveryReport,
    ) -> None:
        """The batch is owned elsewhere or already terminal.

        The one case that still needs work is a batch recovered from a crashed
        worker (now UNKNOWN): its SKUs may still sit in SENT, and they have to be
        marked UNKNOWN so the run reports them honestly instead of leaving them
        in a transient state for ever.
        """
        record = self.state.get_batch(run_id, batch.batch_no)
        status = record.status if record else None
        if status is BatchStatus.UNKNOWN and payloads:
            skus = [str(payload["sku"]) for payload in payloads]
            terminal = self.state.already_terminal(run_id, skus)
            stranded = [
                SkuOutcome(
                    sku,
                    SkuState.UNKNOWN,
                    record.detail if record else "ambiguous outcome",
                )
                for sku in skus
                if sku not in terminal
            ]
            if stranded:
                self.state.record_outcomes(run_id, stranded)
                self._tally(report, unknown=len(stranded))
                self.log.error(
                    "batch outcome is unknown; products are not resent automatically",
                    extra={"batch_no": batch.batch_no, "products": len(stranded)},
                )
        self.metrics.incr("BatchesSkipped")
        self._tally(report, batches_skipped=1, skipped=len(payloads))

    def _apply_result(
        self,
        run_id: str,
        batch: _Batch,
        deliverable: Sequence[Dict[str, Any]],
        result: BatchResult,
        report: DeliveryReport,
        skipped: int,
    ) -> None:
        skus = [str(payload["sku"]) for payload in deliverable]

        if result.status is BatchStatus.COMPLETED:
            self.state.record_outcomes(run_id, result.outcomes())
            self.state.finish_batch(
                run_id, batch.batch_no, BatchStatus.COMPLETED, attempts=result.attempts
            )
            self._tally(
                report,
                accepted=len(result.accepted),
                rejected=len(result.rejected),
                unknown=len(result.unknown),
                batches_sent=1,
                skipped=skipped,
                rejected_samples=result.rejected,
            )
            return

        if result.status is BatchStatus.UNKNOWN:
            # Ambiguous: the WMS may hold these products already. Mark UNKNOWN
            # and stop. Only an explicit reconcile may resend them.
            self.state.record_outcomes(
                run_id,
                [SkuOutcome(sku, SkuState.UNKNOWN, result.detail) for sku in skus],
            )
            self.state.finish_batch(
                run_id,
                batch.batch_no,
                BatchStatus.UNKNOWN,
                attempts=result.attempts,
                detail=result.detail,
            )
            self._tally(report, unknown=len(skus), batches_unknown=1, skipped=skipped)
            return

        if result.status is BatchStatus.FAILED_TRANSIENT:
            # The WMS answered with an error status every time, so it did not
            # process the batch (assumptions A1/A2). Returning the SKUs to
            # PENDING lets a resume retry them without any ambiguity.
            self.state.record_outcomes(
                run_id,
                [SkuOutcome(sku, SkuState.PENDING, result.detail) for sku in skus],
            )
            self.state.finish_batch(
                run_id,
                batch.batch_no,
                BatchStatus.FAILED_TRANSIENT,
                attempts=result.attempts,
                detail=result.detail,
            )
            self._tally(report, batches_failed=1, skipped=skipped)
            return

        # FAILED_PERMANENT: our payload or credentials are wrong. Do not retry.
        self.state.record_outcomes(
            run_id,
            [SkuOutcome(sku, SkuState.FAILED_VALIDATION, result.detail) for sku in skus],
        )
        self.state.finish_batch(
            run_id,
            batch.batch_no,
            BatchStatus.FAILED_PERMANENT,
            attempts=result.attempts,
            detail=result.detail,
        )
        self._tally(
            report, failed_validation=len(skus), batches_failed=1, skipped=skipped
        )

    def _tally(
        self,
        report: DeliveryReport,
        *,
        accepted: int = 0,
        rejected: int = 0,
        unknown: int = 0,
        failed_validation: int = 0,
        skipped: int = 0,
        batches_sent: int = 0,
        batches_skipped: int = 0,
        batches_failed: int = 0,
        batches_unknown: int = 0,
        rejected_samples: Sequence[Tuple[str, str]] = (),
    ) -> None:
        with self._lock:
            report.accepted += accepted
            report.rejected += rejected
            report.unknown += unknown
            report.failed_validation += failed_validation
            report.skipped_already_delivered += skipped
            report.batches_sent += batches_sent
            report.batches_skipped += batches_skipped
            report.batches_failed += batches_failed
            report.batches_unknown += batches_unknown
            for sample in rejected_samples:
                if len(report.rejected_samples) < 20:
                    report.rejected_samples.append(sample)
