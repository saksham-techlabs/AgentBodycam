"""Typed, JSON-serializable results of the local forensics engine.

Every finding here is *observed*: a deterministic statement about recorded events and their
order (``sequence``), never about causes. Findings point at the events they come from
through event IDs and sequence numbers. Nothing in a report is random, time-of-analysis
dependent or read from the current file system, so identical session bytes produce an
identical report (apart from ``recording_state`` / ``analysis_complete`` for a session whose
status is still ``recording``, which depends on whether its recorder is alive).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from agentbodycam.models.events import EvidenceKind, JsonObject, JsonValue

FORENSIC_REPORT_SCHEMA_VERSION = 1
OBSERVED = EvidenceKind.OBSERVED.value


class FailureType(StrEnum):
    CHECK_FAILED = "check_failed"
    COMMAND_SPAWN_ERROR = "command_spawn_error"
    COMMAND_SIGNALED = "command_signaled"
    COMMAND_INTERRUPTED = "command_interrupted"


class RecordingState(StrEnum):
    STOPPED = "stopped"  # the log contains session.stopped (or the metadata says stopped)
    RECORDING = "recording"  # a live recorder is writing this session right now
    ENDED_WITHOUT_STOP = "ended_without_stop"  # status recording, but no live recorder
    UNKNOWN = "unknown"  # status recording; liveness could not be established
    NOT_STARTED = "not_started"  # status created: no recording was ever started


class WarningSeverity(StrEnum):
    INFO = "info"  # evidence-quality note; findings are still sound
    WARNING = "warning"  # contradictory evidence was excluded from findings


class _ToDict(Protocol):
    def to_dict(self) -> JsonObject: ...


T = TypeVar("T", bound=_ToDict)


def _json(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result: JsonValue = to_dict()
        return result
    raise TypeError(f"not serializable: {type(value).__name__}")


class _Serializable:
    __slots__ = ()

    def to_dict(self) -> JsonObject:
        return {
            field.name: _json(getattr(self, field.name))
            for field in dataclasses.fields(self)  # type: ignore[arg-type]
        }


@dataclass(frozen=True, slots=True)
class Bounded(Generic[T]):
    """A list capped for size; ``total`` always counts every item, shown or not."""

    items: tuple[T, ...]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)

    def to_dict(self) -> JsonObject:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "truncated": self.truncated,
        }


def bounded(items: Sequence[T], limit: int) -> Bounded[T]:
    return Bounded(tuple(items[:limit]), len(items))


@dataclass(frozen=True, slots=True)
class TimelineItem(_Serializable):
    sequence: int
    event_id: str
    timestamp: str  # display only; sequence is the authoritative order
    type: str
    category: str
    source: str
    summary: str
    path: str | None
    command_id: str | None
    evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class FileChangeRef(_Serializable):
    sequence: int
    event_id: str
    change: str  # created | modified | deleted
    path: str | None


@dataclass(frozen=True, slots=True)
class ChangeWindow(_Serializable):
    """File events strictly between two sequence numbers (``None``: open-ended)."""

    after_sequence: int | None
    before_sequence: int | None
    description: str
    file_change_count: int
    created: int
    modified: int
    deleted: int
    file_changes: tuple[FileChangeRef, ...]
    file_changes_truncated: bool


@dataclass(frozen=True, slots=True)
class FailureFinding(_Serializable):
    event_id: str
    sequence: int
    failure_type: FailureType
    summary: str
    command_id: str | None
    check_kind: str | None
    check_name: str | None
    check_signature: str | None
    exit_code: int | None
    reason: str | None
    evidence_event_ids: tuple[str, ...]
    finding_evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class Recovery(_Serializable):
    """A check signature failed, then a later run of the same signature passed."""

    check_signature: str
    kind: str
    name: str
    display_name: str
    first_failed_event_id: str
    first_failed_sequence: int
    last_failed_event_id: str
    last_failed_sequence: int
    recovered_event_id: str
    recovered_sequence: int
    failed_attempts: int
    retry_count: int
    previous_pass_event_id: str | None
    previous_pass_sequence: int | None
    previous_pass_note: str | None
    changes_before_failure: ChangeWindow | None
    changes_between_failure_and_pass: ChangeWindow
    changes_after_last_failure: ChangeWindow
    summary: str
    evidence_event_ids: tuple[str, ...]
    evidence_event_ids_truncated: bool
    finding_evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class UnresolvedFailure(_Serializable):
    """The latest recorded result of a check signature is a failure."""

    check_signature: str
    kind: str
    name: str
    display_name: str
    first_failed_event_id: str
    first_failed_sequence: int
    last_failed_event_id: str
    last_failed_sequence: int
    failed_attempts: int
    retry_count: int
    previous_pass_event_id: str | None
    previous_pass_sequence: int | None
    previous_pass_note: str | None
    changes_before_failure: ChangeWindow | None
    changes_after_last_failure: ChangeWindow
    summary: str
    evidence_event_ids: tuple[str, ...]
    evidence_event_ids_truncated: bool
    finding_evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class CheckState(_Serializable):
    """The last observed result of one check signature (or one ungrouped check run)."""

    check_signature: str | None
    identity_established: bool
    kind: str
    name: str
    display_name: str
    working_directory: str | None
    latest_result: str  # passed | failed
    latest_event_id: str
    latest_sequence: int
    attempts: int
    passed: int
    failed: int
    summary: str
    finding_evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class IncompleteCommand(_Serializable):
    command_id: str
    started_event_id: str
    started_sequence: int
    executable: str | None
    working_directory: str | None
    command_summary: str
    summary: str
    evidence_event_ids: tuple[str, ...]
    finding_evidence: str = OBSERVED


@dataclass(frozen=True, slots=True)
class PathObservation(_Serializable):
    path: str
    change_events: int
    first_change: str
    last_change: str
    last_sequence: int
    last_event_id: str
    present_at_last_observation: bool


@dataclass(frozen=True, slots=True)
class FileChangeSummary(_Serializable):
    description: str
    created_events: int
    modified_events: int
    deleted_events: int
    total_events: int
    unique_paths: int
    paths: Bounded[PathObservation]


@dataclass(frozen=True, slots=True)
class GitStateSummary(_Serializable):
    description: str
    event_id: str
    sequence: int
    snapshot_count: int
    repository: bool
    unavailable_reason: str | None
    branch: str | None
    detached: bool | None
    unborn: bool | None
    head: str | None  # abbreviated SHA
    state: str | None  # clean | dirty
    staged: int | None
    unstaged: int | None
    untracked: int | None
    conflicted: int | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    summary: str


@dataclass(frozen=True, slots=True)
class ErrorObserved(_Serializable):
    sequence: int
    event_id: str
    source: str
    operation: str | None
    error: str | None
    path: str | None
    summary: str


@dataclass(frozen=True, slots=True)
class ForensicWarning(_Serializable):
    code: str
    severity: WarningSeverity
    message: str
    event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandSummary(_Serializable):
    commands_started: int
    commands_finished: int
    incomplete_commands: int
    checks_passed: int
    checks_failed: int
    checks_ungrouped: int
    check_signatures: int


@dataclass(frozen=True, slots=True)
class ForensicReport(_Serializable):
    schema_version: int
    session_id: str
    session_status: str
    recording_state: RecordingState
    analysis_complete: bool
    started_at: str
    ended_at: str | None
    event_count: int
    event_counts: tuple[tuple[str, int], ...]
    finding_evidence: str
    timeline: Bounded[TimelineItem]
    first_observable_failure: FailureFinding | None
    observable_failures: Bounded[FailureFinding]
    recoveries: Bounded[Recovery]
    unresolved_failures: Bounded[UnresolvedFailure]
    final_check_states: Bounded[CheckState]
    incomplete_commands: Bounded[IncompleteCommand]
    command_summary: CommandSummary
    file_change_summary: FileChangeSummary
    latest_git_state: GitStateSummary | None
    errors_observed: Bounded[ErrorObserved]
    warnings: Bounded[ForensicWarning]

    def to_dict(self) -> JsonObject:
        data = _Serializable.to_dict(self)
        data["event_counts"] = dict(self.event_counts)
        return data

    def to_json(self) -> str:
        """Deterministic JSON: sorted keys, ASCII only (safe for any terminal)."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=True)
