"""Wrapped command capture: ``agentbodycam run -- <command> [args...]``.

A wrapped command attaches to the project's *currently active* recording (never a stale
or half-published one), appends ``command.started``, runs the command exactly as given
(argv list, ``shell=False``, inherited stdin/stdout/stderr and environment), then appends
``command.finished`` with the same ``command_id``. Both appends go through
:meth:`EventStore.append_recording_event` and re-verify, while holding the session's event
lock, that the very same recording (session ID *and* lease ID) is still live, so nothing is
ever written into another session or after ``session.stopped``.

If the command is classified as a check (``--check KIND``, or automatically for a short
whitelist of unmistakable command lines, see :mod:`agentbodycam.core.checks`), a
``check.passed`` or ``check.failed`` event with its own ``check_id`` and the command's
``command_id`` is appended after ``command.finished``, and only if that finish was recorded.

Only facts are recorded: which (redacted) command ran, where, when, for how long and how it
ended. Command output, stdin and environment variables are never recorded, and nothing is
attributed to any person or agent. AgentBodycam itself makes no network request; the
wrapped command is the user's and may do anything it likes.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import TextIO

from agentbodycam.core.active import ActiveSessionInfo, ActiveState, get_active_session
from agentbodycam.core.checks import (
    CheckClassification,
    CheckKind,
    CheckReason,
    ClassificationSource,
    classify_command,
    explicit_classification,
    judge,
)
from agentbodycam.core.redact import RedactedCommand, default_home, redact_command
from agentbodycam.core.store import (
    STORE_DIRNAME,
    EventStore,
    SessionNotRecordingError,
    StoreError,
)
from agentbodycam.models.events import (
    MAX_SUMMARY_CHARS,
    Event,
    EventDraft,
    EventSource,
    EventType,
    EventValidationError,
    JsonObject,
    validate_relative_path,
)

ATTACH_TIMEOUT_SECONDS = 1.0
WRAPPER_KIND = "agentbodycam-run"

# AgentBodycam's own failures, chosen like `docker run` / POSIX shells so they are
# distinguishable from most child exit codes.
EXIT_SETUP_FAILED = 125  # no active recording, unsafe directory, store error, ...
EXIT_CANNOT_EXECUTE = 126  # found but could not be started (permission, batch script, ...)
EXIT_NOT_FOUND = 127  # executable not found
EXIT_INTERRUPTED = 130  # interrupted before the command could start (128 + SIGINT)

_POLL_SECONDS = 0.05
_MIN_BACKOFF_SECONDS = 0.01
_MAX_BACKOFF_SECONDS = 0.1
_WINDOWS = sys.platform == "win32"
_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".com")
_WINDOWS_SHELL_SCRIPT_SUFFIXES = (".bat", ".cmd")


class CommandError(Exception):
    """Base class for wrapped-command failures."""


class NoActiveRecordingError(CommandError):
    """No live recording could be attached to; the command must not run."""

    def __init__(self, info: ActiveSessionInfo) -> None:
        super().__init__("no active AgentBodycam recording for this project")
        self.info = info


class RecordingChangedError(CommandError):
    """The recording that was attached to is no longer the project's live recording."""


class UnsafeWorkingDirectoryError(CommandError):
    pass


class ShellRequiredError(CommandError):
    """The executable is a Windows batch script, which only ``cmd.exe`` can run."""

    def __init__(self, script: str) -> None:
        super().__init__(f"{script} is a batch script and can only run through cmd.exe")
        self.script = script


class Outcome(StrEnum):
    EXITED = "exited"  # the child exited on its own with exit_code
    SIGNALED = "signaled"  # POSIX: the child was terminated by a signal
    SPAWN_ERROR = "spawn_error"  # the child could not be started
    INTERRUPTED = "interrupted"  # AgentBodycam received an interrupt while the child ran


@dataclass(frozen=True, slots=True)
class Attachment:
    """The live recording a wrapped command writes into."""

    project_root: Path
    session_id: str
    lease_id: str


ActiveProbe = Callable[[Path], ActiveSessionInfo]


def _poll(
    project_root: Path,
    accept: Callable[[ActiveSessionInfo], bool],
    *,
    timeout: float,
    probe: ActiveProbe,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> ActiveSessionInfo:
    """Probe until ``accept`` says yes or the state stops being transient.

    Only :attr:`ActiveState.INVALID` (a live recorder is publishing or tearing down its
    metadata) and transient OS read errors are retried, for at most ``timeout`` seconds.
    """
    deadline = monotonic() + timeout
    backoff = _MIN_BACKOFF_SECONDS
    while True:
        try:
            info = probe(project_root)
        except OSError as exc:
            info = ActiveSessionInfo(ActiveState.INVALID, reason=f"could not be read: {exc}")
        if accept(info) or info.state is not ActiveState.INVALID:
            return info
        remaining = deadline - monotonic()
        if remaining <= 0:
            return info
        sleep(min(backoff, remaining))
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)


def attach_active_session(
    project_root: Path,
    *,
    timeout: float = ATTACH_TIMEOUT_SECONDS,
    probe: ActiveProbe = get_active_session,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Attachment:
    """Attach to the project's live recording, or raise :class:`NoActiveRecordingError`.

    Only :attr:`ActiveState.ACTIVE` is accepted; stale metadata never is.
    """
    root = Path(project_root).resolve()
    info = _poll(
        root,
        lambda i: i.state is ActiveState.ACTIVE,
        timeout=timeout,
        probe=probe,
        sleep=sleep,
        monotonic=monotonic,
    )
    if info.state is not ActiveState.ACTIVE or info.record is None:
        raise NoActiveRecordingError(info)
    return Attachment(root, info.record.session_id, info.record.lease_id)


def _is_same(attachment: Attachment, info: ActiveSessionInfo) -> bool:
    record = info.record
    return (
        info.state is ActiveState.ACTIVE
        and record is not None
        and record.session_id == attachment.session_id
        and record.lease_id == attachment.lease_id
    )


def confirm_attachment(
    attachment: Attachment,
    *,
    timeout: float = ATTACH_TIMEOUT_SECONDS,
    probe: ActiveProbe = get_active_session,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Raise :class:`RecordingChangedError` unless the attached recording is still live."""
    info = _poll(
        attachment.project_root,
        lambda i: _is_same(attachment, i),
        timeout=timeout,
        probe=probe,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not _is_same(attachment, info):
        raise RecordingChangedError(_changed_reason(attachment, info))


def _changed_reason(attachment: Attachment, info: ActiveSessionInfo) -> str:
    record = info.record
    if info.state is ActiveState.ACTIVE and record is not None:
        if record.session_id != attachment.session_id:
            return f"another recording ({record.session_id}) is now active"
        return "the active recording's lease changed"
    return f"the recording is no longer active ({info.state.value})"


def append_to_recording(
    store: EventStore,
    attachment: Attachment,
    draft: EventDraft,
    *,
    probe: ActiveProbe = get_active_session,
) -> Event:
    """Append ``draft`` only while the attached recording is still the live one.

    The session status and the session/lease identity are both checked while holding the
    session's event lock. Raises :class:`RecordingChangedError` otherwise.
    """

    def check() -> None:
        info = probe(attachment.project_root)
        if not _is_same(attachment, info):
            raise RecordingChangedError(_changed_reason(attachment, info))

    try:
        return store.append_recording_event(attachment.session_id, draft, check=check)
    except SessionNotRecordingError as exc:
        raise RecordingChangedError(f"the recording has ended ({exc})") from exc


def select_working_directory(project_root: Path, cwd: Path) -> tuple[Path, str]:
    """The directory to run in and its project-relative POSIX form (``.`` for the root).

    ``cwd`` is used when it is inside the project, otherwise the project root.
    """
    root = Path(project_root).resolve()
    here = Path(cwd).resolve()
    if here != root and not here.is_relative_to(root):
        return root, "."
    relative = here.relative_to(root).as_posix()
    if relative == ".":
        return root, "."
    try:
        validate_relative_path(relative)
    except EventValidationError as exc:
        raise UnsafeWorkingDirectoryError(f"working directory cannot be recorded: {exc}") from None
    if relative.split("/", 1)[0].lower() == STORE_DIRNAME:
        raise UnsafeWorkingDirectoryError(
            f"refusing to run a command inside AgentBodycam's own {STORE_DIRNAME}/ directory"
        )
    return here, relative


def resolve_executable(name: str, cwd: Path, path_env: str | None = None) -> str:
    """Absolute path of the program ``name`` names, without ever involving a shell.

    A name containing a path separator is taken relative to ``cwd``. A bare name is looked
    up only in *absolute* PATH entries (the current directory is never searched implicitly).
    On Windows ``.exe``/``.com`` are tried; batch scripts (``.bat``/``.cmd``) raise
    :class:`ShellRequiredError` because Windows can only run them through ``cmd.exe``.
    Raises :class:`FileNotFoundError` when nothing is found.
    """
    if not name:
        raise FileNotFoundError(2, "empty executable name")
    separators = ("/", "\\") if _WINDOWS else ("/",)
    if any(sep in name for sep in separators):
        base = Path(name) if Path(name).is_absolute() else cwd / name
        if not _WINDOWS:
            # Like execv: an explicit path is used as is; the OS reports a missing
            # permission as PermissionError.
            if base.is_file():
                return str(base)
            raise FileNotFoundError(2, "executable not found")
        directories, stem = [base.parent], base.name
    else:
        search = os.environ.get("PATH", "") if path_env is None else path_env
        directories = []
        for entry in search.split(os.pathsep):
            entry = entry.strip().strip('"')
            if entry and Path(entry).is_absolute():
                directories.append(Path(entry))
        stem = name
    for directory in directories:
        found = _find_in(directory, stem)
        if found is not None:
            return found
    raise FileNotFoundError(2, "executable not found")


def _find_in(directory: Path, stem: str) -> str | None:
    if not _WINDOWS:
        candidate = directory / stem
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    suffix = Path(stem).suffix.lower()
    if suffix in _WINDOWS_SHELL_SCRIPT_SUFFIXES:
        if (directory / stem).is_file():
            raise ShellRequiredError(stem)
        return None
    names = [stem] if suffix in _WINDOWS_EXECUTABLE_SUFFIXES else []
    names += [stem + ext for ext in _WINDOWS_EXECUTABLE_SUFFIXES]
    for candidate_name in names:
        candidate = directory / candidate_name
        if candidate.is_file():
            return str(candidate)
    for ext in _WINDOWS_SHELL_SCRIPT_SUFFIXES:
        if (directory / (stem + ext)).is_file():
            raise ShellRequiredError(stem + ext)
    return None


# -- signals ---------------------------------------------------------------------------


if sys.platform == "win32":
    _RELAYED_SIGNALS = (signal.SIGINT, signal.SIGBREAK)

    def _terminal_delivers_to_child() -> bool:
        return True

else:
    _RELAYED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def _terminal_delivers_to_child() -> bool:
        """Are we in the terminal's foreground process group?

        If so, a Ctrl+C typed in that terminal was delivered by the kernel to the child
        too (it shares our process group), and forwarding it would deliver it twice.
        """
        group = os.getpgrp()
        for fd in (0, 1, 2):
            with contextlib.suppress(OSError):
                if os.tcgetpgrp(fd) == group:
                    return True
        return False


@dataclass(slots=True)
class _InterruptRelay:
    """Keeps AgentBodycam alive through interrupts so it always outlives its child.

    Windows: the console delivers Ctrl+C / Ctrl+Break to every process attached to it,
    including the child, so nothing is forwarded. POSIX: SIGTERM is always forwarded to the
    child; SIGINT and SIGHUP only when they cannot have come from our terminal (we are not
    its foreground process group), because the terminal already sent them to the child.
    """

    received: list[str] = field(default_factory=list)
    process: subprocess.Popen[bytes] | None = None

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        self.received.append(signal.Signals(signum).name)
        process = self.process
        if process is None or _WINDOWS:
            return
        if signum == signal.SIGTERM or not _terminal_delivers_to_child():
            with contextlib.suppress(OSError):
                process.send_signal(signum)

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle) for sig in _RELAYED_SIGNALS}
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


# -- events ----------------------------------------------------------------------------


def new_command_id() -> str:
    return secrets.token_hex(16)


def _summary(text: str) -> str:
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 3] + "..."


def started_draft(command_id: str, command: RedactedCommand, working_directory: str) -> EventDraft:
    return EventDraft(
        type=EventType.COMMAND_STARTED,
        source=EventSource.COMMAND,
        summary=_summary(f"Command started: {command.display()}"),
        payload={
            "command_id": command_id,
            "executable": command.executable,
            "argv": list(command.argv),
            "argument_count": command.argument_count,
            "argv_redacted": command.redacted,
            "argv_truncated": command.truncated,
            "argv_paths_rewritten": command.paths_rewritten,
            # Parallel to argv: how each item was produced (additive; older recordings omit it).
            "argv_item_origins": [origin.value for origin in command.item_origins],
            "working_directory": working_directory,
            "shell": False,
            "stdio": "inherited",
            "output_recorded": False,
            "environment_recorded": False,
            "wrapper": {"kind": WRAPPER_KIND, "pid": os.getpid()},
        },
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    outcome: Outcome
    exit_code: int | None
    signal: int | None
    duration_ms: int
    pid: int | None
    interrupts: tuple[str, ...]
    error_type: str | None = None
    errno: int | None = None

    @property
    def wrapper_exit_code(self) -> int:
        """What ``agentbodycam run`` exits with: normally the child's own exit code."""
        if self.outcome is Outcome.SPAWN_ERROR:
            return EXIT_NOT_FOUND if self.error_type == "FileNotFoundError" else EXIT_CANNOT_EXECUTE
        if self.signal is not None:
            return 128 + self.signal
        if self.exit_code is None:
            return EXIT_INTERRUPTED
        return self.exit_code

    def describe(self) -> str:
        if self.outcome is Outcome.SPAWN_ERROR:
            return f"could not start: {self.error_type}"
        parts = []
        if self.outcome is Outcome.INTERRUPTED:
            parts.append("interrupted")
        if self.signal is not None:
            parts.append(f"signal {_signal_name(self.signal) or self.signal}")
        elif self.exit_code is not None:
            parts.append(f"exit {self.exit_code}")
        elif self.outcome is Outcome.INTERRUPTED:
            parts.append("not started")
        return ", ".join(parts)


def _signal_name(number: int) -> str | None:
    try:
        return signal.Signals(number).name
    except ValueError:
        return None


def finished_draft(command_id: str, command: RedactedCommand, result: CommandResult) -> EventDraft:
    error: JsonObject | None = None
    if result.error_type is not None:
        error = {"type": result.error_type, "errno": result.errno}
    return EventDraft(
        type=EventType.COMMAND_FINISHED,
        source=EventSource.COMMAND,
        summary=_summary(f"Command finished: {command.display()} ({result.describe()})"),
        payload={
            "command_id": command_id,
            "outcome": result.outcome.value,
            "exit_code": result.exit_code,
            "signal": result.signal,
            "signal_name": None if result.signal is None else _signal_name(result.signal),
            "duration_ms": result.duration_ms,
            "pid": result.pid,
            "interrupts": list(result.interrupts),
            "error": error,
        },
    )


def new_check_id() -> str:
    return secrets.token_hex(16)


def should_record_check(classification: CheckClassification, result: CommandResult) -> bool:
    """An explicit check always gets a result; an automatic one only if it really started."""
    return classification.source is ClassificationSource.EXPLICIT or result.pid is not None


def _check_detail(result: CommandResult, reason: CheckReason) -> str:
    if reason is CheckReason.SPAWN_ERROR:
        return "could not start"
    if reason is CheckReason.SIGNALED and result.signal is not None:
        return f"signal {_signal_name(result.signal) or result.signal}"
    if reason is CheckReason.INTERRUPTED:
        return "interrupted" + ("" if result.exit_code is None else f", exit {result.exit_code}")
    return f"exit {result.exit_code}"


def check_draft(
    check_id: str, command_id: str, classification: CheckClassification, result: CommandResult
) -> EventDraft:
    passed, reason = judge(result.outcome.value, result.exit_code)
    verdict = "passed" if passed else "failed"
    summary = f"{classification.kind.label} {verdict}: {classification.name}"
    if not passed:
        summary += f" ({_check_detail(result, reason)})"
    return EventDraft(
        type=EventType.CHECK_PASSED if passed else EventType.CHECK_FAILED,
        source=EventSource.COMMAND,
        summary=_summary(summary),
        payload={
            "check_id": check_id,
            "command_id": command_id,
            "kind": classification.kind.value,
            "name": classification.name,
            "classification": classification.source.value,
            "result": verdict,
            "reason": reason.value,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        },
    )


# -- running ---------------------------------------------------------------------------


def _spawn(argv: Sequence[str], executable: str, cwd: Path) -> subprocess.Popen[bytes]:
    # argv is passed as a list with shell=False: no shell ever parses it. stdin, stdout,
    # stderr and the environment are inherited unchanged and never captured.
    return subprocess.Popen(list(argv), executable=executable, cwd=cwd, shell=False)  # noqa: S603


def _wait(process: subprocess.Popen[bytes]) -> int:
    # Short timed waits so interrupt handlers run promptly on every platform.
    while True:
        try:
            return process.wait(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            continue


def execute(
    argv: Sequence[str], cwd: Path, relay: _InterruptRelay
) -> tuple[CommandResult, BaseException | None]:
    """Start ``argv`` in ``cwd`` and wait for it. Never raises for spawn failures."""
    started = time.monotonic_ns()

    def elapsed() -> int:
        return (time.monotonic_ns() - started) // 1_000_000

    if relay.received:
        return CommandResult(
            Outcome.INTERRUPTED, None, None, elapsed(), None, tuple(relay.received)
        ), None
    try:
        executable = resolve_executable(argv[0], cwd)
        process = _spawn(argv, executable, cwd)
    except ShellRequiredError as exc:
        return CommandResult(
            Outcome.SPAWN_ERROR, None, None, elapsed(), None, (), "ShellRequiredError", None
        ), exc
    except OSError as exc:
        return CommandResult(
            Outcome.SPAWN_ERROR, None, None, elapsed(), None, (), type(exc).__name__, exc.errno
        ), exc
    relay.process = process
    returncode = _wait(process)
    duration = elapsed()
    exit_code: int | None = returncode
    signal_number: int | None = None
    if returncode < 0:  # POSIX: terminated by a signal
        exit_code, signal_number = None, -returncode
    interrupts = tuple(relay.received)
    if interrupts:
        outcome = Outcome.INTERRUPTED
    elif signal_number is not None:
        outcome = Outcome.SIGNALED
    else:
        outcome = Outcome.EXITED
    return CommandResult(outcome, exit_code, signal_number, duration, process.pid, interrupts), None


def process_exit_status(code: int) -> int:
    """``code`` in a form ``sys.exit`` reproduces exactly (Windows NTSTATUS values)."""
    if _WINDOWS and code >= 2**31:
        return code - 2**32
    return code


def _display(text: str) -> str:
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def _write(stream: TextIO, text: str) -> None:
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    stream.flush()


def _no_active_message(info: ActiveSessionInfo) -> str:
    lines = ["error: no active AgentBodycam recording for this project"]
    if info.state is ActiveState.STALE:
        lines.append(
            f"(stale metadata from a recorder that is no longer running was ignored: {info.reason})"
        )
    elif info.state is ActiveState.INVALID:
        lines.append(f"(a recorder holds the lock, but its recording is not usable: {info.reason})")
    lines.append("Start one with: agentbodycam record")
    return "\n".join(lines) + "\n"


def _spawn_error_message(argv0: str, error: BaseException) -> str:
    name = _display(argv0)
    if isinstance(error, ShellRequiredError):
        return (
            f"error: '{name}' is a Windows batch script ({_display(error.script)}), which can "
            "only run through cmd.exe.\nAgentBodycam never adds a shell on its own. To run it "
            "through cmd.exe explicitly, use:\n"
            "  agentbodycam run -- cmd /c <command> [args...]\n"
        )
    if isinstance(error, FileNotFoundError):
        return f"error: could not start '{name}': executable not found\n"
    return f"error: could not start '{name}': {type(error).__name__}\n"


def run_wrapped_command(
    project: Path,
    argv: Sequence[str],
    *,
    err: TextIO,
    cwd: Path | None = None,
    check: CheckKind | None = None,
    attach_timeout: float = ATTACH_TIMEOUT_SECONDS,
) -> int:
    """``agentbodycam run``: returns the process exit code (normally the child's).

    ``check`` classifies the command explicitly; without it, automatic classification
    applies. Classification never changes what is run.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    classification = (
        classify_command(argv) if check is None else explicit_classification(check, argv)
    )
    project_root = Path(project).resolve()
    try:
        attachment = attach_active_session(project_root, timeout=attach_timeout)
    except NoActiveRecordingError as exc:
        _write(err, _no_active_message(exc.info))
        return EXIT_SETUP_FAILED
    except (StoreError, OSError) as exc:
        _write(err, f"error: {exc}\n")
        return EXIT_SETUP_FAILED
    try:
        run_dir, working_directory = select_working_directory(
            project_root, Path.cwd() if cwd is None else cwd
        )
    except (UnsafeWorkingDirectoryError, OSError) as exc:
        _write(err, f"error: {exc}\n")
        return EXIT_SETUP_FAILED

    store = EventStore(project_root)
    command_id = new_command_id()
    command = redact_command(argv, project_root=project_root, home=default_home())
    relay = _InterruptRelay()
    with relay.installed():
        try:
            confirm_attachment(attachment, timeout=attach_timeout)
            append_to_recording(
                store, attachment, started_draft(command_id, command, working_directory)
            )
        except RecordingChangedError as exc:
            _write(
                err, f"error: the AgentBodycam recording ended before the command started: {exc}\n"
            )
            return EXIT_SETUP_FAILED
        except (StoreError, EventValidationError, OSError) as exc:
            _write(err, f"error: could not record the command start: {exc}\n")
            return EXIT_SETUP_FAILED
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
        result, spawn_error = execute(argv, run_dir, relay)
        if spawn_error is not None:
            _write(err, _spawn_error_message(argv[0], spawn_error))
        finished_recorded = False
        try:
            confirm_attachment(attachment, timeout=attach_timeout)
            append_to_recording(store, attachment, finished_draft(command_id, command, result))
            finished_recorded = True
        except RecordingChangedError:
            _write(
                err,
                "warning: the AgentBodycam recording ended before this command finished; "
                "the finish event was not recorded\n",
            )
        except (StoreError, EventValidationError, OSError) as exc:
            _write(err, f"warning: the finish event could not be recorded: {exc}\n")
        if (
            finished_recorded
            and classification is not None
            and should_record_check(classification, result)
        ):
            draft = check_draft(new_check_id(), command_id, classification, result)
            _record_check(store, attachment, draft, err, attach_timeout)
    return process_exit_status(result.wrapper_exit_code)


def _record_check(
    store: EventStore, attachment: Attachment, draft: EventDraft, err: TextIO, timeout: float
) -> None:
    try:
        confirm_attachment(attachment, timeout=timeout)
        append_to_recording(store, attachment, draft)
    except RecordingChangedError:
        _write(
            err,
            "warning: the AgentBodycam recording ended before the check result could be recorded\n",
        )
    except (StoreError, EventValidationError, OSError) as exc:
        _write(err, f"warning: the check result could not be recorded: {exc}\n")
