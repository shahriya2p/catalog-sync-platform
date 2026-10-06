"""Command line interface.

The commands map one to one onto the operational questions:

``run``        start today's synchronisation
``resume``     continue a run that failed or was interrupted
``status``     is a run running, completed, partial or failed?
``reconcile``  deal with products whose outcome is unknown (explicit, guarded)

Exit codes are meaningful so a scheduler can act on them: 0 completed,
2 partial (needs attention, successful work is preserved), 1 failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from app import __version__
from app.config import ConfigError, load_settings
from app.models import RunStatus
from app.observability import setup_logging
from app.services.runner import EXIT_FAILED, SyncRunner, new_run_id
from app.state.store import RunAlreadyExists, RunNotFound


COMMANDS = ("run", "resume", "export", "status", "reconcile")


def _common_options() -> argparse.ArgumentParser:
    """Options accepted either before or after the subcommand.

    ``SUPPRESS`` defaults matter: without them the subparser would overwrite a
    value given before the command with its own default.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        help="DEBUG, INFO, WARNING, ERROR (default INFO)",
    )
    common.add_argument(
        "--log-format", default=argparse.SUPPRESS, choices=["json", "text"], help="default json"
    )
    common.add_argument(
        "--page-size", type=int, default=argparse.SUPPRESS, help="PIM page size (max 500)"
    )
    common.add_argument(
        "--batch-size", type=int, default=argparse.SUPPRESS, help="WMS batch size (max 100)"
    )
    common.add_argument(
        "--pim-rps", type=float, default=argparse.SUPPRESS, help="PIM request rate (max 10)"
    )
    common.add_argument(
        "--wms-rps", type=float, default=argparse.SUPPRESS, help="WMS request rate (max 20)"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Synchronise the product catalogue from the PIM into the WMS.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"catalogue-sync {__version__}")

    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser(
        "run", help="start a new synchronisation run", parents=[common]
    )
    run_cmd.add_argument("--run-id", default=None, help="override the generated run id")

    resume_cmd = commands.add_parser(
        "resume", help="continue an existing run", parents=[common]
    )
    resume_cmd.add_argument("run_id")

    export_cmd = commands.add_parser(
        "export",
        help="run only the export stage (the ECS task in AWS)",
        parents=[common],
    )
    export_cmd.add_argument("run_id")

    status_cmd = commands.add_parser("status", help="show run status", parents=[common])
    status_cmd.add_argument("run_id", nargs="?", default=None)
    status_cmd.add_argument("--limit", type=int, default=10)

    reconcile_cmd = commands.add_parser(
        "reconcile",
        help="resend products whose outcome is unknown",
        parents=[common],
    )
    reconcile_cmd.add_argument("run_id")
    reconcile_cmd.add_argument(
        "--confirm-resend-unknown",
        action="store_true",
        help=(
            "required: these products may already exist in the WMS, so resending "
            "them can create duplicates"
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings().with_overrides(
            log_level=getattr(args, "log_level", None),
            log_format=getattr(args, "log_format", None),
            page_size=getattr(args, "page_size", None),
            batch_size=getattr(args, "batch_size", None),
            pim_requests_per_second=getattr(args, "pim_rps", None),
            wms_requests_per_second=getattr(args, "wms_rps", None),
        )
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return EXIT_FAILED

    setup_logging(settings.log_level, settings.log_format)

    with SyncRunner(settings) as runner:
        try:
            if args.command == "run":
                outcome = runner.run(args.run_id or new_run_id())
                _emit(outcome.as_dict())
                return outcome.exit_code
            if args.command == "resume":
                outcome = runner.resume(args.run_id)
                _emit(outcome.as_dict())
                return outcome.exit_code
            if args.command == "export":
                outcome = runner.export_only(args.run_id)
                _emit(outcome.as_dict())
                return outcome.exit_code
            if args.command == "reconcile":
                outcome = runner.reconcile(
                    args.run_id, confirm=args.confirm_resend_unknown
                )
                _emit(outcome.as_dict())
                if outcome.reason and outcome.status is not RunStatus.COMPLETED:
                    print(outcome.reason, file=sys.stderr)
                return outcome.exit_code
            if args.command == "status":
                _emit(runner.status(args.run_id, limit=args.limit))
                return 0
        except RunAlreadyExists as error:
            print(f"{error}", file=sys.stderr)
            return EXIT_FAILED
        except RunNotFound as error:
            print(f"run not found: {error}", file=sys.stderr)
            return EXIT_FAILED
        except KeyboardInterrupt:  # pragma: no cover - interactive
            print("interrupted; the run can be continued with 'resume'", file=sys.stderr)
            return EXIT_FAILED

    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return EXIT_FAILED


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
