"""CLI behaviour, including the exit codes a scheduler acts on."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.cli import build_parser, main
from app.models import RunStatus
from app.services.runner import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, RunOutcome
from app.state.store import RunAlreadyExists, RunNotFound


@pytest.fixture(autouse=True)
def local_env(monkeypatch, tmp_path):
    """Point every command at throwaway local storage and state."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "s3"))
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")


class FakeRunner:
    """Stands in for SyncRunner so the CLI layer is tested on its own."""

    calls: List[Dict[str, Any]] = []
    outcome_status = RunStatus.COMPLETED
    raises: Optional[Exception] = None

    def __init__(self, settings, **kwargs):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def _record(self, name: str, **kwargs: Any) -> RunOutcome:
        FakeRunner.calls.append({"command": name, **kwargs})
        if FakeRunner.raises is not None:
            raise FakeRunner.raises
        return RunOutcome("run-x", FakeRunner.outcome_status, reason="because")

    def run(self, run_id=None):
        return self._record("run", run_id=run_id)

    def resume(self, run_id):
        return self._record("resume", run_id=run_id)

    def export_only(self, run_id):
        return self._record("export", run_id=run_id)

    def reconcile(self, run_id, *, confirm=False):
        return self._record("reconcile", run_id=run_id, confirm=confirm)

    def status(self, run_id=None, *, limit=10):
        FakeRunner.calls.append({"command": "status", "run_id": run_id, "limit": limit})
        return {"runs": []}


@pytest.fixture
def fake_runner(monkeypatch):
    FakeRunner.calls = []
    FakeRunner.outcome_status = RunStatus.COMPLETED
    FakeRunner.raises = None
    monkeypatch.setattr("app.cli.SyncRunner", FakeRunner)
    return FakeRunner


# --- parsing ---------------------------------------------------------------


def test_global_options_work_before_and_after_the_subcommand():
    before = build_parser().parse_args(["--page-size", "100", "run"])
    after = build_parser().parse_args(["run", "--page-size", "100"])

    assert before.page_size == after.page_size == 100
    assert before.command == after.command == "run"


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- exit codes ------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (RunStatus.COMPLETED, EXIT_OK),
        (RunStatus.PARTIAL, EXIT_PARTIAL),
        (RunStatus.FAILED, EXIT_FAILED),
    ],
)
def test_exit_codes_reflect_the_run_outcome(fake_runner, status, expected, capsys):
    fake_runner.outcome_status = status
    assert main(["run", "--run-id", "run-x"]) == expected
    assert json.loads(capsys.readouterr().out)["status"] == status.value


def test_invalid_configuration_fails_before_doing_anything(capsys):
    assert main(["run", "--batch-size", "500"]) == EXIT_FAILED
    assert "configuration error" in capsys.readouterr().err


def test_an_existing_run_id_is_reported_not_restarted(fake_runner, capsys):
    fake_runner.raises = RunAlreadyExists("run-x already exists; use 'resume run-x'")
    assert main(["run", "--run-id", "run-x"]) == EXIT_FAILED
    assert "resume" in capsys.readouterr().err


def test_an_unknown_run_is_reported(fake_runner, capsys):
    fake_runner.raises = RunNotFound("nope")
    assert main(["resume", "nope"]) == EXIT_FAILED
    assert "run not found" in capsys.readouterr().err


# --- command wiring --------------------------------------------------------


def test_commands_reach_the_runner(fake_runner, capsys):
    main(["run", "--run-id", "r1"])
    main(["resume", "r1"])
    main(["export", "r1"])
    main(["status", "r1"])
    capsys.readouterr()

    assert [call["command"] for call in fake_runner.calls] == [
        "run",
        "resume",
        "export",
        "status",
    ]


def test_reconcile_requires_explicit_confirmation(fake_runner, capsys):
    main(["reconcile", "r1"])
    main(["reconcile", "r1", "--confirm-resend-unknown"])
    capsys.readouterr()

    assert [call["confirm"] for call in fake_runner.calls] == [False, True]


# --- against the real stack -------------------------------------------------


def test_status_with_no_runs_is_not_an_error(capsys):
    assert main(["status"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"runs": []}


def test_module_entry_point_defaults_to_a_full_run(monkeypatch):
    from app import main as main_module

    captured: List[List[str]] = []
    monkeypatch.setattr(main_module, "cli_main", lambda argv: captured.append(argv) or 0)

    main_module.run([])
    main_module.run(["--log-format", "text"])
    main_module.run(["status"])

    assert captured == [
        ["run"],
        ["--log-format", "text", "run"],  # a bare global option is not a usage error
        ["status"],
    ]


def test_the_result_document_is_the_only_thing_on_stdout(fake_runner, capsys):
    """So that `... | jq` keeps working when a retry warning is logged."""
    import logging

    from app.observability import setup_logging

    setup_logging("WARNING", "text")
    logging.getLogger("app.test").warning("retrying after transient error")
    main(["status", "r1"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"runs": []}
    assert "retrying" in captured.err
