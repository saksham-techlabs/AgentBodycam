"""Builders for forensic tests: precise sessions written through the real event store.

Command and check events are produced with the real producers in ``core.command`` so their
payloads are exactly what ``agentbodycam run`` records.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentbodycam.core.active import ActiveRecord, ActiveSessionInfo, ActiveState
from agentbodycam.core.checks import CheckClassification, CheckKind, ClassificationSource
from agentbodycam.core.command import (
    CommandResult,
    Outcome,
    check_draft,
    finished_draft,
    started_draft,
)
from agentbodycam.core.redact import redact_command
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import (
    CATEGORY_BY_TYPE,
    Event,
    EventDraft,
    EventSource,
    EventType,
    EvidenceKind,
    JsonObject,
)

PYTEST = ["python", "-m", "pytest"]
CAUSAL_LANGUAGE = re.compile(
    r"\b(caus(e|ed|es|ing)|fix(ed|es|ing)?|broke|broken|root[ -]cause|responsible|"
    r"introduced|culprit|suspects?|blame[ds]?|regression caused)\b",
    re.IGNORECASE,
)


class SessionBuilder:
    """Appends events one by one; methods return the appended events."""

    def __init__(self, store: EventStore, *, start: bool = True) -> None:
        self.store = store
        self.session_id = store.create_session().session_id
        if start:
            store.mark_recording(self.session_id)
            self._append(EventType.SESSION_STARTED, EventSource.AGENTBODYCAM, "Recording started")

    def _append(
        self,
        type_: EventType,
        source: EventSource,
        summary: str,
        *,
        path: str | None = None,
        payload: JsonObject | None = None,
    ) -> Event:
        draft = EventDraft(
            type=type_, source=source, summary=summary, path=path, payload=payload or {}
        )
        return self.store.append_event(self.session_id, draft)

    # -- files / git / errors --------------------------------------------------------------

    def file(self, change: str, path: str) -> Event:
        type_ = {
            "created": EventType.FILE_CREATED,
            "modified": EventType.FILE_MODIFIED,
            "deleted": EventType.FILE_DELETED,
        }[change]
        return self._append(
            type_,
            EventSource.FILESYSTEM,
            f"File {change}: {path}",
            path=path,
            payload={"detected_by": "polling", "scan": 1},
        )

    def git(
        self,
        *,
        branch: str = "main",
        commit: str = "a" * 40,
        state: str = "clean",
        counts: tuple[int, int, int, int] = (0, 0, 0, 0),
    ) -> Event:
        staged, unstaged, untracked, conflicted = counts
        return self._append(
            EventType.GIT_SNAPSHOT,
            EventSource.GIT,
            f"Git repository: on branch {branch} at {commit[:7]}, working tree {state}",
            payload={
                "repository": True,
                "root": "C:\\Users\\someone\\private-root",
                "project_prefix": "",
                "worktree": "main",
                "head": {"commit": commit, "branch": branch, "detached": False, "unborn": False},
                "upstream": None,
                "state": state,
                "counts": {
                    "staged": staged,
                    "unstaged": unstaged,
                    "untracked": untracked,
                    "conflicted": conflicted,
                },
                "staged": [],
                "unstaged": [],
                "untracked": [],
                "conflicted": [],
                "listed_paths_truncated": False,
                "redacted_paths": 0,
                "unrepresentable_paths": 0,
            },
        )

    def error(self, operation: str = "hash", error: str = "PermissionError") -> Event:
        return self._append(
            EventType.ERROR_OBSERVED,
            EventSource.FILESYSTEM,
            f"Filesystem error during {operation}: locked.bin",
            path="locked.bin",
            payload={"operation": operation, "error": error, "errno": 13, "detail": None},
        )

    # -- commands / checks -----------------------------------------------------------------

    def start(self, argv: list[str], cwd: str = ".", command_id: str | None = None) -> Event:
        command = redact_command(argv, project_root=self.store.project_root, home=None)
        cid = command_id or secrets.token_hex(16)
        return self.store.append_event(self.session_id, started_draft(cid, command, cwd))

    def finish(
        self,
        started: Event,
        *,
        exit_code: int | None = 0,
        outcome: Outcome = Outcome.EXITED,
        command_id: str | None = None,
    ) -> Event:
        argv = [str(item) for item in started.payload["argv"]]  # type: ignore[union-attr]
        command = redact_command(argv, project_root=self.store.project_root, home=None)
        cid = command_id or str(started.payload["command_id"])
        return self.store.append_event(
            self.session_id, finished_draft(cid, command, _result(outcome, exit_code))
        )

    def check(
        self,
        started: Event,
        name: str,
        *,
        exit_code: int | None = 0,
        outcome: Outcome = Outcome.EXITED,
        kind: CheckKind = CheckKind.TEST,
        command_id: str | None = None,
    ) -> Event:
        cid = command_id or str(started.payload["command_id"])
        classification = CheckClassification(kind, name, ClassificationSource.AUTOMATIC)
        draft = check_draft(secrets.token_hex(16), cid, classification, _result(outcome, exit_code))
        return self.store.append_event(self.session_id, draft)

    def run_check(
        self,
        passed: bool,
        argv: list[str] | None = None,
        *,
        name: str = "pytest",
        cwd: str = ".",
        kind: CheckKind = CheckKind.TEST,
    ) -> tuple[Event, Event, Event]:
        started = self.start(argv or PYTEST, cwd)
        code = 0 if passed else 1
        finished = self.finish(started, exit_code=code)
        return started, finished, self.check(started, name, exit_code=code, kind=kind)

    def run_command(
        self,
        argv: list[str],
        *,
        exit_code: int | None = 0,
        outcome: Outcome = Outcome.EXITED,
    ) -> tuple[Event, Event]:
        started = self.start(argv)
        return started, self.finish(started, exit_code=exit_code, outcome=outcome)

    def stop(self) -> Event:
        draft = EventDraft(
            type=EventType.SESSION_STOPPED,
            source=EventSource.AGENTBODYCAM,
            summary="Recording stopped",
            payload={"reason": "requested"},
        )
        event, _ = self.store.stop_recording_session(self.session_id, draft)
        return event


def _result(outcome: Outcome, exit_code: int | None) -> CommandResult:
    if outcome is Outcome.SPAWN_ERROR:
        return CommandResult(outcome, None, None, 1, None, (), "FileNotFoundError", 2)
    if outcome is Outcome.SIGNALED:
        return CommandResult(outcome, None, 9, 5, 4321, ())
    if outcome is Outcome.INTERRUPTED:
        return CommandResult(outcome, exit_code, None, 5, 4321, ("SIGINT",))
    return CommandResult(outcome, exit_code, None, 10, 4321, ())


def probe_inactive(_root: Path) -> ActiveSessionInfo:
    return ActiveSessionInfo(ActiveState.INACTIVE)


def probe_active(session_id: str) -> Callable[[Path], ActiveSessionInfo]:
    record = ActiveRecord(
        session_id=session_id,
        pid=1234,
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
        agentbodycam_version="0",
        lease_id="0" * 32,
    )

    def probe(_root: Path) -> ActiveSessionInfo:
        return ActiveSessionInfo(ActiveState.ACTIVE, record=record)

    return probe


def synthetic_events(
    session_id: str, specs: list[tuple[EventType, str | None, JsonObject]]
) -> list[Event]:
    """In-memory events (no store) for pure ``analyze_events`` tests."""
    base = datetime(2026, 10, 14, tzinfo=UTC)
    return [
        Event(
            event_id=f"{number:032x}",
            session_id=session_id,
            sequence=number,
            timestamp=base + timedelta(milliseconds=number),
            monotonic_ns=number,
            type=type_,
            source=EventSource.COMMAND,
            category=CATEGORY_BY_TYPE[type_],
            evidence=EvidenceKind.OBSERVED,
            summary=f"{type_.value} {number}",
            path=path,
            payload=payload,
        )
        for number, (type_, path, payload) in enumerate(specs, start=1)
    ]
