"""Run state interface.

Four things are recorded, and each exists to answer one operational question:

``runs``
    Is this run running, completed, partial or failed? (operations visibility)

``pages``
    Which PIM pages are already safely in S3? (do not re-fetch on resume)

``batches``
    Which WMS batches still need sending? (unit of retry; the local stand-in for
    the SQS queue)

``ledger``
    What happened to each individual SKU? (duplicate protection, and the record
    that distinguishes "rejected" from "never sent" from "we do not know")

Every state transition is expressed as a conditional update guarded by the
current state, so a repeated or concurrent attempt cannot move a SKU backwards
out of a terminal state. The SQLite implementation uses
``UPDATE ... WHERE state IN (...)`` and the DynamoDB implementation uses a
``ConditionExpression``; the semantics are intentionally the same.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.models import BatchStatus, PageStatus, RunStage, RunStatus, SkuOutcome, SkuState


class RunAlreadyExists(Exception):
    """A run with this id already exists (guards against double scheduling)."""


class RunNotFound(Exception):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class RunRecord:
    run_id: str
    status: RunStatus = RunStatus.RUNNING
    stage: RunStage = RunStage.CREATED
    created_at: str = ""
    updated_at: str = ""
    finished_at: Optional[str] = None
    counters: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "stage": self.stage.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "counters": self.counters,
            "metadata": self.metadata,
        }


@dataclass
class PageRecord:
    run_id: str
    page: int
    status: PageStatus
    product_count: int = 0
    checksum: Optional[str] = None
    object_key: Optional[str] = None
    attempts: int = 0
    detail: Optional[str] = None
    updated_at: str = ""


@dataclass
class BatchRecord:
    run_id: str
    batch_no: int
    status: BatchStatus = BatchStatus.PENDING
    sku_count: int = 0
    first_row: int = 0
    attempts: int = 0
    detail: Optional[str] = None
    updated_at: str = ""


class RunStateStore(abc.ABC):
    # -- runs --------------------------------------------------------------

    @abc.abstractmethod
    def create_run(self, run_id: str, *, metadata: Optional[Dict[str, Any]] = None) -> RunRecord:
        """Create a run, raising :class:`RunAlreadyExists` if it is present."""

    @abc.abstractmethod
    def get_run(self, run_id: str) -> Optional[RunRecord]:
        ...

    @abc.abstractmethod
    def list_runs(self, limit: int = 20) -> List[RunRecord]:
        ...

    @abc.abstractmethod
    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[RunStatus] = None,
        stage: Optional[RunStage] = None,
        counters: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        finished: bool = False,
    ) -> RunRecord:
        ...

    # -- pages -------------------------------------------------------------

    @abc.abstractmethod
    def record_page(
        self,
        run_id: str,
        page: int,
        status: PageStatus,
        *,
        product_count: int = 0,
        checksum: Optional[str] = None,
        object_key: Optional[str] = None,
        attempts: int = 1,
        detail: Optional[str] = None,
    ) -> None:
        ...

    @abc.abstractmethod
    def get_page(self, run_id: str, page: int) -> Optional[PageRecord]:
        ...

    @abc.abstractmethod
    def completed_pages(self, run_id: str) -> Dict[int, PageRecord]:
        ...

    @abc.abstractmethod
    def failed_pages(self, run_id: str) -> List[int]:
        ...

    # -- batches -----------------------------------------------------------

    @abc.abstractmethod
    def register_batch(
        self, run_id: str, batch_no: int, *, sku_count: int, first_row: int
    ) -> BatchRecord:
        """Insert the batch if absent; never overwrite an existing status."""

    @abc.abstractmethod
    def claim_batch(self, run_id: str, batch_no: int) -> bool:
        """Conditionally move PENDING/FAILED_TRANSIENT to SENDING.

        Returns False when another worker already owns the batch or it reached a
        terminal state, which is what stops a redelivered queue message from
        sending the same products twice.
        """

    @abc.abstractmethod
    def finish_batch(
        self,
        run_id: str,
        batch_no: int,
        status: BatchStatus,
        *,
        attempts: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        ...

    @abc.abstractmethod
    def get_batch(self, run_id: str, batch_no: int) -> Optional[BatchRecord]:
        ...

    @abc.abstractmethod
    def allocate_batch_no(self, run_id: str) -> int:
        """A batch number not yet used by this run.

        Used to deliver products that a previous attempt's batch layout did not
        cover, so they never inherit a number that is already terminal.
        """

    @abc.abstractmethod
    def batch_counts(self, run_id: str) -> Dict[str, int]:
        ...

    @abc.abstractmethod
    def reopen_batches(self, run_id: str, batch_numbers: Iterable[int]) -> int:
        """Set the given batches back to PENDING (used by reconcile)."""

    @abc.abstractmethod
    def reclaim_stale_batches(self, run_id: str) -> int:
        """Recover batches left in SENDING by a crashed worker.

        They are moved to UNKNOWN, not PENDING: the request may have reached the
        WMS, so a blind resend could duplicate.
        """

    # -- ledger ------------------------------------------------------------

    @abc.abstractmethod
    def mark_sent(self, run_id: str, items: Sequence[Tuple[str, str, int]]) -> None:
        """Record ``(sku, content_hash, batch_no)`` as SENT before the call.

        Writing before the request is what makes a crash mid-request detectable:
        the SKU is left in SENT with no outcome, which recovery turns into
        UNKNOWN rather than silently retrying it.
        """

    @abc.abstractmethod
    def record_outcomes(self, run_id: str, outcomes: Sequence[SkuOutcome]) -> None:
        """Apply authoritative per-SKU outcomes. Terminal states are not moved."""

    @abc.abstractmethod
    def already_terminal(self, run_id: str, skus: Sequence[str]) -> Set[str]:
        """Subset of ``skus`` that must not be sent again."""

    @abc.abstractmethod
    def ledger_counts(self, run_id: str) -> Dict[str, int]:
        ...

    @abc.abstractmethod
    def entries_in_state(
        self, run_id: str, state: SkuState, limit: Optional[int] = None
    ) -> List[Tuple[str, int]]:
        """``(sku, batch_no)`` pairs in the given state."""

    @abc.abstractmethod
    def reopen_unknown(self, run_id: str) -> int:
        """Move UNKNOWN entries back to PENDING. Explicit operator action only."""

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:  # pragma: no cover - optional for implementations
        return None

    def __enter__(self) -> "RunStateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def build_state_store(settings: Any) -> RunStateStore:
    """Return the configured state store (``sqlite`` or ``dynamodb``)."""
    if settings.state_backend == "dynamodb":
        from app.state.dynamodb_store import DynamoDbRunStateStore

        return DynamoDbRunStateStore(
            runs_table=settings.runs_table,
            pages_table=settings.pages_table,
            batches_table=settings.batches_table,
            ledger_table=settings.ledger_table,
            exceptions_table=settings.exceptions_table,
            region_name=settings.aws_region,
        )
    from app.state.sqlite_store import SqliteRunStateStore

    return SqliteRunStateStore(settings.sqlite_path)
