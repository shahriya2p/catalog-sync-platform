"""Entry point.

``python -m app.main`` with no arguments performs a full synchronisation, which
keeps the behaviour the original script had. Everything else (resume, status,
reconcile) is available through the subcommands in :mod:`app.cli`.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from app.cli import COMMANDS, main as cli_main


def run(argv: Optional[List[str]] = None) -> int:
    """Dispatch to the CLI, defaulting to a full synchronisation.

    ``python -m app.main`` and ``python -m app.main --log-format text`` both
    mean "do today's run": a bare global option should not turn into a usage
    error just because no subcommand was typed.
    """
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not any(argument in COMMANDS for argument in arguments):
        arguments.append("run")
    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(run())
