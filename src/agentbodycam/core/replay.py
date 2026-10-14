"""Read-only replay data for the local Replay API, independent of any transport.

:class:`ReplayService` answers the questions a replay UI asks (which sessions exist, what
does one look like, its forensic report, a page of its timeline) from stored evidence only.
Every answer comes from :class:`EventStore` reads and :func:`analyze_session`; there is no
second analysis, no cache of mutable session data and nothing is ever written, created or
executed (no Git, no subprocess, no network).

Each call opens a fresh :class:`EventStore` view, so concurrent calls share no mutable
state and a live recorder appending to a session is read with the store's usual
guarantees: a call sees every event fully written when it read the log.

Failures surface as :class:`ReplayError` with a stable code and a fixed message. Store
exception texts are never passed on because they contain absolute paths.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from agentbodycam.core.active import ActiveSessionInfo, get_active_session
from agentbodycam.core.forensics import ForensicInputError, analyze_session, timeline_item
from agentbodycam.core.store import EventStore, SessionNotFoundError, StoreError, UnsafePathError
from agentbodycam.models.events import EventValidationError, JsonObject
from agentbodycam.models.forensics import ForensicReport
from agentbodycam.models.replay import (
    TIMELINE_DEFAULT_LIMIT,
    TIMELINE_MAX_LIMIT,
    ProjectSummary,
    SessionDetail,
    SessionList,
    SessionSummary,
    TimelinePage,
)
from agentbodycam.models.session import InvalidSessionIdError, validate_session_id

ActiveProbe = Callable[[Path], ActiveSessionInfo]


class ReplayErrorCode(StrEnum):
    INVALID_SESSION_ID = "invalid_session_id"
    INVALID_PARAMETER = "invalid_parameter"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_UNREADABLE = "session_unreadable"
    STORE_UNSAFE = "store_unsafe"


_MESSAGES = {
    ReplayErrorCode.INVALID_SESSION_ID: "Malformed session ID.",
    ReplayErrorCode.SESSION_NOT_FOUND: "Session not found.",
    ReplayErrorCode.SESSION_UNREADABLE: "The session's stored evidence could not be read.",
    ReplayErrorCode.STORE_UNSAFE: (
        "The AgentBodycam store is not a plain directory inside the project "
        "(symlink, junction or unexpected file); refusing to read it."
    ),
}


class ReplayError(Exception):
    """A safe, client-presentable failure: ``code`` is stable, ``message`` is fixed text."""

    def __init__(self, code: ReplayErrorCode, message: str | None = None) -> None:
        self.code = code
        self.message = message or _MESSAGES[code]
        super().__init__(self.message)


def _translate(exc: Exception) -> ReplayError:
    if isinstance(exc, InvalidSessionIdError):
        return ReplayError(ReplayErrorCode.INVALID_SESSION_ID)
    if isinstance(exc, SessionNotFoundError):
        return ReplayError(ReplayErrorCode.SESSION_NOT_FOUND)
    if isinstance(exc, UnsafePathError):
        return ReplayError(ReplayErrorCode.STORE_UNSAFE)
    return ReplayError(ReplayErrorCode.SESSION_UNREADABLE)


# What reading a stored session may legitimately raise; anything else is a bug.
_READ_ERRORS = (StoreError, EventValidationError, ForensicInputError, OSError, ValueError)


class ReplayService:
    """Read-only replay queries over ``<project_root>/.agentbodycam``.

    Constructing a service, or calling any method, never creates ``.agentbodycam/``.
    """

    def __init__(self, project_root: Path, *, probe: ActiveProbe = get_active_session) -> None:
        self._project_root = Path(project_root)
        self._probe = probe

    def _store(self) -> EventStore:
        return EventStore(self._project_root)

    def _session_ids(self, store: EventStore) -> list[str]:
        try:
            return store.list_session_ids()
        except _READ_ERRORS as exc:
            raise _translate(exc) from None

    def _analyze(self, store: EventStore, session_id: str, *, timeline: bool) -> ForensicReport:
        try:
            validate_session_id(session_id)
            if timeline:
                return analyze_session(store, session_id, probe=self._probe)
            return analyze_session(store, session_id, probe=self._probe, max_timeline_items=0)
        except _READ_ERRORS as exc:
            raise _translate(exc) from None

    def project(self) -> ProjectSummary:
        return ProjectSummary(session_count=len(self._session_ids(self._store())))

    def list_sessions(self) -> SessionList:
        """Every session, newest first. An unreadable session is listed as such."""
        store = self._store()
        summaries: list[SessionSummary] = []
        for session_id in self._session_ids(store):
            try:
                report = self._analyze(store, session_id, timeline=False)
            except ReplayError as exc:
                if exc.code is ReplayErrorCode.STORE_UNSAFE:
                    raise
                summaries.append(SessionSummary(session_id=session_id, readable=False))
                continue
            summaries.append(
                SessionSummary(
                    session_id=report.session_id,
                    readable=True,
                    status=report.session_status,
                    recording_state=report.recording_state,
                    analysis_complete=report.analysis_complete,
                    started_at=report.started_at,
                    ended_at=report.ended_at,
                    event_count=report.event_count,
                )
            )
        # Session IDs begin with their UTC start second; started_at refines the order.
        summaries.sort(
            key=lambda s: (s.session_id[:16], s.started_at or "", s.session_id), reverse=True
        )
        return SessionList(tuple(summaries))

    def session_detail(self, session_id: str) -> SessionDetail:
        report = self._analyze(self._store(), session_id, timeline=False)
        return SessionDetail(
            session_id=report.session_id,
            status=report.session_status,
            recording_state=report.recording_state,
            analysis_complete=report.analysis_complete,
            started_at=report.started_at,
            ended_at=report.ended_at,
            event_count=report.event_count,
            event_counts=report.event_counts,
            command_summary=report.command_summary,
            observable_failure_count=report.observable_failures.total,
            recovery_count=report.recoveries.total,
            unresolved_failure_count=report.unresolved_failures.total,
            warning_count=report.warnings.total,
        )

    def report(self, session_id: str) -> ForensicReport:
        """Exactly what ``agentbodycam analyze --json`` reports for the session."""
        return self._analyze(self._store(), session_id, timeline=True)

    def report_dict(self, session_id: str) -> JsonObject:
        return self.report(session_id).to_dict()

    def timeline(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = TIMELINE_DEFAULT_LIMIT,
    ) -> TimelinePage:
        """Events with ``sequence > after_sequence``, at most ``limit``, in sequence order."""
        if after_sequence < 0:
            raise ReplayError(
                ReplayErrorCode.INVALID_PARAMETER, "after_sequence must be 0 or greater."
            )
        if not 1 <= limit <= TIMELINE_MAX_LIMIT:
            raise ReplayError(
                ReplayErrorCode.INVALID_PARAMETER,
                f"limit must be between 1 and {TIMELINE_MAX_LIMIT}.",
            )
        try:
            validate_session_id(session_id)
            events = self._store().read_events(session_id)
        except _READ_ERRORS as exc:
            raise _translate(exc) from None
        # The store guarantees events[i].sequence == i + 1.
        page = events[after_sequence : after_sequence + limit]
        return TimelinePage(
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
            items=tuple(timeline_item(event) for event in page),
            total=len(events),
        )
