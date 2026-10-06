"""Run state and the SKU ledger.

These are the invariants the duplicate-protection story rests on, so they are
tested directly rather than only through the pipeline.
"""

from __future__ import annotations

import threading

import pytest

from app.models import BatchStatus, PageStatus, RunStage, RunStatus, SkuOutcome, SkuState
from app.state.sqlite_store import SqliteRunStateStore
from app.state.store import RunAlreadyExists, RunNotFound


# --- runs ------------------------------------------------------------------


def test_a_run_id_cannot_be_created_twice(state):
    state.create_run("run-1")
    with pytest.raises(RunAlreadyExists):
        state.create_run("run-1")


def test_run_status_and_counters_are_readable_and_merge(state):
    state.create_run("run-1", metadata={"page_size": 500})
    state.update_run("run-1", stage=RunStage.EXPORT, counters={"pages": 24})
    state.update_run(
        "run-1", status=RunStatus.PARTIAL, counters={"accepted": 10}, finished=True
    )

    record = state.get_run("run-1")
    assert record.status is RunStatus.PARTIAL
    assert record.stage is RunStage.EXPORT
    assert record.counters == {"pages": 24, "accepted": 10}
    assert record.metadata == {"page_size": 500}
    assert record.finished_at is not None


def test_updating_an_unknown_run_is_an_error(state):
    with pytest.raises(RunNotFound):
        state.update_run("nope", status=RunStatus.FAILED)


def test_runs_are_listed_newest_first(state):
    for run_id in ("run-1", "run-2", "run-3"):
        state.create_run(run_id)
    assert [record.run_id for record in state.list_runs()][0] in {"run-3", "run-2", "run-1"}
    assert len(state.list_runs(limit=2)) == 2


# --- pages -----------------------------------------------------------------


def test_page_checkpoints_separate_complete_from_failed(state):
    state.create_run("run-1")
    state.record_page("run-1", 1, PageStatus.COMPLETE, product_count=500, checksum="abc")
    state.record_page("run-1", 2, PageStatus.FAILED, detail="503 after 6 attempts")

    assert set(state.completed_pages("run-1")) == {1}
    assert state.completed_pages("run-1")[1].product_count == 500
    assert state.failed_pages("run-1") == [2]


def test_a_failed_page_can_later_be_recorded_as_complete(state):
    """A resume must be able to turn a failure into a success."""
    state.create_run("run-1")
    state.record_page("run-1", 7, PageStatus.FAILED, detail="timeout")
    state.record_page("run-1", 7, PageStatus.COMPLETE, product_count=500)

    assert state.failed_pages("run-1") == []
    assert state.get_page("run-1", 7).attempts == 2


# --- batches ---------------------------------------------------------------


def test_a_batch_can_only_be_claimed_once(state):
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)

    assert state.claim_batch("run-1", 1) is True
    assert state.claim_batch("run-1", 1) is False, "a second worker must not take it"


def test_registering_a_batch_again_does_not_reset_it(state):
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    state.claim_batch("run-1", 1)
    state.finish_batch("run-1", 1, BatchStatus.COMPLETED)

    state.register_batch("run-1", 1, sku_count=100, first_row=1)

    assert state.get_batch("run-1", 1).status is BatchStatus.COMPLETED
    assert state.claim_batch("run-1", 1) is False


def test_a_transiently_failed_batch_is_claimable_again(state):
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    state.claim_batch("run-1", 1)
    state.finish_batch("run-1", 1, BatchStatus.FAILED_TRANSIENT, detail="503")

    assert state.claim_batch("run-1", 1) is True


def test_an_unknown_batch_is_not_claimable_again(state):
    """Ambiguous batches are never picked up automatically."""
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    state.claim_batch("run-1", 1)
    state.finish_batch("run-1", 1, BatchStatus.UNKNOWN, detail="read timeout")

    assert state.claim_batch("run-1", 1) is False


def test_batches_left_in_flight_by_a_crash_become_unknown(state):
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    state.claim_batch("run-1", 1)  # process dies here

    assert state.reclaim_stale_batches("run-1") == 1
    assert state.get_batch("run-1", 1).status is BatchStatus.UNKNOWN


def test_concurrent_claims_produce_exactly_one_winner(state):
    state.create_run("run-1")
    state.register_batch("run-1", 1, sku_count=100, first_row=1)
    results = []

    def claim():
        results.append(state.claim_batch("run-1", 1))

    threads = [threading.Thread(target=claim) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1


# --- ledger ----------------------------------------------------------------


def test_outcomes_are_recorded_per_sku(state):
    state.create_run("run-1")
    state.mark_sent("run-1", [("A1", "h1", 1), ("A2", "h2", 1)])
    state.record_outcomes(
        "run-1",
        [SkuOutcome("A1", SkuState.ACCEPTED), SkuOutcome("A2", SkuState.REJECTED, "bad")],
    )

    assert state.ledger_counts("run-1") == {"ACCEPTED": 1, "REJECTED": 1}


def test_a_terminal_state_is_never_moved_backwards(state):
    """The core duplicate guard: an accepted product cannot become sendable."""
    state.create_run("run-1")
    state.mark_sent("run-1", [("A1", "h1", 1)])
    state.record_outcomes("run-1", [SkuOutcome("A1", SkuState.ACCEPTED)])

    state.mark_sent("run-1", [("A1", "h1", 2)])  # a redelivered message tries again

    assert state.ledger_counts("run-1") == {"ACCEPTED": 1}
    assert state.already_terminal("run-1", ["A1"]) == {"A1"}


def test_already_terminal_reports_only_undeliverable_skus(state):
    state.create_run("run-1")
    state.mark_sent("run-1", [("A1", "h", 1), ("A2", "h", 1), ("A3", "h", 1)])
    state.record_outcomes(
        "run-1",
        [
            SkuOutcome("A1", SkuState.ACCEPTED),
            SkuOutcome("A2", SkuState.REJECTED, "invalid"),
            SkuOutcome("A3", SkuState.UNKNOWN, "timeout"),
        ],
    )
    state.mark_sent("run-1", [("A4", "h", 1)])  # still in flight

    assert state.already_terminal("run-1", ["A1", "A2", "A3", "A4", "A5"]) == {
        "A1",
        "A2",
        "A3",
    }


def test_an_outcome_can_be_the_first_record_for_a_sku(state):
    """Rows that fail local validation are never sent but must be recorded."""
    state.create_run("run-1")
    state.record_outcomes(
        "run-1", [SkuOutcome("A9", SkuState.FAILED_VALIDATION, "sku is empty")]
    )

    assert state.ledger_counts("run-1") == {"FAILED_VALIDATION": 1}


def test_unknown_entries_are_listed_and_only_reopened_explicitly(state):
    state.create_run("run-1")
    state.mark_sent("run-1", [("A1", "h", 3), ("A2", "h", 3)])
    state.record_outcomes(
        "run-1",
        [SkuOutcome("A1", SkuState.UNKNOWN, "timeout"), SkuOutcome("A2", SkuState.ACCEPTED)],
    )

    assert state.entries_in_state("run-1", SkuState.UNKNOWN) == [("A1", 3)]
    assert state.already_terminal("run-1", ["A1"]) == {"A1"}

    assert state.reopen_unknown("run-1") == 1
    assert state.already_terminal("run-1", ["A1"]) == set()
    assert state.ledger_counts("run-1") == {"PENDING": 1, "ACCEPTED": 1}


def test_transient_failures_return_skus_to_pending(state):
    state.create_run("run-1")
    state.mark_sent("run-1", [("A1", "h", 1)])
    state.record_outcomes("run-1", [SkuOutcome("A1", SkuState.PENDING, "503")])

    assert state.already_terminal("run-1", ["A1"]) == set()
    assert state.ledger_counts("run-1") == {"PENDING": 1}


def test_the_store_survives_being_reopened(settings, state):
    """State must be durable: a crashed run has to be resumable."""
    state.create_run("run-1")
    state.record_page("run-1", 1, PageStatus.COMPLETE, product_count=500)
    state.mark_sent("run-1", [("A1", "h", 1)])
    state.record_outcomes("run-1", [SkuOutcome("A1", SkuState.ACCEPTED)])

    reopened = SqliteRunStateStore(settings.sqlite_path)
    try:
        assert reopened.get_run("run-1").run_id == "run-1"
        assert set(reopened.completed_pages("run-1")) == {1}
        assert reopened.already_terminal("run-1", ["A1"]) == {"A1"}
    finally:
        reopened.close()
