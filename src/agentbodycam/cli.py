"""Command-line interface for AgentBodycam.

Only foundation and session-store commands exist at this stage. Recording, forensics and
replay are not implemented yet and are intentionally absent from the CLI.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TextIO

from agentbodycam import __version__
from agentbodycam.core.doctor import collect_environment, format_report
from agentbodycam.core.store import EventStore, StoreError
from agentbodycam.models.events import EventValidationError
from agentbodycam.models.session import InvalidSessionIdError, Session

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

    project = argparse.ArgumentParser(add_help=False)
    project.add_argument(
        "--project",
        type=Path,
        default=None,
        metavar="DIR",
        help="Project root containing .agentbodycam/ (default: current directory).",
    )
    session = subparsers.add_parser(
        "session",
        help="Manage local AgentBodycam sessions (no recording yet).",
        description="Create and inspect local sessions stored under .agentbodycam/.",
    )
    session_commands = session.add_subparsers(
        dest="session_command", metavar="<session-command>", required=True
    )
    session_commands.add_parser(
        "create",
        parents=[project],
        help="Create an empty session. Does not start recording.",
    )
    session_commands.add_parser("list", parents=[project], help="List sessions.")
    show = session_commands.add_parser(
        "show", parents=[project], help="Show session metadata and event count."
    )
    show.add_argument("session_id", metavar="<session-id>")
    return parser


def _run_doctor(out: TextIO) -> int:
    report = collect_environment()
    out.write(format_report(report))
    out.write("\n")
    return 0


def _fmt_time(value: datetime | None) -> str:
    return "-" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def _session_create(store: EventStore, out: TextIO) -> int:
    session = store.create_session()
    out.write(f"Created AgentBodycam session: {session.session_id}\n")
    out.write(f"Storage: {store.session_dir(session.session_id)}\n")
    return 0


def _session_list(store: EventStore, out: TextIO, err: TextIO) -> int:
    session_ids = store.list_session_ids()
    if not session_ids:
        out.write(f"No AgentBodycam sessions found in {store.project_root}\n")
        return 0
    status = 0
    out.write(f"AgentBodycam sessions in {store.sessions_dir}\n\n")
    out.write(f"  {'SESSION ID':<34}{'STATUS':<11}{'STARTED (UTC)':<21}EVENTS\n")
    for session_id in session_ids:
        try:
            session = store.load_session(session_id)
            events = str(store.count_events(session_id))
        except (StoreError, EventValidationError) as exc:
            err.write(f"error: {exc}\n")
            out.write(f"  {session_id:<34}{'unreadable':<11}\n")
            status = 1
            continue
        out.write(
            f"  {session.session_id:<34}{session.status.value:<11}"
            f"{_fmt_time(session.started_at):<21}{events}\n"
        )
    return status


def _format_session(session: Session, storage: Path, events: str) -> str:
    plat = session.platform
    lines = [
        f"AgentBodycam session {session.session_id}",
        "",
        f"  Status        : {session.status.value}",
        f"  Started (UTC) : {_fmt_time(session.started_at)}",
        f"  Ended (UTC)   : {_fmt_time(session.ended_at)}",
        f"  Project root  : {session.project_root}",
        f"  Storage       : {storage}",
        f"  AgentBodycam  : {session.agentbodycam_version}",
        f"  Platform      : {plat.os_name} {plat.os_release} ({plat.machine}), "
        f"{plat.python_implementation} {plat.python_version}",
        f"  Events        : {events}",
    ]
    return "\n".join(lines) + "\n"


def _session_show(store: EventStore, session_id: str, out: TextIO, err: TextIO) -> int:
    session = store.load_session(session_id)
    try:
        events = str(store.count_events(session_id))
    except StoreError as exc:
        err.write(f"error: {exc}\n")
        out.write(_format_session(session, store.session_dir(session_id), "unreadable"))
        return 1
    out.write(_format_session(session, store.session_dir(session_id), events))
    return 0


def _run_session(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    project: Path = args.project if args.project is not None else Path.cwd()
    store = EventStore(project)
    try:
        if args.session_command == "create":
            return _session_create(store, out)
        if args.session_command == "list":
            return _session_list(store, out, err)
        return _session_show(store, args.session_id, out, err)
    except InvalidSessionIdError as exc:
        err.write(f"error: {exc}\n")
        return 2
    except (StoreError, EventValidationError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run the CLI and return a process exit code."""
    stream = out if out is not None else sys.stdout
    err_stream = err if err is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _run_doctor(stream)
    if args.command == "session":
        return _run_session(args, stream, err_stream)

    parser.print_help(stream)
    return 0
