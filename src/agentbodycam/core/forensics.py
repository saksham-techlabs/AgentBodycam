"""Deterministic, read-only forensic analysis of a recorded session. No AI, no network.

:func:`analyze_session` reads a session's metadata and events through :class:`EventStore`
and returns a :class:`~agentbodycam.models.forensics.ForensicReport`. It writes nothing,
creates no derived events, runs no Git or other process, and never looks at the project's
current files: every finding is computed from stored evidence only.

Findings are *observed* facts about recorded events and their order: "pytest failed at
event #14 and later passed at event #27", "2 file changes were observed in between". They
never say why: ordering is a fact, causation is not.

Rules (see docs/architecture.md, "Local forensics engine"):

- ``event.sequence`` is the only order used. Timestamps are display metadata.
- Observable failures are ``check.failed`` events, plus ``command.finished`` with outcome
  ``spawn_error``, ``signaled`` or ``interrupted`` for commands that have no check event.
  A plain non-zero exit of an unclassified command is not a failure (``git diff --quiet``).
- Two check runs are the *same check* only when their signature matches: a SHA-256 over
  the canonical JSON of the check kind and name and the paired ``command.started``'s
  recorded executable, recorded (redacted) argv and project-relative working directory.
  Absolute project paths are recorded as ``<project>/...``, so they cannot collapse with
  an originally relative argument that happens to look the same after redaction. When the
  recorded argv is lossy (truncated, redacted, or an argument rewritten to
  ``<external-path>``) the run is left ungrouped: missing a recovery is preferred over
  inventing one.
- command.started -> command.finished -> optional check event is validated per
  ``command_id``. Contradictory relationships (duplicate starts, a finish or check without
  a start, a check before its finish, a check that contradicts its finish) are reported as
  warnings and the affected command is excluded from findings; nothing is repaired. A
  start without a finish is legitimate (crash, kill, recording ended) and is reported as
  an incomplete command.

Analysis is O(number of events): one pass builds indexes, file-change windows are answered
with binary search over prefix counts.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentbodycam.core.active import ActiveSessionInfo, ActiveState, get_active_session
from agentbodycam.core.checks import CheckKind, judge
from agentbodycam.core.redact import EXTERNAL_PATH, REDACTED
from agentbodycam.core.store import EventStore, StoreError
from agentbodycam.models.events import (
    Event,
    EventType,
    EventValidationError,
    JsonObject,
    JsonValue,
    dumps_canonical,
    format_timestamp,
)
from agentbodycam.models.forensics import (
    FORENSIC_REPORT_SCHEMA_VERSION,
    OBSERVED,
    Bounded,
    ChangeWindow,
    CheckState,
    CommandSummary,
    ErrorObserved,
    FailureFinding,
    FailureType,
    FileChangeRef,
    FileChangeSummary,
    ForensicReport,
    ForensicWarning,
    GitStateSummary,
    IncompleteCommand,
    PathObservation,
    RecordingState,
    Recovery,
    TimelineItem,
    UnresolvedFailure,
    WarningSeverity,
    bounded,
)
from agentbodycam.models.session import Session, SessionStatus

SIGNATURE_VERSION = 1
SHORT_SIGNATURE_CHARS = 8

# Bounds keep a report small for very long sessions. Lists that are cut keep their full
# count (``total``) and say so (``truncated``); nothing is dropped silently.
DEFAULT_MAX_TIMELINE_ITEMS = 5_000
MAX_WINDOW_CHANGES = 20
MAX_EVIDENCE_REFS = 60
MAX_FINDINGS = 200
MAX_PATHS = 200
MAX_WARNING_EVENT_IDS = 10

NO_EARLIER_PASSING_RESULT = "No earlier passing result for this exact check was recorded."
FILE_SUMMARY_DESCRIPTION = (
    "File events observed during this recording (not necessarily the current file system)."
)
GIT_DESCRIPTION = "Last recorded Git state (not the current Git state)."

_OPERATIONAL_FAILURES = {
    "spawn_error": FailureType.COMMAND_SPAWN_ERROR,
    "signaled": FailureType.COMMAND_SIGNALED,
    "interrupted": FailureType.COMMAND_INTERRUPTED,
}
_FILE_CHANGES = {
    EventType.FILE_CREATED: "created",
    EventType.FILE_MODIFIED: "modified",
    EventType.FILE_DELETED: "deleted",
}
_CHECK_KINDS = frozenset(kind.value for kind in CheckKind)

ActiveProbe = Callable[[Path], ActiveSessionInfo]


class ForensicInputError(ValueError):
    """Events handed to :func:`analyze_events` are not one session's complete, ordered log."""


# -- small helpers -------------------------------------------------------------------------


def _str(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _int(payload: JsonObject, key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(payload: JsonObject, key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _bounded_ids(
    ids: Sequence[str], limit: int = MAX_EVIDENCE_REFS
) -> tuple[tuple[str, ...], bool]:
    """Evidence IDs capped at ``limit``, keeping the earliest and the last three."""
    unique = list(dict.fromkeys(ids))
    if len(unique) <= limit:
        return tuple(unique), False
    tail = 3
    return tuple(unique[: limit - tail] + unique[-tail:]), True


# -- check identity ------------------------------------------------------------------------


def check_signature(kind: str, name: str, started: Event) -> str | None:
    """Deterministic identity of a check run, or ``None`` if it cannot be trusted.

    Built only from evidence already stored: the check's kind and generated name and the
    paired ``command.started``'s recorded executable, recorded (redacted) argv and recorded
    project-relative working directory. The original process argv is never involved.
    ``None`` when the recorded argv does not faithfully identify the invocation: truncated,
    redacted, or an argument (after the program itself) rewritten to ``<external-path>``,
    because two different invocations could then look identical.
    """
    payload = started.payload
    argv = payload.get("argv")
    executable = _str(payload, "executable")
    working_directory = _str(payload, "working_directory")
    count = _int(payload, "argument_count")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
        or executable is None
        or working_directory is None
        or count is None
        or count != len(argv) - 1
        or _bool(payload, "argv_truncated") is not False
        or _bool(payload, "argv_redacted") is not False
    ):
        return None
    items = [item for item in argv if isinstance(item, str)]
    if any(EXTERNAL_PATH in item or REDACTED in item for item in items[1:]):
        return None
    if REDACTED in items[0]:
        return None
    basis: JsonObject = {
        "version": SIGNATURE_VERSION,
        "kind": kind,
        "name": name,
        "executable": executable,
        "argv": list(items),
        "working_directory": working_directory,
    }
    return hashlib.sha256(dumps_canonical(basis).encode("utf-8")).hexdigest()


# -- file-change windows -------------------------------------------------------------------


@dataclass(slots=True)
class _FileIndex:
    """Sequence-sorted file events with prefix counts per change kind."""

    sequences: list[int] = field(default_factory=list)
    refs: list[FileChangeRef] = field(default_factory=list)
    created: list[int] = field(default_factory=lambda: [0])
    modified: list[int] = field(default_factory=lambda: [0])
    deleted: list[int] = field(default_factory=lambda: [0])

    def add(self, event: Event, change: str) -> None:
        self.sequences.append(event.sequence)
        self.refs.append(FileChangeRef(event.sequence, event.event_id, change, event.path))
        self.created.append(self.created[-1] + (change == "created"))
        self.modified.append(self.modified[-1] + (change == "modified"))
        self.deleted.append(self.deleted[-1] + (change == "deleted"))

    def window(self, after: int | None, before: int | None, description: str) -> ChangeWindow:
        lo = 0 if after is None else bisect.bisect_right(self.sequences, after)
        hi = len(self.sequences) if before is None else bisect.bisect_left(self.sequences, before)
        hi = max(hi, lo)
        shown = tuple(self.refs[lo : min(hi, lo + MAX_WINDOW_CHANGES)])
        count = hi - lo
        return ChangeWindow(
            after_sequence=after,
            before_sequence=before,
            description=f"{description}: {count}.",
            file_change_count=count,
            created=self.created[hi] - self.created[lo],
            modified=self.modified[hi] - self.modified[lo],
            deleted=self.deleted[hi] - self.deleted[lo],
            file_changes=shown,
            file_changes_truncated=count > len(shown),
        )


# -- command / check indexes ---------------------------------------------------------------


@dataclass(slots=True)
class _CheckRun:
    event: Event
    passed: bool
    kind: str
    name: str
    command_id: str
    started: Event
    finished: Event
    signature: str | None

    @property
    def evidence(self) -> list[str]:
        return [self.started.event_id, self.finished.event_id, self.event.event_id]


@dataclass(slots=True)
class _Commands:
    started: dict[str, Event] = field(default_factory=dict)
    finished: dict[str, Event] = field(default_factory=dict)
    checks: dict[str, Event] = field(default_factory=dict)
    check_order: list[tuple[Event, str]] = field(default_factory=list)
    tainted: set[str] = field(default_factory=set)
    warnings: list[ForensicWarning] = field(default_factory=list)
    started_count: int = 0
    finished_count: int = 0

    def warn(
        self,
        code: str,
        message: str,
        events: Iterable[Event],
        severity: WarningSeverity = WarningSeverity.WARNING,
    ) -> None:
        ids, _ = _bounded_ids([e.event_id for e in events], MAX_WARNING_EVENT_IDS)
        self.warnings.append(ForensicWarning(code, severity, message, ids))

    def taint(self, command_id: str, code: str, message: str, events: Iterable[Event]) -> None:
        self.tainted.add(command_id)
        self.warn(code, f"{message}; command {command_id} is excluded from findings", events)

    def add_started(self, event: Event) -> None:
        self.started_count += 1
        command_id = _str(event.payload, "command_id")
        if command_id is None:
            self.warn("invalid_command_event", "command.started has no command_id", [event])
            return
        previous = self.started.get(command_id)
        if previous is not None:
            self.taint(
                command_id,
                "duplicate_command_started",
                f"command.started recorded twice for one command_id (#{previous.sequence}, "
                f"#{event.sequence})",
                [previous, event],
            )
            return
        later = self.finished.get(command_id) or self.checks.get(command_id)
        if later is not None:
            self.taint(
                command_id,
                "command_started_after_result",
                f"command.started (#{event.sequence}) follows a finish or check for the same "
                f"command (#{later.sequence})",
                [later, event],
            )
        self.started[command_id] = event

    def add_finished(self, event: Event) -> None:
        self.finished_count += 1
        command_id = _str(event.payload, "command_id")
        if command_id is None:
            self.warn("invalid_command_event", "command.finished has no command_id", [event])
            return
        if command_id not in self.started:
            self.taint(
                command_id,
                "command_finished_without_start",
                f"command.finished (#{event.sequence}) has no earlier command.started",
                [event],
            )
        previous = self.finished.get(command_id)
        if previous is not None:
            self.taint(
                command_id,
                "duplicate_command_finished",
                f"command.finished recorded twice for one command_id (#{previous.sequence}, "
                f"#{event.sequence})",
                [previous, event],
            )
            return
        self.finished[command_id] = event

    def add_check(self, event: Event) -> None:
        command_id = _str(event.payload, "command_id")
        if command_id is None:
            self.warn(
                "check_without_command",
                f"{event.type.value} (#{event.sequence}) has no command_id; excluded from findings",
                [event],
            )
            return
        if command_id not in self.started:
            self.taint(
                command_id,
                "check_without_command_start",
                f"{event.type.value} (#{event.sequence}) has no earlier command.started",
                [event],
            )
        elif command_id not in self.finished:
            self.taint(
                command_id,
                "check_before_command_finished",
                f"{event.type.value} (#{event.sequence}) precedes its command.finished",
                [self.started[command_id], event],
            )
        previous = self.checks.get(command_id)
        if previous is not None:
            self.taint(
                command_id,
                "duplicate_check",
                f"two check events for one command (#{previous.sequence}, #{event.sequence})",
                [previous, event],
            )
            return
        self.checks[command_id] = event
        self.check_order.append((event, command_id))

    def check_runs(self) -> list[_CheckRun]:
        """Valid check runs in sequence order; contradictory ones become warnings."""
        runs: list[_CheckRun] = []
        for event, command_id in self.check_order:
            if command_id in self.tainted:
                continue
            started, finished = self.started[command_id], self.finished[command_id]
            payload = event.payload
            kind, name = _str(payload, "kind"), _str(payload, "name")
            passed = event.type is EventType.CHECK_PASSED
            if kind not in _CHECK_KINDS or name is None or kind is None:
                self.taint(
                    command_id,
                    "invalid_check_event",
                    f"{event.type.value} (#{event.sequence}) has no valid kind/name",
                    [event],
                )
                continue
            if _str(payload, "result") not in (None, "passed" if passed else "failed"):
                self.taint(
                    command_id,
                    "check_result_mismatch",
                    f"{event.type.value} (#{event.sequence}) payload result disagrees with "
                    "its type",
                    [event],
                )
                continue
            if not _consistent(passed, payload, finished.payload):
                self.taint(
                    command_id,
                    "check_contradicts_command_finished",
                    f"{event.type.value} (#{event.sequence}) contradicts command.finished "
                    f"(#{finished.sequence})",
                    [finished, event],
                )
                continue
            runs.append(
                _CheckRun(
                    event=event,
                    passed=passed,
                    kind=kind,
                    name=name,
                    command_id=command_id,
                    started=started,
                    finished=finished,
                    signature=check_signature(kind, name, started),
                )
            )
        return runs


def _consistent(passed: bool, check: JsonObject, finished: JsonObject) -> bool:
    outcome = _str(finished, "outcome")
    if outcome is None:
        return False
    try:
        expected, _ = judge(outcome, _int(finished, "exit_code"))
    except ValueError:
        return False
    if expected != passed:
        return False
    return "exit_code" not in check or check.get("exit_code") == finished.get("exit_code")


# -- report assembly -----------------------------------------------------------------------


def _validate_input(session: Session, events: Sequence[Event]) -> None:
    seen: set[str] = set()
    for expected, event in enumerate(events, start=1):
        if event.session_id != session.session_id:
            raise ForensicInputError(
                f"event #{event.sequence} belongs to session {event.session_id!r}"
            )
        if event.sequence != expected:
            raise ForensicInputError(
                f"events must be the complete log in sequence order: expected #{expected}, "
                f"got #{event.sequence}"
            )
        if event.event_id in seen:
            raise ForensicInputError(f"duplicate event_id {event.event_id}")
        seen.add(event.event_id)


def timeline_item(event: Event) -> TimelineItem:
    """The safe, replay-facing view of one event: no payload is copied."""
    command_id = None
    if event.type in (
        EventType.COMMAND_STARTED,
        EventType.COMMAND_FINISHED,
        EventType.CHECK_PASSED,
        EventType.CHECK_FAILED,
    ):
        command_id = _str(event.payload, "command_id")
    return TimelineItem(
        sequence=event.sequence,
        event_id=event.event_id,
        timestamp=format_timestamp(event.timestamp),
        type=event.type.value,
        category=event.category.value,
        source=event.source.value,
        summary=event.summary,
        path=event.path,
        command_id=command_id,
        evidence=event.evidence.value,
    )


def _git_summary(event: Event, count: int) -> GitStateSummary:
    payload = event.payload
    repository = payload.get("repository") is True
    head = payload.get("head")
    head_obj: dict[str, JsonValue] = head if isinstance(head, dict) else {}
    counts = payload.get("counts")
    counts_obj: dict[str, JsonValue] = counts if isinstance(counts, dict) else {}
    upstream = payload.get("upstream")
    upstream_obj: dict[str, JsonValue] = upstream if isinstance(upstream, dict) else {}
    commit = _str(head_obj, "commit")
    state = _str(payload, "state")
    return GitStateSummary(
        description=GIT_DESCRIPTION,
        event_id=event.event_id,
        sequence=event.sequence,
        snapshot_count=count,
        repository=repository,
        unavailable_reason=None if repository else _str(payload, "reason"),
        branch=_str(head_obj, "branch") if repository else None,
        detached=_bool(head_obj, "detached") if repository else None,
        unborn=_bool(head_obj, "unborn") if repository else None,
        head=commit[:7] if repository and commit is not None else None,
        state=state if repository and state in ("clean", "dirty") else None,
        staged=_int(counts_obj, "staged") if repository else None,
        unstaged=_int(counts_obj, "unstaged") if repository else None,
        untracked=_int(counts_obj, "untracked") if repository else None,
        conflicted=_int(counts_obj, "conflicted") if repository else None,
        upstream=_str(upstream_obj, "name") if repository else None,
        ahead=_int(upstream_obj, "ahead") if repository else None,
        behind=_int(upstream_obj, "behind") if repository else None,
        summary=event.summary,
    )


def _error(event: Event) -> ErrorObserved:
    return ErrorObserved(
        sequence=event.sequence,
        event_id=event.event_id,
        source=event.source.value,
        operation=_str(event.payload, "operation"),
        error=_str(event.payload, "error"),
        path=event.path,
        summary=event.summary,
    )


@dataclass(slots=True)
class _PathState:
    count: int
    first: str
    last: str
    last_event: Event


def _file_summary(
    paths: dict[str, _PathState], created: int, modified: int, deleted: int
) -> FileChangeSummary:
    observations = [
        PathObservation(
            path=path,
            change_events=state.count,
            first_change=state.first,
            last_change=state.last,
            last_sequence=state.last_event.sequence,
            last_event_id=state.last_event.event_id,
            present_at_last_observation=state.last != "deleted",
        )
        for path, state in sorted(paths.items())
    ]
    return FileChangeSummary(
        description=FILE_SUMMARY_DESCRIPTION,
        created_events=created,
        modified_events=modified,
        deleted_events=deleted,
        total_events=created + modified + deleted,
        unique_paths=len(paths),
        paths=bounded(observations, MAX_PATHS),
    )


def _check_failure(run: _CheckRun) -> FailureFinding:
    return FailureFinding(
        event_id=run.event.event_id,
        sequence=run.event.sequence,
        failure_type=FailureType.CHECK_FAILED,
        summary=run.event.summary,
        command_id=run.command_id,
        check_kind=run.kind,
        check_name=run.name,
        check_signature=run.signature,
        exit_code=_int(run.event.payload, "exit_code"),
        reason=_str(run.event.payload, "reason"),
        evidence_event_ids=tuple(run.evidence),
    )


def _operational_failures(commands: _Commands, valid_checks: set[str]) -> list[FailureFinding]:
    failures: list[FailureFinding] = []
    for command_id, finished in commands.finished.items():
        if command_id in commands.tainted or command_id in valid_checks:
            continue
        if command_id in commands.checks:
            continue  # a check exists but was excluded; its warning covers this command
        outcome = _str(finished.payload, "outcome")
        failure_type = _OPERATIONAL_FAILURES.get(outcome or "")
        if failure_type is None:
            continue
        started = commands.started[command_id]
        failures.append(
            FailureFinding(
                event_id=finished.event_id,
                sequence=finished.sequence,
                failure_type=failure_type,
                summary=finished.summary,
                command_id=command_id,
                check_kind=None,
                check_name=None,
                check_signature=None,
                exit_code=_int(finished.payload, "exit_code"),
                reason=outcome,
                evidence_event_ids=(started.event_id, finished.event_id),
            )
        )
    return failures


def _incomplete(commands: _Commands, live: bool) -> list[IncompleteCommand]:
    note = "Command started but no finish event was recorded."
    if live:
        note += " The recording is still live; the command may still be running."
    result: list[IncompleteCommand] = []
    for command_id, started in commands.started.items():
        if command_id in commands.finished or command_id in commands.tainted:
            continue
        result.append(
            IncompleteCommand(
                command_id=command_id,
                started_event_id=started.event_id,
                started_sequence=started.sequence,
                executable=_str(started.payload, "executable"),
                working_directory=_str(started.payload, "working_directory"),
                command_summary=started.summary,
                summary=note,
                evidence_event_ids=(started.event_id,),
            )
        )
    return result


@dataclass(slots=True)
class _Series:
    """Results of one check signature, analysed into failure/recovery periods."""

    recoveries: list[Recovery] = field(default_factory=list)
    unresolved: UnresolvedFailure | None = None


def _display_names(runs: Sequence[_CheckRun]) -> dict[str, str]:
    by_name: dict[tuple[str, str], list[str]] = {}
    for run in runs:
        if run.signature is not None:
            sigs = by_name.setdefault((run.kind, run.name), [])
            if run.signature not in sigs:
                sigs.append(run.signature)
    names: dict[str, str] = {}
    for (_, name), sigs in by_name.items():
        for sig in sigs:
            names[sig] = name if len(sigs) == 1 else f"{name} [{sig[:SHORT_SIGNATURE_CHARS]}]"
    return names


def _analyse_series(
    signature: str, display: str, runs: Sequence[_CheckRun], files: _FileIndex
) -> _Series:
    series = _Series()
    last_pass: _CheckRun | None = None
    failing: list[_CheckRun] = []

    def before_failure(first: _CheckRun) -> ChangeWindow | None:
        if last_pass is None:
            return None
        return files.window(
            last_pass.event.sequence,
            first.event.sequence,
            f"File changes observed after the last recorded passing check "
            f"(#{last_pass.event.sequence}) and before the first recorded failing check "
            f"(#{first.event.sequence})",
        )

    def evidence(extra: Sequence[_CheckRun]) -> tuple[tuple[str, ...], bool]:
        ids: list[str] = []
        if last_pass is not None:
            ids.append(last_pass.event.event_id)
        for run in [*failing, *extra]:
            ids += run.evidence
        return _bounded_ids(ids)

    for run in runs:
        if not run.passed:
            failing.append(run)
            continue
        if failing:
            first, last = failing[0], failing[-1]
            ids, truncated = evidence([run])
            n = len(failing)
            seq_first, seq_last, seq_pass = (
                first.event.sequence,
                last.event.sequence,
                run.event.sequence,
            )
            series.recoveries.append(
                Recovery(
                    check_signature=signature,
                    kind=run.kind,
                    name=run.name,
                    display_name=display,
                    first_failed_event_id=first.event.event_id,
                    first_failed_sequence=seq_first,
                    last_failed_event_id=last.event.event_id,
                    last_failed_sequence=seq_last,
                    recovered_event_id=run.event.event_id,
                    recovered_sequence=seq_pass,
                    failed_attempts=n,
                    retry_count=n - 1,
                    previous_pass_event_id=None if last_pass is None else last_pass.event.event_id,
                    previous_pass_sequence=None if last_pass is None else last_pass.event.sequence,
                    previous_pass_note=NO_EARLIER_PASSING_RESULT if last_pass is None else None,
                    changes_before_failure=before_failure(first),
                    changes_between_failure_and_pass=files.window(
                        seq_first,
                        seq_pass,
                        f"File changes observed between the failed check (#{seq_first}) and "
                        f"the later passing check (#{seq_pass})",
                    ),
                    changes_after_last_failure=files.window(
                        seq_last,
                        seq_pass,
                        f"File changes observed between the last recorded failing check "
                        f"(#{seq_last}) and the later passing check (#{seq_pass})",
                    ),
                    summary=(
                        f"{display} failed at event #{seq_first} and later passed at event "
                        f"#{seq_pass} after {_plural(n, 'additional recorded attempt')}."
                    ),
                    evidence_event_ids=ids,
                    evidence_event_ids_truncated=truncated,
                )
            )
            failing = []
        last_pass = run

    if failing:
        first, last = failing[0], failing[-1]
        ids, truncated = evidence([])
        n = len(failing)
        text = f"Latest recorded {display} result: FAILED (unresolved in this recording)."
        if n > 1:
            text += f" {display} remained failing across {n} recorded attempts."
        series.unresolved = UnresolvedFailure(
            check_signature=signature,
            kind=last.kind,
            name=last.name,
            display_name=display,
            first_failed_event_id=first.event.event_id,
            first_failed_sequence=first.event.sequence,
            last_failed_event_id=last.event.event_id,
            last_failed_sequence=last.event.sequence,
            failed_attempts=n,
            retry_count=n - 1,
            previous_pass_event_id=None if last_pass is None else last_pass.event.event_id,
            previous_pass_sequence=None if last_pass is None else last_pass.event.sequence,
            previous_pass_note=NO_EARLIER_PASSING_RESULT if last_pass is None else None,
            changes_before_failure=before_failure(first),
            changes_after_last_failure=files.window(
                last.event.sequence,
                None,
                f"File changes observed after the latest failing check "
                f"(#{last.event.sequence}) until the end of the recorded evidence",
            ),
            summary=text,
            evidence_event_ids=ids,
            evidence_event_ids_truncated=truncated,
        )
    return series


def _check_state(runs: Sequence[_CheckRun], signature: str | None, display: str) -> CheckState:
    latest = runs[-1]
    passed = sum(run.passed for run in runs)
    result = "passed" if latest.passed else "failed"
    return CheckState(
        check_signature=signature,
        identity_established=signature is not None,
        kind=latest.kind,
        name=latest.name,
        display_name=display,
        working_directory=_str(latest.started.payload, "working_directory"),
        latest_result=result,
        latest_event_id=latest.event.event_id,
        latest_sequence=latest.event.sequence,
        attempts=len(runs),
        passed=passed,
        failed=len(runs) - passed,
        summary=(
            f"Latest recorded {display} result: {result.upper()} (event #{latest.event.sequence})."
        ),
    )


def _recording_state(
    session: Session, events: Sequence[Event], recorder_live: bool | None
) -> tuple[RecordingState, bool, list[ForensicWarning]]:
    notes: list[ForensicWarning] = []
    last = events[-1].sequence if events else 0
    stopped = next((e for e in events if e.type is EventType.SESSION_STOPPED), None)
    if stopped is not None:
        if session.status is not SessionStatus.STOPPED:
            notes.append(
                ForensicWarning(
                    "stale_session_status",
                    WarningSeverity.INFO,
                    f"session.stopped was recorded (#{stopped.sequence}) but the session "
                    f"metadata still says {session.status.value!r}.",
                    (stopped.event_id,),
                )
            )
        return RecordingState.STOPPED, True, notes
    if session.status is SessionStatus.STOPPED:
        notes.append(
            ForensicWarning(
                "no_session_stopped_event",
                WarningSeverity.INFO,
                "The session metadata says 'stopped' but no session.stopped event was recorded.",
                (),
            )
        )
        return RecordingState.STOPPED, True, notes
    if session.status is SessionStatus.CREATED:
        notes.append(
            ForensicWarning(
                "recording_not_started",
                WarningSeverity.INFO,
                "The session was created but never marked as recording.",
                (),
            )
        )
        return RecordingState.NOT_STARTED, False, notes
    if recorder_live:
        notes.append(
            ForensicWarning(
                "recording_in_progress",
                WarningSeverity.INFO,
                f"The session is being recorded right now; this analysis covers the evidence "
                f"read so far (up to event #{last}).",
                (),
            )
        )
        return RecordingState.RECORDING, False, notes
    if recorder_live is None:
        notes.append(
            ForensicWarning(
                "recording_state_unknown",
                WarningSeverity.INFO,
                "The session metadata says 'recording' and it could not be established "
                f"whether a recorder is still running; evidence read up to event #{last}.",
                (),
            )
        )
        return RecordingState.UNKNOWN, False, notes
    notes.append(
        ForensicWarning(
            "recording_ended_without_stop",
            WarningSeverity.INFO,
            "The session metadata still says 'recording', but no live recorder holds this "
            "project's recording lock and no session.stopped event was recorded: the "
            f"recording ended without a clean stop. The evidence ends at event #{last}.",
            (),
        )
    )
    return RecordingState.ENDED_WITHOUT_STOP, True, notes


def analyze_events(
    session: Session,
    events: Sequence[Event],
    *,
    recorder_live: bool | None = False,
    max_timeline_items: int = DEFAULT_MAX_TIMELINE_ITEMS,
) -> ForensicReport:
    """Pure forensic analysis of one session's complete, sequence-ordered event log.

    ``recorder_live`` says whether a recorder is writing the session right now (only
    consulted while the session status is ``recording``; ``None`` = unknown).
    """
    _validate_input(session, events)
    files = _FileIndex()
    paths: dict[str, _PathState] = {}
    commands = _Commands()
    errors: list[ErrorObserved] = []
    timeline: list[TimelineItem] = []
    counts: dict[str, int] = {}
    last_git: Event | None = None
    git_count = 0
    first_start: Event | None = None

    for event in events:
        counts[event.type.value] = counts.get(event.type.value, 0) + 1
        if len(timeline) < max_timeline_items:
            timeline.append(timeline_item(event))
        kind = event.type
        change = _FILE_CHANGES.get(kind)
        if change is not None:
            files.add(event, change)
            if event.path is not None:
                seen = paths.get(event.path)
                if seen is None:
                    paths[event.path] = _PathState(1, change, change, event)
                else:
                    seen.count += 1
                    seen.last = change
                    seen.last_event = event
        elif kind is EventType.COMMAND_STARTED:
            commands.add_started(event)
        elif kind is EventType.COMMAND_FINISHED:
            commands.add_finished(event)
        elif kind in (EventType.CHECK_PASSED, EventType.CHECK_FAILED):
            commands.add_check(event)
        elif kind is EventType.GIT_SNAPSHOT:
            last_git = event
            git_count += 1
        elif kind is EventType.ERROR_OBSERVED:
            errors.append(_error(event))
        elif kind is EventType.SESSION_STARTED:
            if first_start is None:
                first_start = event
            else:
                commands.warn(
                    "multiple_session_started",
                    f"session.started was recorded again at #{event.sequence}",
                    [first_start, event],
                )

    runs = commands.check_runs()
    names = _display_names(runs)
    by_signature: dict[str, list[_CheckRun]] = {}
    ungrouped: list[_CheckRun] = []
    for run in runs:
        if run.signature is None:
            ungrouped.append(run)
        else:
            by_signature.setdefault(run.signature, []).append(run)

    recoveries: list[Recovery] = []
    unresolved: list[UnresolvedFailure] = []
    states: list[tuple[int, CheckState]] = []
    for signature, series_runs in by_signature.items():
        display = names[signature]
        series = _analyse_series(signature, display, series_runs, files)
        recoveries += series.recoveries
        if series.unresolved is not None:
            unresolved.append(series.unresolved)
        states.append(
            (series_runs[0].event.sequence, _check_state(series_runs, signature, display))
        )
    for run in ungrouped:
        display = f"{run.name} (ungrouped, event #{run.event.sequence})"
        states.append((run.event.sequence, _check_state([run], None, display)))
    recoveries.sort(key=lambda r: r.recovered_sequence)
    unresolved.sort(key=lambda u: u.first_failed_sequence)
    states.sort(key=lambda item: item[0])

    if ungrouped:
        commands.warn(
            "ungrouped_checks",
            f"{_plural(len(ungrouped), 'check result')} could not be matched to other runs of "
            "the same check because the recorded command line was truncated, redacted or "
            "contained rewritten external paths; they take no part in retry, recovery or "
            "unresolved-failure findings.",
            [run.event for run in ungrouped],
            WarningSeverity.INFO,
        )

    valid_check_commands = {run.command_id for run in runs}
    failures = [_check_failure(run) for run in runs if not run.passed]
    failures += _operational_failures(commands, valid_check_commands)
    failures.sort(key=lambda f: f.sequence)

    state, complete, notes = _recording_state(session, events, recorder_live)
    live = state is RecordingState.RECORDING
    incomplete = _incomplete(commands, live)
    warnings = notes + commands.warnings

    passed = sum(run.passed for run in runs)
    return ForensicReport(
        schema_version=FORENSIC_REPORT_SCHEMA_VERSION,
        session_id=session.session_id,
        session_status=session.status.value,
        recording_state=state,
        analysis_complete=complete,
        started_at=format_timestamp(session.started_at),
        ended_at=None if session.ended_at is None else format_timestamp(session.ended_at),
        event_count=len(events),
        event_counts=tuple(sorted(counts.items())),
        finding_evidence=OBSERVED,
        timeline=Bounded(tuple(timeline), len(events)),
        first_observable_failure=failures[0] if failures else None,
        observable_failures=bounded(failures, MAX_FINDINGS),
        recoveries=bounded(recoveries, MAX_FINDINGS),
        unresolved_failures=bounded(unresolved, MAX_FINDINGS),
        final_check_states=bounded([s for _, s in states], MAX_FINDINGS),
        incomplete_commands=bounded(incomplete, MAX_FINDINGS),
        command_summary=CommandSummary(
            commands_started=commands.started_count,
            commands_finished=commands.finished_count,
            incomplete_commands=len(incomplete),
            checks_passed=passed,
            checks_failed=len(runs) - passed,
            checks_ungrouped=len(ungrouped),
            check_signatures=len(by_signature),
        ),
        file_change_summary=_file_summary(
            paths, files.created[-1], files.modified[-1], files.deleted[-1]
        ),
        latest_git_state=None if last_git is None else _git_summary(last_git, git_count),
        errors_observed=bounded(errors, MAX_FINDINGS),
        warnings=bounded(warnings, MAX_FINDINGS),
    )


def recorder_liveness(store: EventStore, session: Session, probe: ActiveProbe) -> bool | None:
    """Is a live recorder writing ``session`` right now? ``None`` if it cannot be told.

    Uses the read-only active-session discovery (the OS-held recording lock); creates and
    modifies nothing. Only meaningful while the session status is ``recording``.
    """
    if session.status is not SessionStatus.RECORDING:
        return False
    try:
        info = probe(store.project_root)
    except (StoreError, EventValidationError, OSError):
        return None
    if info.state is ActiveState.ACTIVE:
        record = info.record
        return record is not None and record.session_id == session.session_id
    if info.state is ActiveState.INVALID:
        return None
    return False


def analyze_session(
    store: EventStore,
    session_id: str,
    *,
    probe: ActiveProbe = get_active_session,
    max_timeline_items: int = DEFAULT_MAX_TIMELINE_ITEMS,
) -> ForensicReport:
    """Forensic report for a stored session. Read-only: writes nothing anywhere."""
    session = store.load_session(session_id)
    events = store.read_events(session_id)
    return analyze_events(
        session,
        events,
        recorder_live=recorder_liveness(store, session, probe),
        max_timeline_items=max_timeline_items,
    )
