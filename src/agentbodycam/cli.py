"""Command-line interface for AgentBodycam.

Only foundation commands exist at this stage. Recording, forensics and replay
are not implemented yet and are intentionally absent from the CLI.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from agentbodycam import __version__
from agentbodycam.core.doctor import collect_environment, format_report

PROG = "agentbodycam"
TAGLINE = "Your AI coding agent now has a bodycam."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=f"{TAGLINE}\n\nEarly development: recording is not implemented yet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "doctor",
        help="Show basic environment information (read-only).",
        description=(
            "Inspect the local environment: Python version, operating system, "
            "current directory and Git availability. Makes no changes."
        ),
    )
    return parser


def _run_doctor(out: TextIO) -> int:
    report = collect_environment()
    out.write(format_report(report))
    out.write("\n")
    return 0


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Run the CLI and return a process exit code."""
    stream = out if out is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _run_doctor(stream)

    parser.print_help(stream)
    return 0
