"""DynamoDB run state store (the production backend).

Key design points, which differ from the SQLite version for reasons that only
matter at scale:

* **Ledger keys are single hash keys** (``pk = "<run_id>#<sku>"``). A million
  ledger writes in one run would otherwise all land on the ``run_id``
  partition, which is capped at 1,000 WCU/s and would throttle the delivery
  stage. Hashing the SKU into the key spreads them. The access pattern is
  purely point read/write, so no query capability is lost: SKU membership is
  checked with ``BatchGetItem``, 100 keys per call, which is exactly one WMS
  batch.
* **Aggregates are counters, not queries.** Counting a million ledger items to
  report run progress would be absurd, so ``record_outcomes`` applies atomic
  ``ADD`` updates to the run item (at most a handful of updates per batch) and
  ``ledger_counts`` reads them back.
* **Exceptions get their own small table.** Rejected, unknown and
  failed-validation SKUs are written to ``exceptions_table`` keyed
  ``(run_id, sku)``. These are expected to be rare, so the partition stays
  small and operations can list "what went wrong in this run" with one query —
  without a global secondary index over the full ledger.

Conditional expressions carry the same guarantees as the SQLite
``UPDATE ... WHERE state IN (...)`` statements: a terminal SKU state can never
be moved backwards, and a batch can only be claimed from a retryable state.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.models import (
    TERMINAL_SKU_STATES,
    BatchStatus,
    PageStatus,
    RunStage,
    RunStatus,
    SkuOutcome,
    SkuState,
)
from app.state.store import (
    BatchRecord,
    PageRecord,
    RunAlreadyExists,
    RunNotFound,
    RunRecord,
    RunStateStore,
    utcnow,
)

_TERMINAL = [state.value for state in TERMINAL_SKU_STATES]
_EXCEPTION_STATES = {
    SkuState.REJECTED.value,
    SkuState.UNKNOWN.value,
    SkuState.FAILED_VALIDATION.value,
}


class DynamoDbRunStateStore(RunStateStore):
    def __init__(
        self,
        *,
        runs_table: str,
        pages_table: str,
        batches_table: str,
        ledger_table: str,
        exceptions_table: str,
        region_name: Optional[str] = None,
        resource: Any = None,
    ) -> None:
        if resource is None:
            import boto3  # lazy: local runs never import boto3

            resource = boto3.resource("dynamodb", region_name=region_name)
        self._runs = resource.Table(runs_table)
        self._pages = resource.Table(pages_table)
        self._batches = resource.Table(batches_table)
        self._ledger = resource.Table(ledger_table)
        self._exceptions = resource.Table(exceptions_table)

    # -- runs --------------------------------------------------------------

    def create_run(self, run_id: str, *, metadata: Optional[Dict[str, Any]] = None) -> RunRecord:
        now = utcnow()
        item = {
            "run_id": run_id,
            "status": RunStatus.RUNNING.value,
            "stage": RunStage.CREATED.value,
            "created_at": now,
            "updated_at": now,
            "counters": {},
            "metadata": metadata or {},
        }
        try:
            self._runs.put_item(
                Item=item, ConditionExpression="attribute_not_exists(run_id)"
            )
        except Exception as error:
            if _is_conditional_failure(error):
                raise RunAlreadyExists(run_id) from error
            raise
        return RunRecord(
            run_id=run_id,
            status=RunStatus.RUNNING,
            stage=RunStage.CREATED,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        item = self._runs.get_item(Key={"run_id": run_id}).get("Item")
        return _run_from_item(item) if item else None

    def list_runs(self, limit: int = 20) -> List[RunRecord]:
        # Runs are few (one per day), so a bounded scan is cheaper and simpler
        # than maintaining a GSI purely for an operator listing.
        items = self._runs.scan(Limit=max(limit * 2, limit)).get("Items", [])
        runs = sorted(
            (_run_from_item(item) for item in items),
            key=lambda run: run.created_at,
            reverse=True,
        )
        return runs[:limit]

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
        names = {"#updated_at": "updated_at"}
        values: Dict[str, Any] = {":updated_at": utcnow()}
        sets = ["#updated_at = :updated_at"]
        if status is not None:
            names["#status"] = "status"
            values[":status"] = status.value
            sets.append("#status = :status")
        if stage is not None:
            names["#stage"] = "stage"
            values[":stage"] = stage.value
            sets.append("#stage = :stage")
        if finished:
            names["#finished_at"] = "finished_at"
            values[":finished_at"] = values[":updated_at"]
            sets.append("#finished_at = :finished_at")
        for index, (key, value) in enumerate(sorted((counters or {}).items())):
            names[f"#c{index}"] = key
            values[f":c{index}"] = _number_safe(value)
            sets.append(f"counters.#c{index} = :c{index}")
        for index, (key, value) in enumerate(sorted((metadata or {}).items())):
            names[f"#m{index}"] = key
            values[f":m{index}"] = _number_safe(value)
            sets.append(f"metadata.#m{index} = :m{index}")
        try:
            response = self._runs.update_item(
                Key={"run_id": run_id},
                UpdateExpression="SET " + ", ".join(sets),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(run_id)",
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if _is_conditional_failure(error):
                raise RunNotFound(run_id) from error
            raise
        return _run_from_item(response["Attributes"])

    # -- pages -------------------------------------------------------------

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
        self._pages.update_item(
            Key={"run_id": run_id, "page": page},
            UpdateExpression=(
                "SET #status = :status, product_count = :count, checksum = :checksum,"
                " object_key = :key, detail = :detail, updated_at = :now"
                " ADD attempts :attempts"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status.value,
                ":count": product_count,
                ":checksum": checksum,
                ":key": object_key,
                ":detail": detail,
                ":now": utcnow(),
                ":attempts": attempts,
            },
        )

    def get_page(self, run_id: str, page: int) -> Optional[PageRecord]:
        item = self._pages.get_item(Key={"run_id": run_id, "page": page}).get("Item")
        return _page_from_item(item) if item else None

    def completed_pages(self, run_id: str) -> Dict[int, PageRecord]:
        return {
            record.page: record
            for record in self._query_pages(run_id)
            if record.status is PageStatus.COMPLETE
        }

    def failed_pages(self, run_id: str) -> List[int]:
        return sorted(
            record.page
            for record in self._query_pages(run_id)
            if record.status is PageStatus.FAILED
        )

    def _query_pages(self, run_id: str) -> List[PageRecord]:
        from boto3.dynamodb.conditions import Key

        records: List[PageRecord] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("run_id").eq(run_id)}
        while True:
            response = self._pages.query(**kwargs)
            records.extend(_page_from_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return records
            kwargs["ExclusiveStartKey"] = start_key

    # -- batches -----------------------------------------------------------

    def register_batch(
        self, run_id: str, batch_no: int, *, sku_count: int, first_row: int
    ) -> BatchRecord:
        now = utcnow()
        try:
            self._batches.put_item(
                Item={
                    "run_id": run_id,
                    "batch_no": batch_no,
                    "status": BatchStatus.PENDING.value,
                    "sku_count": sku_count,
                    "first_row": first_row,
                    "attempts": 0,
                    "updated_at": now,
                },
                ConditionExpression="attribute_not_exists(batch_no)",
            )
        except Exception as error:
            if not _is_conditional_failure(error):
                raise
        existing = self.get_batch(run_id, batch_no)
        return existing or BatchRecord(
            run_id=run_id,
            batch_no=batch_no,
            sku_count=sku_count,
            first_row=first_row,
            updated_at=now,
        )

    def claim_batch(self, run_id: str, batch_no: int) -> bool:
        try:
            self._batches.update_item(
                Key={"run_id": run_id, "batch_no": batch_no},
                UpdateExpression="SET #status = :sending, updated_at = :now ADD attempts :one",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":sending": BatchStatus.SENDING.value,
                    ":now": utcnow(),
                    ":one": 1,
                    ":pending": BatchStatus.PENDING.value,
                    ":transient": BatchStatus.FAILED_TRANSIENT.value,
                },
                ConditionExpression="#status IN (:pending, :transient)",
            )
            return True
        except Exception as error:
            if _is_conditional_failure(error):
                return False
            raise

    def finish_batch(
        self,
        run_id: str,
        batch_no: int,
        status: BatchStatus,
        *,
        attempts: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        self._batches.update_item(
            Key={"run_id": run_id, "batch_no": batch_no},
            UpdateExpression="SET #status = :status, detail = :detail, updated_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status.value,
                ":detail": detail,
                ":now": utcnow(),
            },
        )

    def get_batch(self, run_id: str, batch_no: int) -> Optional[BatchRecord]:
        item = self._batches.get_item(Key={"run_id": run_id, "batch_no": batch_no}).get("Item")
        return _batch_from_item(item) if item else None

    def allocate_batch_no(self, run_id: str) -> int:
        records = self._query_batches(run_id)
        return max((record.batch_no for record in records), default=0) + 1

    def batch_counts(self, run_id: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for record in self._query_batches(run_id):
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        return counts

    def reopen_batches(self, run_id: str, batch_numbers: Iterable[int]) -> int:
        reopened = 0
        for batch_no in batch_numbers:
            self._batches.update_item(
                Key={"run_id": run_id, "batch_no": batch_no},
                UpdateExpression="SET #status = :pending, updated_at = :now",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": BatchStatus.PENDING.value,
                    ":now": utcnow(),
                },
            )
            reopened += 1
        return reopened

    def reclaim_stale_batches(self, run_id: str) -> int:
        reclaimed = 0
        for record in self._query_batches(run_id):
            if record.status is not BatchStatus.SENDING:
                continue
            self.finish_batch(
                run_id,
                record.batch_no,
                BatchStatus.UNKNOWN,
                detail="worker stopped while the batch was in flight",
            )
            reclaimed += 1
        return reclaimed

    def _query_batches(self, run_id: str) -> List[BatchRecord]:
        from boto3.dynamodb.conditions import Key

        records: List[BatchRecord] = []
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("run_id").eq(run_id)}
        while True:
            response = self._batches.query(**kwargs)
            records.extend(_batch_from_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return records
            kwargs["ExclusiveStartKey"] = start_key

    # -- ledger ------------------------------------------------------------

    @staticmethod
    def _pk(run_id: str, sku: str) -> str:
        return f"{run_id}#{sku}"

    def mark_sent(self, run_id: str, items: Sequence[Tuple[str, str, int]]) -> None:
        for sku, content_hash, batch_no in items:
            try:
                self._ledger.update_item(
                    Key={"pk": self._pk(run_id, sku)},
                    UpdateExpression=(
                        "SET #state = :sent, run_id = :run_id, sku = :sku,"
                        " batch_no = :batch_no, content_hash = :hash, updated_at = :now"
                        " ADD attempts :one"
                    ),
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":sent": SkuState.SENT.value,
                        ":run_id": run_id,
                        ":sku": sku,
                        ":batch_no": batch_no,
                        ":hash": content_hash,
                        ":now": utcnow(),
                        ":one": 1,
                        ":pending": SkuState.PENDING.value,
                        ":sent_state": SkuState.SENT.value,
                    },
                    ConditionExpression=(
                        "attribute_not_exists(#state) OR #state IN (:pending, :sent_state)"
                    ),
                )
            except Exception as error:
                if _is_conditional_failure(error):
                    # Already terminal: never move it back to SENT.
                    continue
                raise

    def record_outcomes(self, run_id: str, outcomes: Sequence[SkuOutcome]) -> None:
        applied: Dict[str, int] = {}
        for outcome in outcomes:
            try:
                self._ledger.update_item(
                    Key={"pk": self._pk(run_id, outcome.sku)},
                    UpdateExpression=(
                        "SET #state = :state, reason = :reason, updated_at = :now,"
                        " run_id = :run_id, sku = :sku"
                    ),
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":state": outcome.state.value,
                        ":reason": outcome.reason,
                        ":now": utcnow(),
                        ":run_id": run_id,
                        ":sku": outcome.sku,
                        ":pending": SkuState.PENDING.value,
                        ":sent": SkuState.SENT.value,
                    },
                    # An outcome may be the first record for a SKU (local
                    # validation failure), so an absent item is allowed.
                    ConditionExpression=(
                        "attribute_not_exists(#state) OR #state IN (:pending, :sent)"
                    ),
                )
            except Exception as error:
                if _is_conditional_failure(error):
                    continue  # terminal state wins
                raise
            applied[outcome.state.value] = applied.get(outcome.state.value, 0) + 1
            if outcome.state.value in _EXCEPTION_STATES:
                self._exceptions.put_item(
                    Item={
                        "run_id": run_id,
                        "sku": outcome.sku,
                        "state": outcome.state.value,
                        "reason": outcome.reason,
                        "updated_at": utcnow(),
                    }
                )
        self._add_counters(run_id, applied)

    def _add_counters(self, run_id: str, applied: Dict[str, int]) -> None:
        if not applied:
            return
        names = {}
        values = {}
        adds = []
        for index, (state, count) in enumerate(sorted(applied.items())):
            names[f"#s{index}"] = f"ledger_{state}"
            values[f":v{index}"] = count
            adds.append(f"#s{index} :v{index}")
        self._runs.update_item(
            Key={"run_id": run_id},
            UpdateExpression="ADD " + ", ".join(adds),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def already_terminal(self, run_id: str, skus: Sequence[str]) -> Set[str]:
        if not skus:
            return set()
        found: Set[str] = set()
        unique = list(dict.fromkeys(skus))
        for index in range(0, len(unique), 100):  # BatchGetItem limit
            chunk = unique[index : index + 100]
            keys = [{"pk": self._pk(run_id, sku)} for sku in chunk]
            response = self._ledger.meta.client.batch_get_item(
                RequestItems={
                    self._ledger.name: {
                        "Keys": keys,
                        "ProjectionExpression": "sku, #s",
                        "ExpressionAttributeNames": {"#s": "state"},
                    }
                }
            )
            for item in response.get("Responses", {}).get(self._ledger.name, []):
                if item.get("state") in _TERMINAL:
                    found.add(item["sku"])
        return found

    def ledger_counts(self, run_id: str) -> Dict[str, int]:
        """Aggregates from the run item's counters, not a scan of the ledger."""
        item = self._runs.get_item(Key={"run_id": run_id}).get("Item") or {}
        counts: Dict[str, int] = {}
        for key, value in item.items():
            if key.startswith("ledger_"):
                counts[key[len("ledger_") :]] = int(value)
        return counts

    def entries_in_state(
        self, run_id: str, state: SkuState, limit: Optional[int] = None
    ) -> List[Tuple[str, int]]:
        from boto3.dynamodb.conditions import Attr, Key

        if state in (SkuState.PENDING, SkuState.SENT):
            # Not queryable by design: non-exceptional states are not indexed.
            # Recovery works per batch instead (see DeliveryPipeline).
            return []
        results: List[Tuple[str, int]] = []
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("run_id").eq(run_id),
            "FilterExpression": Attr("state").eq(state.value),
        }
        while True:
            response = self._exceptions.query(**kwargs)
            for item in response.get("Items", []):
                results.append((item["sku"], int(item.get("batch_no", 0) or 0)))
                if limit is not None and len(results) >= limit:
                    return results
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return results
            kwargs["ExclusiveStartKey"] = start_key

    def reopen_unknown(self, run_id: str) -> int:
        reopened = 0
        for sku, _ in self.entries_in_state(run_id, SkuState.UNKNOWN):
            try:
                self._ledger.update_item(
                    Key={"pk": self._pk(run_id, sku)},
                    UpdateExpression="SET #state = :pending, reason = :reason, updated_at = :now",
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":pending": SkuState.PENDING.value,
                        ":reason": "reopened by explicit reconcile",
                        ":now": utcnow(),
                        ":unknown": SkuState.UNKNOWN.value,
                    },
                    ConditionExpression="#state = :unknown",
                )
            except Exception as error:
                if _is_conditional_failure(error):
                    continue
                raise
            self._exceptions.delete_item(Key={"run_id": run_id, "sku": sku})
            reopened += 1
        if reopened:
            self._add_counters(run_id, {SkuState.UNKNOWN.value: -reopened})
        return reopened


# -- helpers ---------------------------------------------------------------


def _is_conditional_failure(error: Exception) -> bool:
    code = (
        getattr(error, "response", {}).get("Error", {}).get("Code")
        if hasattr(error, "response")
        else None
    )
    return code == "ConditionalCheckFailedException" or type(error).__name__ == (
        "ConditionalCheckFailedException"
    )


def _number_safe(value: Any) -> Any:
    """DynamoDB rejects floats; store them as strings of fixed precision."""
    if isinstance(value, float):
        return str(round(value, 3))
    return value


def _run_from_item(item: Dict[str, Any]) -> RunRecord:
    counters = {
        key[len("ledger_") :]: int(value)
        for key, value in item.items()
        if key.startswith("ledger_")
    }
    counters.update({k: _plain(v) for k, v in (item.get("counters") or {}).items()})
    return RunRecord(
        run_id=item["run_id"],
        status=RunStatus(item.get("status", RunStatus.RUNNING.value)),
        stage=RunStage(item.get("stage", RunStage.CREATED.value)),
        created_at=item.get("created_at", ""),
        updated_at=item.get("updated_at", ""),
        finished_at=item.get("finished_at"),
        counters=counters,
        metadata={k: _plain(v) for k, v in (item.get("metadata") or {}).items()},
    )


def _page_from_item(item: Dict[str, Any]) -> PageRecord:
    return PageRecord(
        run_id=item["run_id"],
        page=int(item["page"]),
        status=PageStatus(item["status"]),
        product_count=int(item.get("product_count", 0) or 0),
        checksum=item.get("checksum"),
        object_key=item.get("object_key"),
        attempts=int(item.get("attempts", 0) or 0),
        detail=item.get("detail"),
        updated_at=item.get("updated_at", ""),
    )


def _batch_from_item(item: Dict[str, Any]) -> BatchRecord:
    return BatchRecord(
        run_id=item["run_id"],
        batch_no=int(item["batch_no"]),
        status=BatchStatus(item["status"]),
        sku_count=int(item.get("sku_count", 0) or 0),
        first_row=int(item.get("first_row", 0) or 0),
        attempts=int(item.get("attempts", 0) or 0),
        detail=item.get("detail"),
        updated_at=item.get("updated_at", ""),
    )


def _plain(value: Any) -> Any:
    """Convert DynamoDB Decimals into ints/floats for JSON output."""
    if hasattr(value, "quantize"):
        as_int = int(value)
        return as_int if as_int == value else float(value)
    return value
