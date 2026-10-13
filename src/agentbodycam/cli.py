"""Command-line interface for AgentBodycam.

Implemented: environment check, session-store commands (including the read-only
``session active``), the polling recorder (file changes and read-only Git state; one
recorder per project) and explicit wrapped-command capture (``run -- <command>``).
Test/build classification, forensics and replay are not implemented yet and are
intentionally absent from the CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TextIO

from agentbodycam import __version__
from agentbodycam.core.active import (
    ActiveSessionInfo,
    ActiveState,
    RecordingInProgressError,
    get_active_session,
)
from agentbodycam.core.command import run_wrapped_command
from agentbodycam.core.doctor import collect_environment, format_report
from agentbodycam.core.recorder import (
    DEFAULT_INTERVAL_SECONDS,
    GitStatus,
    Recorder,
    StopReason,
    stop_on_signals,
)
from agentbodycam.core.store import EventStore, StoreError
from agentbodycam.models.events import Event, EventSource, EventType, EventValidationError
from agentbodycam.models.session import InvalidSessionIdError, Session

PROG = "agentbodycam"
TAGLINE = "Your AI coding agent now has a bodycam."
MIN_INTERVAL_SECONDS = 0.05
MAX_INTERVAL_SECONDS = 3600.0

_EVENT_LABELS = {
    EventType.FILE_CREATED: "CREATED",
    EventType.FILE_MODIFIED: "MODIFIED",
    EventType.FILE_DELETED: "DELETED",
    EventType.GIT_SNAPSHOT: "GIT",
    EventType.ERROR_OBSERVED: "ERROR",
}
RUN_USAGE = f"{PROG} run [-h] [--project DIR] -- <command> [args...]"
_GIT_NOTES = {
    GitStatus.NOT_A_REPOSITORY: "Git: not a Git repository (Git state not recorded)",
    GitStatus.GIT_NOT_FOUND: "Git: executable not found (Git state not recorded)",
    GitStatus.ERROR: "Git: repository state could not be read (will keep trying)",
}


def _seconds(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum:g} and {maximum:g}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            f"{TAGLINE}\n\nEarly development: file-system and read-only Git state recording, "
            "plus commands explicitly launched with `agentbodycam run -- ...`. Other terminal "
            "commands and agent internals are not captured."
        ),
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
    session_commands.add_parser(
        "active",
        parents=[project],
        help="Show the session being recorded right now, if any (read-only).",
        description=(
            "Report the session a live `agentbodycam record` is writing to. A recording is "
            "active only while its recorder holds the project's OS-level lock; metadata "
            "left behind by a crashed recorder is reported as stale. Makes no changes."
        ),
    )

    record = subparsers.add_parser(
        "record",
        parents=[project],
        help="Record file and Git state changes in the project into a new session.",
        description=(
            "Start a new session and record observed file creations, modifications and "
            "deletions in the project, plus read-only Git state snapshots when the project "
            "is in a Git repository (polling; no command or agent capture). "
            "Stop with Ctrl+C."
        ),
    )
    record.add_argument(
        "--interval",
        type=_seconds(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=f"Polling interval (default: {DEFAULT_INTERVAL_SECONDS}).",
    )
    record.add_argument(
        "--duration",
        type=_seconds(0.0, math.inf),
        default=None,
        metavar="SECONDS",
        help="Stop automatically after this many seconds (default: until Ctrl+C).",
    )

    subparsers.add_parser(
        "run",
        parents=[project],
        usage=RUN_USAGE,
        help="Run a command and record it in the active recording.",
        description=(
            "Run <command> [args...] and record command.started / command.finished in the "
            "project's active recording (start one with `agentbodycam record`). Without an "
            "active recording the command is not run.\n\n"
            "Everything after `--` is passed to the command exactly as given: no shell is "
            "involved, so `&&`, `|`, `>` and `;` are ordinary arguments. To use a shell, run "
            "it explicitly (e.g. `cmd /c ...` or `sh -c ...`). stdin, stdout, stderr and the "
            "environment are inherited and never recorded; arguments are recorded with "
            "secrets redacted.\n\n"
            "Exit status: the command's own exit status (128+N if a POSIX signal N ended it). "
            "AgentBodycam's own failures: 125 (no active recording or other setup error), "
            "126 (cannot execute), 127 (executable not found), 130 (interrupted before the "
            "command started)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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


def _session_active(project: Path, out: TextIO, err: TextIO) -> int:
    info: ActiveSessionInfo = get_active_session(project)
    record = info.record
    if info.state is ActiveState.ACTIVE and record is not None:
        out.write(
            "Active AgentBodycam recording\n"
            f"Session: {record.session_id}\n"
            f"Started: {_fmt_time(record.started_at)} UTC\n"
            f"PID: {record.pid}\n"
        )
        return 0
    if info.state is ActiveState.INVALID:
        err.write(
            "error: a recorder holds this project's active lock, but its metadata cannot be "
            f"trusted: {info.reason}\n"
        )
        return 1
    out.write("No active AgentBodycam recording.\n")
    if info.state is ActiveState.STALE:
        which = f" (session {record.session_id})" if record is not None else ""
        out.write(
            f"Stale active-recording metadata found{which}: {info.reason}.\n"
            "The recorder that wrote it is no longer running; the next "
            "`agentbodycam record` replaces it.\n"
        )
    return 0


def _run_session(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    project: Path = args.project if args.project is not None else Path.cwd()
    store = EventStore(project)
    try:
        if args.session_command == "create":
            return _session_create(store, out)
        if args.session_command == "list":
            return _session_list(store, out, err)
        if args.session_command == "active":
            return _session_active(project, out, err)
        return _session_show(store, args.session_id, out, err)
    except InvalidSessionIdError as exc:
        err.write(f"error: {exc}\n")
        return 2
    except (StoreError, EventValidationError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1


def _display(text: str) -> str:
    """Escape control characters so a hostile filename cannot drive the terminal."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def _write(out: TextIO, text: str) -> None:
    try:
        out.write(text)
    except UnicodeEncodeError:
        out.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    out.flush()


def _print_event(out: TextIO, event: Event) -> None:
    label = _EVENT_LABELS.get(event.type)
    if label is None:
        return
    when = event.timestamp.astimezone().strftime("%H:%M:%S")
    if event.source is EventSource.GIT:
        target = _display(event.summary)
    else:
        target = _display(event.path) if event.path is not None else "(path not recorded)"
        if event.type is EventType.ERROR_OBSERVED:
            target += f" ({event.payload.get('error')})"
    _write(out, f"  {when}  {label:<9} {target}\n")


def _run_record(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    project: Path = args.project if args.project is not None else Path.cwd()
    store = EventStore(project)
    recorder = Recorder(
        store, interval=args.interval, on_event=lambda event: _print_event(out, event)
    )
    try:
        session = recorder.start()
    except RecordingInProgressError as exc:
        owner = exc.record.session_id if exc.record is not None else "unknown (starting up)"
        err.write(f"error: {exc}\nSession: {owner}\n")
        return 1
    except (StoreError, EventValidationError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1
    try:
        _write(
            out,
            f"AgentBodycam recording\nSession: {session.session_id}\n"
            f"Project: {_display(str(store.project_root))}\n\n",
        )
        lease = recorder.lease
        stale = lease.stale_record if lease is not None else None
        if stale is not None:
            _write(
                out,
                f"Note: replaced stale active-recording metadata from session "
                f"{stale.session_id} (its recorder did not stop cleanly).\n\n",
            )
        with stop_on_signals(recorder):
            baseline = recorder.take_baseline()
            _write(out, f"Baseline snapshot: {baseline} file{'' if baseline == 1 else 's'}\n")
            recorder.observe_git()
            note = _GIT_NOTES.get(recorder.git_status)
            if note is not None:
                _write(out, note + "\n")
            watched = "file and Git" if recorder.git_status is GitStatus.REPOSITORY else "file"
            _write(
                out,
                f"Watching for {watched} changes (polling every {args.interval:g}s)...\n"
                "Press Ctrl+C to stop.\n\n",
            )
            session = recorder.run(duration=args.duration)
    except (StoreError, EventValidationError, OSError) as exc:
        err.write(f"error: {exc}\n")
        if recorder.is_active:
            with contextlib.suppress(StoreError, EventValidationError, OSError):
                recorder.stop(StopReason.ERROR)
        return 1
    finally:
        recorder.release_lease()
    stats = recorder.stats
    lines = [
        "",
        "Recording stopped.",
        f"File events recorded: {stats.file_events} "
        f"({stats.created} created, {stats.modified} modified, {stats.deleted} deleted)",
    ]
    if stats.git_snapshots:
        lines.append(f"Git snapshots recorded: {stats.git_snapshots}")
    if stats.errors:
        lines.append(f"Errors observed: {stats.errors}")
    lines.append(f"Session: {session.session_id}")
    _write(out, "\n".join(lines) + "\n")
    return 0


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
    arguments = list(sys.argv[1:] if argv is None else argv)
    # `run [options] -- <command...>`: everything after the first `--` belongs to the
    # command and is never seen by argparse.
    command_argv: list[str] | None = None
    if arguments[:1] == ["run"] and "--" in arguments:
        split = arguments.index("--")
        arguments, command_argv = arguments[:split], arguments[split + 1 :]
    args = parser.parse_args(arguments)

    if args.command == "run":
        if command_argv is None:
            parser.error(f"run needs `--` before the command\nusage: {RUN_USAGE}")
        if not command_argv:
            parser.error(f"run needs a command after `--`\nusage: {RUN_USAGE}")
        project: Path = args.project if args.project is not None else Path.cwd()
        return run_wrapped_command(project, command_argv, err=err_stream)
    if args.command == "doctor":
        return _run_doctor(stream)
    if args.command == "session":
        return _run_session(args, stream, err_stream)
    if args.command == "record":
        return _run_record(args, stream, err_stream)

    parser.print_help(stream)
    return 0
