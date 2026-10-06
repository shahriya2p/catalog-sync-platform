"""Run orchestration: the local stand-in for the Step Functions state machine.

The stages, their order, and the rules for deciding a final status are defined
here once and used by both the local CLI and the AWS handlers, so the behaviour
operations see locally is the behaviour they get in production.

Final status rules:

``COMPLETED``
    Every page was exported and every product reached a definite outcome.
    Business rejections do not make a run unsuccessful - the warehouse told us
    exactly what it thinks of those products - but they are always reported.

``PARTIAL``
    Something is missing or ambiguous: failed pages, failed batches, or any
    UNKNOWN product. The run needs a human decision, and a resume or reconcile
    can finish it without redoing the successful work.

``FAILED``
    The run could not produce a usable export at all.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.clients.product_api import ProductApiClient
from app.clients.warehouse_api import WarehouseApiClient
from app.config import Settings
from app.models import (
    DeliveryReport,
    ExportResult,
    RunStage,
    RunStatus,
    SkuState,
)
from app.observability import Metrics, Timer, get_logger, log_context, setup_logging
from app.services.catalogue_export import (
    CatalogueExporter,
    ExportAborted,
    csv_key,
)
from app.services.warehouse_sync import DeliveryPipeline
from app.state.store import RunAlreadyExists, RunNotFound, RunStateStore, build_state_store
from app.storage.object_store import ObjectStore
from app.storage.s3 import build_object_store

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2


def new_run_id(now: Optional[datetime] = None) -> str:
    """A sortable, human-readable run id: ``20261005T0931Z-3f9c``.

    The timestamp makes runs sort chronologically and ties a run to its business
    date; the random suffix keeps a manual re-run on the same day distinct from
    the scheduled one, so its state and its S3 export never collide.
    """
    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%MZ')}-{secrets.token_hex(2)}"


@dataclass
class RunOutcome:
    run_id: str
    status: RunStatus
    export: Optional[ExportResult] = None
    delivery: Optional[DeliveryReport] = None
    reason: Optional[str] = None
    metrics: Dict[str, float] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        # RUNNING is a success for a single-stage command (the run continues
        # elsewhere); only PARTIAL and FAILED are non-zero.
        if self.status in (RunStatus.COMPLETED, RunStatus.RUNNING):
            return EXIT_OK
        if self.status is RunStatus.PARTIAL:
            return EXIT_PARTIAL
        return EXIT_FAILED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "reason": self.reason,
            "export": self.export.as_dict() if self.export else None,
            "delivery": self.delivery.as_dict() if self.delivery else None,
            "metrics": self.metrics,
        }


class SyncRunner:
    """Owns one synchronisation run from start to finish."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: Optional[ObjectStore] = None,
        state: Optional[RunStateStore] = None,
        product_client: Optional[ProductApiClient] = None,
        warehouse_client: Optional[WarehouseApiClient] = None,
        metrics: Optional[Metrics] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.settings = settings
        self.store = store or build_object_store(settings)
        self.state = state or build_state_store(settings)
        self.metrics = metrics or Metrics(
            settings.metrics_namespace, emit_emf=settings.emit_emf_metrics
        )
        self.log = logger or get_logger("app.runner")
        self._product_client = product_client
        self._warehouse_client = warehouse_client
        self._owns_product_client = product_client is None
        self._owns_warehouse_client = warehouse_client is None

    # -- lifecycle ---------------------------------------------------------

    @property
    def product_client(self) -> ProductApiClient:
        if self._product_client is None:
            self._product_client = ProductApiClient(
                self.settings, metrics=self.metrics
            )
        return self._product_client

    @property
    def warehouse_client(self) -> WarehouseApiClient:
        if self._warehouse_client is None:
            self._warehouse_client = WarehouseApiClient(
                self.settings, metrics=self.metrics
            )
        return self._warehouse_client

    def close(self) -> None:
        if self._owns_product_client and self._product_client is not None:
            self._product_client.close()
        if self._owns_warehouse_client and self._warehouse_client is not None:
            self._warehouse_client.close()

    def __enter__(self) -> "SyncRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- commands ----------------------------------------------------------

    def run(self, run_id: Optional[str] = None) -> RunOutcome:
        """Start a new run. Fails fast if the run id already exists."""
        identifier = run_id or new_run_id()
        try:
            self.state.create_run(
                identifier,
                metadata={
                    "page_size": self.settings.page_size,
                    "batch_size": self.settings.batch_size,
                    "storage_backend": self.settings.storage_backend,
                },
            )
        except RunAlreadyExists:
            raise RunAlreadyExists(
                f"run {identifier} already exists; use 'resume {identifier}' to continue it"
            )
        return self._execute(identifier)

    def resume(self, run_id: str) -> RunOutcome:
        """Continue an existing run, redoing only what is missing."""
        record = self.state.get_run(run_id)
        if record is None:
            raise RunNotFound(run_id)
        self.state.update_run(run_id, status=RunStatus.RUNNING)
        self.log.info(
            "resuming run",
            extra={
                "run_id": run_id,
                "previous_status": record.status.value,
                "previous_stage": record.stage.value,
            },
        )
        return self._execute(run_id, resumed=True)

    def reconcile(self, run_id: str, *, confirm: bool = False) -> RunOutcome:
        """Resend products whose outcome is unknown.

        This is the only operation that can create a duplicate in the warehouse,
        because an UNKNOWN product may already be stored there. It therefore
        requires explicit confirmation and never runs as part of a scheduled
        run. See ARCHITECTURE.md section 8.
        """
        record = self.state.get_run(run_id)
        if record is None:
            raise RunNotFound(run_id)
        unknown = self.state.entries_in_state(run_id, SkuState.UNKNOWN)
        if not unknown:
            self.log.info("nothing to reconcile", extra={"run_id": run_id})
            return RunOutcome(run_id, record.status, reason="no unknown products")
        if not (confirm or self.settings.resend_unknown):
            return RunOutcome(
                run_id,
                record.status,
                reason=(
                    f"{len(unknown)} products have an unknown outcome. Resending them may "
                    "duplicate products the WMS already stored, so it needs "
                    "--confirm-resend-unknown."
                ),
            )

        skus: Set[str] = {sku for sku, _ in unknown}
        batch_numbers = {batch_no for _, batch_no in unknown if batch_no}
        with log_context(run_id=run_id, stage=RunStage.RECONCILE.value):
            self.state.update_run(run_id, stage=RunStage.RECONCILE, status=RunStatus.RUNNING)
            self.state.reopen_unknown(run_id)
            self.state.reopen_batches(run_id, batch_numbers)
            local_csv = self._local_csv(run_id)
            self.log.warning(
                "reconciling products with an unknown outcome; duplicates are possible",
                extra={"run_id": run_id, "products": len(skus), "batches": len(batch_numbers)},
            )
            report = DeliveryPipeline(
                self.settings,
                self.state,
                self.warehouse_client,
                metrics=self.metrics,
                logger=self.log,
            ).deliver(run_id, local_csv, only_skus=skus)
        return self._finalize(run_id, export=None, delivery=report, reconciled=True)

    def export_only(self, run_id: str, *, create_if_missing: bool = True) -> RunOutcome:
        """Run the export stage alone.

        This is the ECS task in the AWS design: the export can exceed Lambda's
        15-minute limit, and delivery happens separately through the queue. It
        is also the useful command when investigating a PIM problem locally.
        """
        if self.state.get_run(run_id) is None:
            if not create_if_missing:
                raise RunNotFound(run_id)
            self.state.create_run(run_id, metadata={"stage_only": "export"})
        with log_context(run_id=run_id):
            self.state.update_run(run_id, status=RunStatus.RUNNING, stage=RunStage.EXPORT)
            try:
                export = CatalogueExporter(
                    self.settings,
                    self.store,
                    self.state,
                    self.product_client,
                    metrics=self.metrics,
                    logger=self.log,
                ).export(run_id)
            except ExportAborted as error:
                self.state.update_run(
                    run_id,
                    status=RunStatus.FAILED,
                    counters={"error": str(error)},
                    finished=True,
                )
                self.metrics.flush("export")
                return RunOutcome(run_id, RunStatus.FAILED, reason=str(error))
            self.state.update_run(
                run_id,
                stage=RunStage.DELIVER,
                counters={
                    "products_exported": export.product_count,
                    "pages": export.page_count,
                    "pages_failed": len(export.pages_failed),
                    "pim_total": export.total_reported,
                },
                metadata={"csv_key": export.csv_key, "manifest_key": export.manifest_key},
            )
        status = RunStatus.RUNNING if export.complete else RunStatus.PARTIAL
        return RunOutcome(
            run_id,
            status,
            export=export,
            reason=None if export.complete else "export incomplete",
            metrics=self.metrics.flush("export"),
        )

    def finalize(self, run_id: str) -> RunOutcome:
        """Decide and record the final status of a run.

        Used by the ``Finalize`` state of the AWS state machine, where the
        export and delivery stages ran in different processes, so the outcome
        has to be derived from the state store rather than from in-memory
        results.
        """
        if self.state.get_run(run_id) is None:
            raise RunNotFound(run_id)
        return self._finalize(run_id, export=None, delivery=None)

    def status(self, run_id: Optional[str] = None, *, limit: int = 10) -> Dict[str, Any]:
        """Everything operations needs to answer 'what happened to this run?'."""
        if run_id is None:
            latest = self.state.list_runs(limit=limit)
            return {"runs": [self._summarise(record.run_id) for record in latest]}
        record = self.state.get_run(run_id)
        if record is None:
            raise RunNotFound(run_id)
        return self._summarise(run_id)

    # -- execution ---------------------------------------------------------

    def _execute(self, run_id: str, *, resumed: bool = False) -> RunOutcome:
        with log_context(run_id=run_id), Timer(self.metrics, "RunDurationSeconds"):
            self.log.info(
                "run started",
                extra={
                    "run_id": run_id,
                    "resumed": resumed,
                    "page_size": self.settings.page_size,
                    "batch_size": self.settings.batch_size,
                    "pim_rps": self.settings.pim_requests_per_second,
                    "wms_rps": self.settings.wms_requests_per_second,
                },
            )
            try:
                self.state.update_run(run_id, stage=RunStage.EXPORT)
                export = CatalogueExporter(
                    self.settings,
                    self.store,
                    self.state,
                    self.product_client,
                    metrics=self.metrics,
                    logger=self.log,
                ).export(run_id)
            except ExportAborted as error:
                self.log.error("export aborted", extra={"run_id": run_id, "error": str(error)})
                self.state.update_run(
                    run_id,
                    status=RunStatus.FAILED,
                    stage=RunStage.EXPORT,
                    counters={"error": str(error)},
                    finished=True,
                )
                self.metrics.incr("RunsFailed")
                self.metrics.flush("export")
                return RunOutcome(run_id, RunStatus.FAILED, reason=str(error))

            self.state.update_run(
                run_id,
                stage=RunStage.DELIVER,
                counters={
                    "products_exported": export.product_count,
                    "pages": export.page_count,
                    "pages_failed": len(export.pages_failed),
                    "pim_total": export.total_reported,
                },
                metadata={"csv_key": export.csv_key, "manifest_key": export.manifest_key},
            )

            if export.product_count == 0:
                reason = "export produced no products"
                self.state.update_run(
                    run_id, status=RunStatus.FAILED, counters={"error": reason}, finished=True
                )
                self.metrics.incr("RunsFailed")
                self.metrics.flush("run")
                return RunOutcome(run_id, RunStatus.FAILED, export=export, reason=reason)

            delivery = DeliveryPipeline(
                self.settings,
                self.state,
                self.warehouse_client,
                metrics=self.metrics,
                logger=self.log,
            ).deliver(run_id, export.local_csv_path)
            return self._finalize(run_id, export=export, delivery=delivery)

    def _finalize(
        self,
        run_id: str,
        *,
        export: Optional[ExportResult],
        delivery: Optional[DeliveryReport],
        reconciled: bool = False,
    ) -> RunOutcome:
        self.state.update_run(run_id, stage=RunStage.FINALIZE)
        ledger = self.state.ledger_counts(run_id)
        batches = self.state.batch_counts(run_id)
        failed_pages = self.state.failed_pages(run_id)
        unknown = int(ledger.get(SkuState.UNKNOWN.value, 0))
        pending = int(ledger.get(SkuState.PENDING.value, 0)) + int(
            ledger.get(SkuState.SENT.value, 0)
        )
        failed_batches = int(batches.get("FAILED_TRANSIENT", 0)) + int(
            batches.get("FAILED_PERMANENT", 0)
        )

        problems: List[str] = []
        exported = self.state.get_run(run_id).counters.get("products_exported")
        accounted = sum(int(value) for value in ledger.values())
        if exported is not None and accounted < int(exported):
            # A product that was exported but never reached the ledger is the
            # worst kind of silent gap: nothing else in the run would report it.
            problems.append(f"{int(exported) - accounted} exported products were never delivered")
        if failed_pages:
            problems.append(f"{len(failed_pages)} PIM pages failed")
        if export is not None and not export.complete:
            problems.append(
                f"exported {export.product_count} of {export.total_reported} products"
            )
        if unknown:
            problems.append(f"{unknown} products have an unknown outcome")
        if failed_batches:
            problems.append(f"{failed_batches} WMS batches failed")
        if pending:
            problems.append(f"{pending} products were never delivered")

        status = RunStatus.PARTIAL if problems else RunStatus.COMPLETED
        counters = {
            "accepted": int(ledger.get(SkuState.ACCEPTED.value, 0)),
            "rejected": int(ledger.get(SkuState.REJECTED.value, 0)),
            "unknown": unknown,
            "failed_validation": int(ledger.get(SkuState.FAILED_VALIDATION.value, 0)),
            "pending": pending,
            "batches_failed": failed_batches,
            "reconciled": reconciled,
        }
        self.state.update_run(
            run_id,
            status=status,
            counters=counters,
            metadata={"problems": "; ".join(problems)} if problems else None,
            finished=True,
        )
        self.metrics.incr("RunsCompleted" if status is RunStatus.COMPLETED else "RunsPartial")
        self.metrics.observe("ProductsUnknownFinal", unknown)
        flushed = self.metrics.flush("run")
        level = self.log.info if status is RunStatus.COMPLETED else self.log.error
        level(
            "run finished",
            extra={
                "run_id": run_id,
                "status": status.value,
                "problems": problems,
                "counters": counters,
            },
        )
        return RunOutcome(
            run_id,
            status,
            export=export,
            delivery=delivery,
            reason="; ".join(problems) or None,
            metrics=flushed,
        )

    # -- helpers -----------------------------------------------------------

    def _local_csv(self, run_id: str) -> str:
        """Return a local copy of the run's export, downloading it if needed."""
        local_path = os.path.join(self.settings.scratch_dir, run_id, "catalogue.csv")
        if os.path.exists(local_path):
            return local_path
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        record = self.state.get_run(run_id)
        key = (record.metadata.get("csv_key") if record else None) or csv_key(run_id)
        return self.store.download_to(key, local_path)

    def _summarise(self, run_id: str) -> Dict[str, Any]:
        record = self.state.get_run(run_id)
        if record is None:
            raise RunNotFound(run_id)
        ledger = self.state.ledger_counts(run_id)
        return {
            **record.as_dict(),
            "pages": {
                "complete": len(self.state.completed_pages(run_id)),
                "failed": self.state.failed_pages(run_id),
            },
            "batches": self.state.batch_counts(run_id),
            "products": ledger,
            "unknown_sample": [
                sku for sku, _ in self.state.entries_in_state(run_id, SkuState.UNKNOWN, limit=10)
            ],
        }


def build_runner(settings: Settings) -> SyncRunner:
    setup_logging(settings.log_level, settings.log_format)
    return SyncRunner(settings)
