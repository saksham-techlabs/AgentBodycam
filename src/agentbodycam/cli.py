"""Command-line interface for AgentBodycam.

Implemented: environment check, session-store commands (including the read-only
``session active``), the polling recorder (file changes and read-only Git state; one
recorder per project), explicit wrapped-command capture (``run -- <command>``) and
deterministic check results for wrapped commands (``run --check KIND -- <command>``),
read-only deterministic forensic analysis of a recorded session (``analyze``), the local,
authenticated, read-only Replay API (``replay-api``) and the local Replay UI served from
the same loopback origin (``replay``; under development).
"""

from __future__ import annotations

import argparse
import contextlib
import math
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import TextIO

from agentbodycam import __version__
from agentbodycam.core.active import (
    ActiveSessionInfo,
    ActiveState,
    RecordingInProgressError,
    get_active_session,
)
from agentbodycam.core.checks import CheckKind
from agentbodycam.core.command import run_wrapped_command
from agentbodycam.core.doctor import collect_environment, format_report
from agentbodycam.core.forensics import analyze_session
from agentbodycam.core.recorder import (
    DEFAULT_INTERVAL_SECONDS,
    GitStatus,
    Recorder,
    StopReason,
    stop_on_signals,
)
from agentbodycam.core.replay import ReplayService
from agentbodycam.core.replay_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    BindAddressError,
    ReplayServer,
    resolve_bind_host,
)
from agentbodycam.core.store import EventStore, StoreError
from agentbodycam.core.ui_assets import UiBundle, UiUnavailableError, load_ui_bundle
from agentbodycam.models.events import Event, EventSource, EventType, EventValidationError
from agentbodycam.models.forensics import (
    ChangeWindow,
    FailureType,
    ForensicReport,
    GitStateSummary,
    RecordingState,
)
from agentbodycam.models.session import InvalidSessionIdError, Session

PROG = "agentbodycam"
TAGLINE = "Your AI coding agent now has a bodycam."
MIN_INTERVAL_SECONDS = 0.05
MAX_INTERVAL_SECONDS = 3600.0
MAX_CLI_TIMELINE_ITEMS = 40

_EVENT_LABELS = {
    EventType.FILE_CREATED: "CREATED",
    EventType.FILE_MODIFIED: "MODIFIED",
    EventType.FILE_DELETED: "DELETED",
    EventType.GIT_SNAPSHOT: "GIT",
    EventType.ERROR_OBSERVED: "ERROR",
}
CHECK_KINDS = [kind.value for kind in CheckKind]
RUN_USAGE = f"{PROG} run [-h] [--project DIR] [--check KIND] -- <command> [args...]"
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


def _loopback_host(text: str) -> str:
    try:
        resolve_bind_host(text)
    except BindAddressError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return text


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            f"{TAGLINE}\n\nEarly development: file-system and read-only Git state recording, "
            "plus commands explicitly launched with `agentbodycam run -- ...` and their "
            "test/build/lint/typecheck results, and a deterministic, read-only forensic "
            "report of a recorded session (`agentbodycam analyze`), served locally by a "
            "read-only, authenticated Replay API (`agentbodycam replay-api`) and browsable "
            "in an early local Replay UI (`agentbodycam replay`; sessions, timeline with "
            "playback controls, findings). Other terminal commands and agent internals are "
            "not captured."
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

    run = subparsers.add_parser(
        "run",
        parents=[project],
        usage=RUN_USAGE,
        help="Run a command and record it (and its check result) in the active recording.",
        description=(
            "Run <command> [args...] and record command.started / command.finished in the "
            "project's active recording (start one with `agentbodycam record`). Without an "
            "active recording the command is not run.\n\n"
            "Everything after `--` is passed to the command exactly as given: no shell is "
            "involved, so `&&`, `|`, `>` and `;` are ordinary arguments. To use a shell, run "
            "it explicitly (e.g. `cmd /c ...` or `sh -c ...`). stdin, stdout, stderr and the "
            "environment are inherited and never recorded; arguments are recorded with "
            "secrets redacted.\n\n"
            "Checks: a command classified as a test, build, lint or typecheck also gets a "
            "check.passed or check.failed event after command.finished. It passes only if it "
            "exited by itself with status 0; output is never analysed and no cause is "
            "inferred. Use --check when certainty matters. Without it, only a conservative "
            "whitelist of commands that appear to actually execute a check is recognised "
            "(e.g. `python -m pytest`, `cargo test`, `npm run build`, `ruff check`, `mypy`, "
            "`tsc --noEmit`); anything else, including informational runs such as "
            "`pytest --version`, `mypy --help` or `pytest --collect-only`, stays a plain "
            "command.\n\n"
            "Examples:\n"
            f"  {PROG} run --check test -- python -m pytest\n"
            f"  {PROG} run --check build -- cmd /c npm run build\n"
            f"  {PROG} run -- python -m pytest      (recognised automatically as a test)\n"
            f"  {PROG} run -- python script.py      (plain command, no check)\n\n"
            "Exit status: the command's own exit status (128+N if a POSIX signal N ended it). "
            "AgentBodycam's own failures: 125 (no active recording or other setup error), "
            "126 (cannot execute), 127 (executable not found), 130 (interrupted before the "
            "command started)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "--check",
        choices=CHECK_KINDS,
        default=None,
        metavar="KIND",
        help=(
            f"Record the command's result as a check of this kind ({', '.join(CHECK_KINDS)}). "
            "Overrides automatic classification."
        ),
    )

    analyze = subparsers.add_parser(
        "analyze",
        parents=[project],
        help="Deterministic forensic report for a recorded session (read-only, no AI).",
        description=(
            "Reconstruct what a recorded session shows: the ordered timeline, the first "
            "observable failure, repeated failures and later passes of the same check, "
            "failures still unresolved at the end of the recording, file changes observed "
            "around them, incomplete commands and the last recorded Git state.\n\n"
            "Findings describe order, never causes: 'pytest failed, then later passed' is "
            "reported; why it passed is not. Reads only stored evidence: writes nothing, "
            "runs no Git command and makes no network request. A session that is still "
            "being recorded is analysed as a snapshot of the evidence so far."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze.add_argument(
        "--json",
        action="store_true",
        help="Print the report as deterministic JSON (the same report, machine-readable).",
    )
    analyze.add_argument("session_id", metavar="<session-id>")

    replay_api = subparsers.add_parser(
        "replay-api",
        parents=[project],
        help="Serve the local, read-only Replay API only (JSON; no UI).",
        description=(
            "Serve recorded sessions, their forensic reports and paginated timelines as "
            "JSON on a loopback address, for scripts and UI development. Runs in the "
            "foreground until Ctrl+C. (`agentbodycam replay` serves the same API together "
            "with the Replay UI.)\n\n"
            "Security: listens on 127.0.0.1 (or ::1 / localhost) only; public and LAN "
            "addresses are refused. A new random API token is printed at startup; it is "
            "held in memory only and stops working when the server stops. Every data "
            "endpoint requires `Authorization: Bearer <token>`. Read-only: GET and HEAD "
            "only, nothing is written, no Git or other process is run, no CORS is offered "
            "and no outbound network request is made. Starting the server creates nothing "
            "in the project.\n\n"
            "Routes: /api/v1/health (no token), /api/v1/project, /api/v1/sessions, "
            "/api/v1/sessions/<id>, /api/v1/sessions/<id>/report, "
            "/api/v1/sessions/<id>/timeline?after_sequence=N&limit=N"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    replay_api.add_argument(
        "--host",
        type=_loopback_host,
        default=DEFAULT_HOST,
        metavar="HOST",
        help="Loopback address to listen on: 127.0.0.1 (default), ::1 or localhost.",
    )
    replay_api.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        metavar="PORT",
        help="Port to listen on (default: 0, a free port chosen by the operating system).",
    )

    replay = subparsers.add_parser(
        "replay",
        parents=[project],
        help="Open the local Replay UI (under development: sessions, timeline, findings).",
        description=(
            "Serve the Replay UI and the Replay API from one loopback address, then open the "
            "printed address in your browser. Runs in the foreground until Ctrl+C. The UI "
            "is under development: it lists recorded sessions and shows each session's "
            "overview, paginated timeline with playback controls and a scrubber, findings, "
            "observed files and last recorded Git state, plus a local privacy-safe export "
            "(SVG share card, JSON summary, generated WebM replay clip) produced in the "
            "browser; GIF/MP4 export and hosted sharing are not built.\n\n"
            "Security: listens on 127.0.0.1 (or ::1 / localhost) only. The UI ships inside "
            "the package (no Node.js needed) and is served from the API's own origin, so no "
            "CORS is involved. A new random API token is created at startup, held in memory "
            "only and not printed: the UI page served at this address receives it directly "
            "from the server, never through the URL, and keeps it in memory only. Other web "
            "sites cannot obtain it. Any program on this computer that can reach the "
            "loopback port could, so use `replay-api` on shared multi-user machines. "
            "Read-only: nothing is written, no Git or other process is run and no outbound "
            "network request is made."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    replay.add_argument(
        "--host",
        type=_loopback_host,
        default=DEFAULT_HOST,
        metavar="HOST",
        help="Loopback address to listen on: 127.0.0.1 (default), ::1 or localhost.",
    )
    replay.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        metavar="PORT",
        help="Port to listen on (default: 0, a free port chosen by the operating system).",
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


def _window_lines(window: ChangeWindow | None, indent: str) -> list[str]:
    if window is None:
        return []
    lines = [f"{indent}{_display(window.description)}"]
    for change in window.file_changes:
        path = _display(change.path) if change.path is not None else "(path not recorded)"
        lines.append(f"{indent}  #{change.sequence:<5} {change.change:<9} {path}")
    if window.file_changes_truncated:
        hidden = window.file_change_count - len(window.file_changes)
        lines.append(f"{indent}  ... {hidden} more (see --json)")
    return lines


def _more(total: int, shown: int, indent: str = "  ") -> list[str]:
    return [f"{indent}... {total - shown} more (see --json)"] if total > shown else []


def _git_line(git: GitStateSummary) -> str:
    if not git.repository:
        return f"not a Git repository ({git.unavailable_reason or 'unavailable'})"
    if git.detached:
        where = f"detached HEAD at {git.head or '(none)'}"
    elif git.head is None:
        where = f"branch {git.branch or '(unknown)'} (no commits yet)"
    else:
        where = f"branch {git.branch or '(unknown)'} at {git.head}"
    text = f"{where}, {git.state or 'unknown'}"
    if git.state == "dirty":
        text += (
            f" ({git.staged} staged, {git.unstaged} unstaged, {git.untracked} untracked"
            + (f", {git.conflicted} conflicted" if git.conflicted else "")
            + ")"
        )
    return f"{_display(text)} (event #{git.sequence})"


_STATE_TEXT = {
    RecordingState.STOPPED: "stopped",
    RecordingState.RECORDING: "recording in progress (snapshot of the evidence so far)",
    RecordingState.ENDED_WITHOUT_STOP: "ended without a clean stop",
    RecordingState.UNKNOWN: "recording state unknown",
    RecordingState.NOT_STARTED: "never started recording",
}


def render_report(report: ForensicReport, *, max_timeline: int = MAX_CLI_TIMELINE_ITEMS) -> str:
    """Concise human-readable rendering of a :class:`ForensicReport` (no analysis here)."""
    completeness = "complete" if report.analysis_complete else "incomplete"
    lines = [
        "AgentBodycam forensic report",
        f"Session: {report.session_id}",
        f"Recording: {_STATE_TEXT[report.recording_state]} (analysis {completeness})",
        f"Started: {report.started_at}   Ended: {report.ended_at or '-'}",
        f"Events: {report.event_count}",
        "Findings are observed order only; AgentBodycam does not infer why anything happened.",
        "",
    ]
    first = report.first_observable_failure
    if first is None:
        lines.append("No observable check failure was recorded.")
    else:
        lines += ["First observable failure", f"  #{first.sequence:<5} {_display(first.summary)}"]
    lines.append("")

    if report.recoveries.total:
        lines.append("Observed recoveries (later pass of the same check)")
        for recovery in report.recoveries.items:
            lines.append(f"  {_display(recovery.summary)}")
            lines.append(f"    Failed attempts: {recovery.failed_attempts}")
            if recovery.changes_before_failure is None:
                lines.append(f"    {recovery.previous_pass_note}")
            else:
                lines += _window_lines(recovery.changes_before_failure, "    ")
            lines += _window_lines(recovery.changes_between_failure_and_pass, "    ")
        lines += _more(report.recoveries.total, len(report.recoveries.items))
        lines.append("")

    if report.unresolved_failures.total:
        lines.append("Unresolved in this recording")
        for failure in report.unresolved_failures.items:
            lines.append(f"  {_display(failure.summary)}")
            if failure.changes_before_failure is None:
                lines.append(f"    {failure.previous_pass_note}")
            else:
                lines += _window_lines(failure.changes_before_failure, "    ")
            lines += _window_lines(failure.changes_after_last_failure, "    ")
        lines += _more(report.unresolved_failures.total, len(report.unresolved_failures.items))
        lines.append("")

    operational = [
        f
        for f in report.observable_failures.items
        if f.failure_type is not FailureType.CHECK_FAILED
    ]
    if operational:
        lines.append("Commands that did not complete normally")
        lines += [f"  #{f.sequence:<5} {_display(f.summary)}" for f in operational]
        lines.append("")

    if report.incomplete_commands.total:
        lines.append("Incomplete commands")
        for command in report.incomplete_commands.items:
            lines.append(f"  #{command.started_sequence:<5} {_display(command.command_summary)}")
            lines.append(f"         {command.summary}")
        lines += _more(report.incomplete_commands.total, len(report.incomplete_commands.items))
        lines.append("")

    if report.errors_observed.total:
        lines.append("Errors observed (AgentBodycam, file system or Git; not project failures)")
        lines += [f"  #{e.sequence:<5} {_display(e.summary)}" for e in report.errors_observed.items]
        lines += _more(report.errors_observed.total, len(report.errors_observed.items))
        lines.append("")

    files = report.file_change_summary
    lines += [
        "Final evidence (observed during this recording)",
        f"  Files: {files.modified_events} modified, {files.created_events} created, "
        f"{files.deleted_events} deleted ({files.unique_paths} "
        f"{'path' if files.unique_paths == 1 else 'paths'})",
    ]
    git = report.latest_git_state
    lines.append(f"  Last recorded Git state: {_git_line(git) if git else 'none recorded'}")
    states = report.final_check_states
    if states.total:
        lines.append("  Checks (latest recorded result):")
        for state in states.items:
            verdict = "PASS" if state.latest_result == "passed" else "FAIL"
            name = _display(state.display_name)
            lines.append(f"    {name:<40} {verdict}  (event #{state.latest_sequence})")
        lines += _more(states.total, len(states.items), "    ")
    else:
        lines.append("  Checks: no check results were recorded")
    lines.append("")

    if report.warnings.total:
        lines.append("Evidence notes")
        for warning in report.warnings.items:
            lines.append(f"  [{warning.severity.value}] {_display(warning.message)}")
        lines += _more(report.warnings.total, len(report.warnings.items))
        lines.append("")

    shown = report.timeline.items[:max_timeline]
    lines.append(f"Timeline ({len(shown)} of {report.timeline.total} events)")
    for item in shown:
        lines.append(
            f"  #{item.sequence:<5} {item.timestamp[11:19]}  {item.type:<17} "
            f"{_display(item.summary)}"
        )
    lines += _more(report.timeline.total, len(shown))
    return "\n".join(lines) + "\n"


def _run_analyze(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    project: Path = args.project if args.project is not None else Path.cwd()
    try:
        store = EventStore(project)
        report = analyze_session(store, args.session_id)
    except InvalidSessionIdError as exc:
        err.write(f"error: {exc}\n")
        return 2
    except (StoreError, EventValidationError, OSError) as exc:
        err.write(f"error: {exc}\n")
        return 1
    _write(out, report.to_json() + "\n" if args.json else render_report(report))
    return 0


@contextlib.contextmanager
def _stop_event_on_signals(stop: threading.Event) -> Iterator[None]:
    """Ctrl+C, Ctrl+Break and SIGTERM set ``stop``. No-op outside the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    def handler(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, handler) for sig in signals}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _run_replay_api(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    stop: threading.Event | None = None,
) -> int:
    return _serve_replay(args, out, err, stop, ui=None)


def _run_replay(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    stop: threading.Event | None = None,
) -> int:
    try:
        ui = load_ui_bundle()
    except (UiUnavailableError, OSError):
        err.write(
            "error: the Replay UI files are missing from this AgentBodycam installation; "
            "reinstall AgentBodycam (or, in a source checkout, run `npm run build` in web/). "
            "`agentbodycam replay-api` still serves the JSON API.\n"
        )
        return 1
    return _serve_replay(args, out, err, stop, ui=ui)


def _serve_replay(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    stop: threading.Event | None,
    *,
    ui: UiBundle | None,
) -> int:
    project: Path = args.project if args.project is not None else Path.cwd()
    if not project.is_dir():
        err.write("error: the project directory does not exist or is not a directory\n")
        return 1
    try:
        # Fails early on a symlinked/junctioned store; creates nothing.
        EventStore(project).store_root_exists()
    except StoreError:
        err.write(
            "error: the project's .agentbodycam store is not a plain directory inside the "
            "project; refusing to serve it\n"
        )
        return 1
    try:
        server = ReplayServer(
            ReplayService(project),
            host=args.host,
            port=args.port,
            log=lambda line: _write(out, f"  {line}\n"),
            ui=ui,
        )
    except BindAddressError as exc:
        err.write(f"error: {exc}\n")
        return 2
    except OSError as exc:
        err.write(f"error: could not listen on {args.host} port {args.port}: {exc.strerror}\n")
        return 1
    if ui is None:
        banner = (
            "AgentBodycam Replay API\n"
            f"Project: {_display(str(project.resolve()))}\n"
            f"Listening: {server.url}\n"
            "Authentication: enabled (send `Authorization: Bearer <token>`)\n"
            f"API token: {server.token}\n"
            "The token is temporary: it is held in memory only and stops working when "
            "this server stops.\n"
            "Local development API (read-only, loopback only, JSON only). "
            "For the Replay UI, run `agentbodycam replay`.\n"
        )
        stopped = "Replay API stopped."
    else:
        # The token is deliberately not printed: the UI page receives it from the server.
        banner = (
            "AgentBodycam Replay (UI preview)\n"
            f"Project: {_display(str(project.resolve()))}\n"
            f"Listening: {server.url}\n"
            f"Open in your browser: {server.url}/\n"
            "The Replay UI is under development: playback and local privacy-safe export "
            "(SVG card, JSON summary, WebM replay clip) exist; GIF/MP4 export does not.\n"
            "Access is granted automatically to the page served at this address; the "
            "address contains no secret and nothing is stored in your browser.\n"
            "Read-only, loopback only.\n"
        )
        stopped = "Replay stopped."
    stop = stop if stop is not None else threading.Event()
    try:
        with _stop_event_on_signals(stop):
            thread = server.start()
            _write(out, banner + "Press Ctrl+C to stop.\n\n")
            while thread.is_alive() and not stop.wait(0.2):
                pass
    finally:
        server.stop()
    _write(out, f"\n{stopped}\n")
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
        check = None if args.check is None else CheckKind(args.check)
        return run_wrapped_command(project, command_argv, err=err_stream, check=check)
    if args.command == "doctor":
        return _run_doctor(stream)
    if args.command == "session":
        return _run_session(args, stream, err_stream)
    if args.command == "record":
        return _run_record(args, stream, err_stream)
    if args.command == "analyze":
        return _run_analyze(args, stream, err_stream)
    if args.command == "replay-api":
        return _run_replay_api(args, stream, err_stream)
    if args.command == "replay":
        return _run_replay(args, stream, err_stream)

    parser.print_help(stream)
    return 0
