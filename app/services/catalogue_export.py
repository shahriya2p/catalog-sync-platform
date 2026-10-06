"""Stage 1: export the PIM catalogue to durable storage.

Three properties matter more than speed here.

**Bounded memory.** The original implementation accumulated the whole catalogue
in a list before writing anything; at a million products that is gigabytes of
Python objects and a single failure loses everything. Here each page is written
to the object store as it arrives, and the CSV is then assembled by streaming
those pages one at a time, so peak memory is one page (<=500 products)
regardless of catalogue size.

**Page-level checkpoints.** Every stored page is recorded in the state store. A
resume re-fetches only the pages that are missing or failed, which is what keeps
a late failure from costing a full re-export (and a second full pass over the
PIM's rate limit).

**A failure boundary per page.** A page that exhausts its retries is marked
FAILED and the run continues. The CSV is still built from the pages that
succeeded, the manifest records what is missing, and the run is reported as
PARTIAL. The requirement is explicit that a small number of failing products
must not force successful products to be reprocessed.

The one exception is authentication: a 401 means every subsequent page will fail
too, so it aborts the stage immediately instead of burning the retry budget 2000
times.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.clients.product_api import ProductApiClient
from app.clients.retry import ApiError, PermanentApiError
from app.config import Settings
from app.models import CSV_FIELDS, ExportResult, PageStatus, PimPage
from app.observability import Metrics, Timer, get_logger, log_context
from app.state.store import RunStateStore
from app.storage.object_store import ObjectStore

MANIFEST_SCHEMA_VERSION = 1


def raw_page_key(run_id: str, page: int) -> str:
    return f"raw/{run_id}/pages/{page:06d}.json"


def csv_key(run_id: str) -> str:
    return f"exports/{run_id}/catalogue.csv"


def manifest_key(run_id: str) -> str:
    return f"exports/{run_id}/manifest.json"


class ExportAborted(RuntimeError):
    """The stage cannot usefully continue (for example invalid credentials)."""


@dataclass
class _PageOutcome:
    page: int
    product_count: int
    fetched: bool
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CatalogueExporter:
    def __init__(
        self,
        settings: Settings,
        store: ObjectStore,
        state: RunStateStore,
        client: ProductApiClient,
        *,
        metrics: Optional[Metrics] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.state = state
        self.client = client
        self.metrics = metrics or Metrics(settings.metrics_namespace)
        self.log = logger or get_logger("app.services.catalogue_export")

    # -- public API --------------------------------------------------------

    def export(self, run_id: str) -> ExportResult:
        with log_context(stage="EXPORT"), Timer(self.metrics, "ExportDurationSeconds"):
            return self._export(run_id)

    # -- stage -------------------------------------------------------------

    def _export(self, run_id: str) -> ExportResult:
        page_size = self.settings.page_size

        # Page 1 is special: it carries the catalogue total, which is how the
        # number of pages becomes known. Without it the stage cannot plan.
        first = self._ensure_page(run_id, 1)
        if not first.ok:
            raise ExportAborted(f"could not fetch the first PIM page: {first.error}")

        envelope = self._load_page(run_id, 1)
        total = int(envelope.get("total", 0))
        page_count = max(1, (total + page_size - 1) // page_size) if total else 1
        self.log.info(
            "catalogue export started",
            extra={
                "run_id": run_id,
                "pim_total": total,
                "page_size": page_size,
                "page_count": page_count,
                "concurrency": self.settings.pim_concurrency,
            },
        )

        outcomes: List[_PageOutcome] = [first]
        if page_count > 1:
            outcomes.extend(self._fetch_pages(run_id, range(2, page_count + 1)))

        failed = sorted(outcome.page for outcome in outcomes if not outcome.ok)
        if failed:
            self.metrics.incr("PagesFailed", len(failed))
            self.log.error(
                "some PIM pages could not be fetched; export will be partial",
                extra={"run_id": run_id, "pages_failed": failed[:20], "failed_count": len(failed)},
            )

        local_path, product_count, checksum, size_bytes = self._build_csv(run_id, page_count)
        csv_object_key = csv_key(run_id)
        self.store.put_file(local_path, csv_object_key, content_type="text/csv")

        result = ExportResult(
            run_id=run_id,
            csv_key=csv_object_key,
            manifest_key=manifest_key(run_id),
            local_csv_path=local_path,
            product_count=product_count,
            page_count=page_count,
            pages_failed=failed,
            total_reported=total,
            checksum=checksum,
        )
        self._write_manifest(result, page_size=page_size, size_bytes=size_bytes)
        self.metrics.incr("ProductsExported", product_count)
        self.log.info(
            "catalogue export finished",
            extra={"run_id": run_id, **result.as_dict(), "csv_bytes": size_bytes},
        )
        return result

    # -- pages -------------------------------------------------------------

    def _fetch_pages(self, run_id: str, pages: Any) -> List[_PageOutcome]:
        outcomes: List[_PageOutcome] = []
        aborted: List[str] = []
        with ThreadPoolExecutor(
            max_workers=self.settings.pim_concurrency, thread_name_prefix="pim"
        ) as pool:
            futures = {pool.submit(self._ensure_page, run_id, page): page for page in pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    outcomes.append(future.result())
                except ExportAborted as error:
                    aborted.append(str(error))
                    outcomes.append(_PageOutcome(page, 0, False, str(error)))
                except Exception as error:  # pragma: no cover - defensive
                    self.log.exception("unexpected failure fetching page", extra={"page": page})
                    outcomes.append(_PageOutcome(page, 0, False, repr(error)))
        if aborted:
            raise ExportAborted(aborted[0])
        return outcomes

    def _ensure_page(self, run_id: str, page: int) -> _PageOutcome:
        """Fetch and store one page unless it is already durably stored."""
        existing = self.state.get_page(run_id, page)
        key = raw_page_key(run_id, page)
        if (
            existing is not None
            and existing.status is PageStatus.COMPLETE
            and self.store.exists(key)
        ):
            self.metrics.incr("PagesSkippedAlreadyStored")
            return _PageOutcome(page, existing.product_count, fetched=False)

        try:
            pim_page: PimPage = self.client.fetch_page(page)
        except PermanentApiError as error:
            if error.status_code in (401, 403):
                # Credentials are wrong: every other page will fail the same way.
                self.state.record_page(
                    run_id, page, PageStatus.FAILED, detail=str(error), attempts=1
                )
                raise ExportAborted(f"PIM authentication failed: {error}") from error
            self.state.record_page(run_id, page, PageStatus.FAILED, detail=str(error), attempts=1)
            return _PageOutcome(page, 0, True, str(error))
        except ApiError as error:
            self.state.record_page(run_id, page, PageStatus.FAILED, detail=str(error), attempts=1)
            return _PageOutcome(page, 0, True, str(error))

        payload = {
            "run_id": run_id,
            "page": pim_page.page,
            "page_size": pim_page.page_size,
            "total": pim_page.total,
            "has_next": pim_page.has_next,
            "next_page": pim_page.next_page,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "products": pim_page.products,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(body).hexdigest()
        # Store first, then checkpoint: a crash between the two makes the page
        # look unfetched and it is simply fetched again. The reverse order could
        # mark a page complete that is not in storage.
        self.store.put_bytes(key, body, content_type="application/json")
        self.state.record_page(
            run_id,
            page,
            PageStatus.COMPLETE,
            product_count=len(pim_page.products),
            checksum=checksum,
            object_key=key,
            attempts=1,
        )
        self.metrics.incr("PagesFetched")
        return _PageOutcome(page, len(pim_page.products), fetched=True)

    def _load_page(self, run_id: str, page: int) -> Dict[str, Any]:
        return json.loads(self.store.get_bytes(raw_page_key(run_id, page)).decode("utf-8"))

    # -- CSV ---------------------------------------------------------------

    def _build_csv(self, run_id: str, page_count: int) -> Tuple[str, int, str, int]:
        """Assemble the CSV from stored pages. Returns path, rows, sha256, bytes."""
        local_path = os.path.join(self.settings.scratch_dir, run_id, "catalogue.csv")
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        digest = hashlib.sha256()
        rows = 0
        with open(local_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
            writer.writeheader()
            for page in range(1, page_count + 1):
                try:
                    envelope = self._load_page(run_id, page)
                except KeyError:
                    # Missing page: already recorded as FAILED, nothing to add.
                    continue
                for product in envelope.get("products", []):
                    writer.writerow({field: product.get(field, "") for field in CSV_FIELDS})
                    rows += 1
                handle.flush()
        with open(local_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return local_path, rows, digest.hexdigest(), os.path.getsize(local_path)

    def _write_manifest(self, result: ExportResult, *, page_size: int, size_bytes: int) -> None:
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": result.run_id,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "columns": list(CSV_FIELDS),
            "csv_key": result.csv_key,
            "csv_sha256": result.checksum,
            "csv_bytes": size_bytes,
            "page_size": page_size,
            "page_count": result.page_count,
            "pages_failed": result.pages_failed,
            "product_count": result.product_count,
            "pim_total": result.total_reported,
            "complete": result.complete,
            "retention_days": self.settings.retention_days,
        }
        self.store.put_bytes(
            manifest_key(result.run_id),
            json.dumps(manifest, indent=2).encode("utf-8"),
            content_type="application/json",
        )
