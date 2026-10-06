"""Domain types shared by the clients, services and state stores."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

CSV_FIELDS: Tuple[str, ...] = (
    "id",
    "name",
    "category",
    "price",
    "currency",
    "updated_at",
)
"""Export columns. Unchanged from the original implementation: the CSV is a
published artefact and other teams may already consume it."""


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RunStage(str, Enum):
    CREATED = "CREATED"
    EXPORT = "EXPORT"
    MANIFEST = "MANIFEST"
    DELIVER = "DELIVER"
    RECONCILE = "RECONCILE"
    FINALIZE = "FINALIZE"


class PageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED_TRANSIENT = "FAILED_TRANSIENT"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    UNKNOWN = "UNKNOWN"


class SkuState(str, Enum):
    """Per-product delivery state.

    ``UNKNOWN`` is the important one: the WMS may or may not have stored the
    product. It is terminal for an automatic run and is only reopened by an
    explicit ``reconcile`` (see ARCHITECTURE.md section 8).
    """

    PENDING = "PENDING"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    FAILED_VALIDATION = "FAILED_VALIDATION"


TERMINAL_SKU_STATES = (
    SkuState.ACCEPTED,
    SkuState.REJECTED,
    SkuState.UNKNOWN,
    SkuState.FAILED_VALIDATION,
)
"""States that must never be re-sent automatically.

Defined once here and used by both state stores, so the SQLite and DynamoDB
guards cannot drift apart.
"""

SENDABLE_SKU_STATES = (SkuState.PENDING, SkuState.SENT)
"""The only states an outcome may be written over."""

RETRYABLE_BATCH_STATES = (BatchStatus.PENDING, BatchStatus.FAILED_TRANSIENT)
"""Batch states a resume is allowed to pick up again."""


@dataclass(frozen=True)
class PimPage:
    """One PIM page exactly as received, plus the pagination envelope."""

    page: int
    page_size: int
    products: List[Dict[str, Any]]
    total: int
    has_next: bool
    next_page: Optional[int]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], *, page: int, page_size: int) -> "PimPage":
        products = payload.get("products")
        if not isinstance(products, list):
            raise ValueError(f"PIM page {page} has no 'products' array")
        return cls(
            page=int(payload.get("page", page)),
            page_size=int(payload.get("page_size", page_size)),
            products=products,
            total=int(payload.get("total", 0)),
            has_next=bool(payload.get("has_next", False)),
            next_page=payload.get("next_page"),
        )


@dataclass(frozen=True)
class SkuOutcome:
    sku: str
    state: SkuState
    reason: Optional[str] = None


@dataclass
class BatchResult:
    """Outcome of one WMS request.

    A WMS 200 can mix accepted and rejected records, and may mention neither
    for a SKU we sent. Those three outcomes are tracked separately: only
    ``accepted`` and ``rejected`` are authoritative, anything missing is
    ``unknown`` and must not be assumed delivered.
    """

    status: BatchStatus
    accepted: List[str] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    http_status: Optional[int] = None
    attempts: int = 0
    detail: Optional[str] = None

    def outcomes(self) -> List[SkuOutcome]:
        results = [SkuOutcome(sku, SkuState.ACCEPTED) for sku in self.accepted]
        results += [SkuOutcome(sku, SkuState.REJECTED, reason) for sku, reason in self.rejected]
        results += [
            SkuOutcome(sku, SkuState.UNKNOWN, self.detail or "not reported by WMS")
            for sku in self.unknown
        ]
        return results


@dataclass
class DeliveryReport:
    accepted: int = 0
    rejected: int = 0
    unknown: int = 0
    failed_validation: int = 0
    skipped_already_delivered: int = 0
    batches_sent: int = 0
    batches_skipped: int = 0
    batches_failed: int = 0
    batches_unknown: int = 0
    rejected_samples: List[Tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "unknown": self.unknown,
            "failed_validation": self.failed_validation,
            "skipped_already_delivered": self.skipped_already_delivered,
            "batches_sent": self.batches_sent,
            "batches_skipped": self.batches_skipped,
            "batches_failed": self.batches_failed,
            "batches_unknown": self.batches_unknown,
            "rejected_samples": self.rejected_samples[:10],
        }


@dataclass
class ExportResult:
    run_id: str
    csv_key: str
    manifest_key: str
    local_csv_path: str
    product_count: int
    page_count: int
    pages_failed: List[int] = field(default_factory=list)
    total_reported: int = 0
    checksum: str = ""

    @property
    def complete(self) -> bool:
        """True when every page was fetched and the row count matches the PIM."""
        return not self.pages_failed and self.product_count == self.total_reported

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "csv_key": self.csv_key,
            "manifest_key": self.manifest_key,
            "product_count": self.product_count,
            "page_count": self.page_count,
            "pages_failed": self.pages_failed,
            "total_reported": self.total_reported,
            "checksum": self.checksum,
            "complete": self.complete,
        }


def content_hash(payload: Dict[str, Any]) -> str:
    """Stable hash of a WMS payload.

    Stored alongside each ledger entry so a future cross-run idempotency check
    can tell "already delivered, unchanged" from "changed, must be re-sent"
    without diffing the whole catalogue.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
