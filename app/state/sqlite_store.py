"""SQLite run state store: the local stand-in for DynamoDB.

SQLite is used because it gives real durability and real conditional updates
without any infrastructure, so the resume and duplicate-protection logic can be
tested honestly. The schema and the guarded updates mirror the DynamoDB
implementation one-to-one (see :mod:`app.state.dynamodb_store`).

Concurrency: the delivery stage runs many worker threads. A single connection is
shared behind one lock rather than a connection per thread. The lock is held for
microseconds of local I/O while the workers spend hundreds of milliseconds in
HTTP calls, so it is not the bottleneck, and it removes any chance of SQLite
"database is locked" errors under write contention.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.models import (
    SENDABLE_SKU_STATES,
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    counters    TEXT NOT NULL DEFAULT '{}',
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS pages (
    run_id        TEXT NOT NULL,
    page          INTEGER NOT NULL,
    status        TEXT NOT NULL,
    product_count INTEGER NOT NULL DEFAULT 0,
    checksum      TEXT,
    object_key    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, page)
);

CREATE TABLE IF NOT EXISTS batches (
    run_id     TEXT NOT NULL,
    batch_no   INTEGER NOT NULL,
    status     TEXT NOT NULL,
    sku_count  INTEGER NOT NULL DEFAULT 0,
    first_row  INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, batch_no)
);

CREATE TABLE IF NOT EXISTS ledger (
    run_id       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    state        TEXT NOT NULL,
    batch_no     INTEGER,
    content_hash TEXT,
    reason       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, sku)
);

CREATE INDEX IF NOT EXISTS ledger_state_idx ON ledger (run_id, state);
CREATE INDEX IF NOT EXISTS batches_status_idx ON batches (run_id, status);
CREATE INDEX IF NOT EXISTS pages_status_idx ON pages (run_id, status);
"""

_TERMINAL = tuple(state.value for state in TERMINAL_SKU_STATES)
_SENDABLE = tuple(state.value for state in SENDABLE_SKU_STATES)


class SqliteRunStateStore(RunStateStore):
    def __init__(self, path: str = "./runtime/state/catalogue_sync.db") -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL keeps readers (status queries) from blocking the writers.
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- runs --------------------------------------------------------------

    def create_run(self, run_id: str, *, metadata: Optional[Dict[str, Any]] = None) -> RunRecord:
        now = utcnow()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO runs (run_id, status, stage, created_at, updated_at, counters, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        RunStatus.RUNNING.value,
                        RunStage.CREATED.value,
                        now,
                        now,
                        "{}",
                        json.dumps(metadata or {}),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RunAlreadyExists(run_id) from exc
            self._conn.commit()
        return RunRecord(
            run_id=run_id,
            status=RunStatus.RUNNING,
            stage=RunStage.CREATED,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def list_runs(self, limit: int = 20) -> List[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

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
        now = utcnow()
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            current = self._run_from_row(row)
            merged_counters = {**current.counters, **(counters or {})}
            merged_metadata = {**current.metadata, **(metadata or {})}
            self._conn.execute(
                "UPDATE runs SET status = ?, stage = ?, updated_at = ?, finished_at = ?,"
                " counters = ?, metadata = ? WHERE run_id = ?",
                (
                    (status or current.status).value,
                    (stage or current.stage).value,
                    now,
                    now if finished else current.finished_at,
                    json.dumps(merged_counters),
                    json.dumps(merged_metadata),
                    run_id,
                ),
            )
            self._conn.commit()
        return RunRecord(
            run_id=run_id,
            status=status or current.status,
            stage=stage or current.stage,
            created_at=current.created_at,
            updated_at=now,
            finished_at=now if finished else current.finished_at,
            counters=merged_counters,
            metadata=merged_metadata,
        )

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
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO pages (run_id, page, status, product_count, checksum, object_key,"
                " attempts, detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, page) DO UPDATE SET status = excluded.status,"
                " product_count = excluded.product_count, checksum = excluded.checksum,"
                " object_key = excluded.object_key, attempts = pages.attempts + excluded.attempts,"
                " detail = excluded.detail, updated_at = excluded.updated_at",
                (
                    run_id,
                    page,
                    status.value,
                    product_count,
                    checksum,
                    object_key,
                    attempts,
                    detail,
                    now,
                ),
            )
            self._conn.commit()

    def get_page(self, run_id: str, page: int) -> Optional[PageRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pages WHERE run_id = ? AND page = ?", (run_id, page)
            ).fetchone()
        return self._page_from_row(row) if row else None

    def completed_pages(self, run_id: str) -> Dict[int, PageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pages WHERE run_id = ? AND status = ?",
                (run_id, PageStatus.COMPLETE.value),
            ).fetchall()
        return {int(row["page"]): self._page_from_row(row) for row in rows}

    def failed_pages(self, run_id: str) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT page FROM pages WHERE run_id = ? AND status = ? ORDER BY page",
                (run_id, PageStatus.FAILED.value),
            ).fetchall()
        return [int(row["page"]) for row in rows]

    # -- batches -----------------------------------------------------------

    def register_batch(
        self, run_id: str, batch_no: int, *, sku_count: int, first_row: int
    ) -> BatchRecord:
        now = utcnow()
        with self._lock:
            # DO NOTHING on conflict: a resume must not reset a batch that has
            # already been delivered.
            self._conn.execute(
                "INSERT INTO batches (run_id, batch_no, status, sku_count, first_row, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, batch_no) DO NOTHING",
                (run_id, batch_no, BatchStatus.PENDING.value, sku_count, first_row, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM batches WHERE run_id = ? AND batch_no = ?", (run_id, batch_no)
            ).fetchone()
        return self._batch_from_row(row)

    def claim_batch(self, run_id: str, batch_no: int) -> bool:
        now = utcnow()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE batches SET status = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE run_id = ? AND batch_no = ? AND status IN (?, ?)",
                (
                    BatchStatus.SENDING.value,
                    now,
                    run_id,
                    batch_no,
                    BatchStatus.PENDING.value,
                    BatchStatus.FAILED_TRANSIENT.value,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def finish_batch(
        self,
        run_id: str,
        batch_no: int,
        status: BatchStatus,
        *,
        attempts: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE batches SET status = ?, detail = ?, updated_at = ?,"
                " attempts = MAX(attempts, ?) WHERE run_id = ? AND batch_no = ?",
                (status.value, detail, now, attempts, run_id, batch_no),
            )
            self._conn.commit()

    def get_batch(self, run_id: str, batch_no: int) -> Optional[BatchRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE run_id = ? AND batch_no = ?", (run_id, batch_no)
            ).fetchone()
        return self._batch_from_row(row) if row else None

    def allocate_batch_no(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(batch_no), 0) + 1 AS next_no FROM batches WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["next_no"])

    def batch_counts(self, run_id: str) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM batches WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def reopen_batches(self, run_id: str, batch_numbers: Iterable[int]) -> int:
        numbers = list(batch_numbers)
        if not numbers:
            return 0
        now = utcnow()
        reopened = 0
        with self._lock:
            for chunk in _chunks(numbers, 200):
                placeholders = ",".join("?" * len(chunk))
                cursor = self._conn.execute(
                    f"UPDATE batches SET status = ?, updated_at = ? WHERE run_id = ?"
                    f" AND batch_no IN ({placeholders})",
                    [BatchStatus.PENDING.value, now, run_id, *chunk],
                )
                reopened += cursor.rowcount
            self._conn.commit()
        return reopened

    def reclaim_stale_batches(self, run_id: str) -> int:
        now = utcnow()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE batches SET status = ?, detail = ?, updated_at = ?"
                " WHERE run_id = ? AND status = ?",
                (
                    BatchStatus.UNKNOWN.value,
                    "worker stopped while the batch was in flight",
                    now,
                    run_id,
                    BatchStatus.SENDING.value,
                ),
            )
            self._conn.commit()
            return cursor.rowcount

    # -- ledger ------------------------------------------------------------

    def mark_sent(self, run_id: str, items: Sequence[Tuple[str, str, int]]) -> None:
        if not items:
            return
        now = utcnow()
        rows = [(run_id, sku, SkuState.SENT.value, batch_no, content_hash, now) for sku, content_hash, batch_no in items]
        with self._lock:
            # The guard is the point: an entry that already reached a terminal
            # state is never moved back to SENT, even if a caller asks.
            self._conn.executemany(
                "INSERT INTO ledger (run_id, sku, state, batch_no, content_hash, attempts, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)"
                " ON CONFLICT(run_id, sku) DO UPDATE SET state = excluded.state,"
                " batch_no = excluded.batch_no, content_hash = excluded.content_hash,"
                " attempts = ledger.attempts + 1, updated_at = excluded.updated_at"
                f" WHERE ledger.state IN ({','.join('?' * len(_SENDABLE))})",
                [row + _SENDABLE for row in rows],
            )
            self._conn.commit()

    def record_outcomes(self, run_id: str, outcomes: Sequence[SkuOutcome]) -> None:
        if not outcomes:
            return
        now = utcnow()
        with self._lock:
            # Upsert, because an outcome can be the first thing ever recorded for
            # a SKU (a row that failed local validation was never sent). The
            # conflict guard still prevents a terminal state from moving.
            self._conn.executemany(
                "INSERT INTO ledger (run_id, sku, state, reason, attempts, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?)"
                " ON CONFLICT(run_id, sku) DO UPDATE SET state = excluded.state,"
                " reason = excluded.reason, updated_at = excluded.updated_at"
                f" WHERE ledger.state IN ({','.join('?' * len(_SENDABLE))})",
                [
                    (run_id, outcome.sku, outcome.state.value, outcome.reason, now, *_SENDABLE)
                    for outcome in outcomes
                ],
            )
            self._conn.commit()

    def already_terminal(self, run_id: str, skus: Sequence[str]) -> Set[str]:
        if not skus:
            return set()
        found: Set[str] = set()
        with self._lock:
            for chunk in _chunks(list(skus), 200):
                placeholders = ",".join("?" * len(chunk))
                terminal_placeholders = ",".join("?" * len(_TERMINAL))
                rows = self._conn.execute(
                    f"SELECT sku FROM ledger WHERE run_id = ? AND sku IN ({placeholders})"
                    f" AND state IN ({terminal_placeholders})",
                    [run_id, *chunk, *_TERMINAL],
                ).fetchall()
                found.update(row["sku"] for row in rows)
        return found

    def ledger_counts(self, run_id: str) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM ledger WHERE run_id = ? GROUP BY state",
                (run_id,),
            ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    def entries_in_state(
        self, run_id: str, state: SkuState, limit: Optional[int] = None
    ) -> List[Tuple[str, int]]:
        sql = "SELECT sku, batch_no FROM ledger WHERE run_id = ? AND state = ? ORDER BY sku"
        params: List[Any] = [run_id, state.value]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(row["sku"], int(row["batch_no"] or 0)) for row in rows]

    def reopen_unknown(self, run_id: str) -> int:
        now = utcnow()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE ledger SET state = ?, reason = ?, updated_at = ?"
                " WHERE run_id = ? AND state = ?",
                (
                    SkuState.PENDING.value,
                    "reopened by explicit reconcile",
                    now,
                    run_id,
                    SkuState.UNKNOWN.value,
                ),
            )
            self._conn.commit()
            return cursor.rowcount

    # -- row mapping -------------------------------------------------------

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            status=RunStatus(row["status"]),
            stage=RunStage(row["stage"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
            counters=json.loads(row["counters"] or "{}"),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    @staticmethod
    def _page_from_row(row: sqlite3.Row) -> PageRecord:
        return PageRecord(
            run_id=row["run_id"],
            page=int(row["page"]),
            status=PageStatus(row["status"]),
            product_count=int(row["product_count"]),
            checksum=row["checksum"],
            object_key=row["object_key"],
            attempts=int(row["attempts"]),
            detail=row["detail"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> BatchRecord:
        return BatchRecord(
            run_id=row["run_id"],
            batch_no=int(row["batch_no"]),
            status=BatchStatus(row["status"]),
            sku_count=int(row["sku_count"]),
            first_row=int(row["first_row"]),
            attempts=int(row["attempts"]),
            detail=row["detail"],
            updated_at=row["updated_at"],
        )


def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]
