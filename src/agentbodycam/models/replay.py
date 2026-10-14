"""JSON-ready response models of the local Replay API (``/api/v1``).

They only carry replay-facing facts that the forensics engine already derives: session
identity, status, times, counts and the safe :class:`TimelineItem` view of events. None of
them holds the absolute project root, host or user names, platform details or raw event
payloads. The forensic report itself is served as :meth:`ForensicReport.to_dict` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentbodycam.models.events import JsonObject
from agentbodycam.models.forensics import CommandSummary, RecordingState, TimelineItem

REPLAY_API_VERSION = 1
PRODUCT_NAME = "AgentBodycam"
TIMELINE_DEFAULT_LIMIT = 100
TIMELINE_MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One entry of the session picker. ``readable: false`` leaves the other fields null."""

    session_id: str
    readable: bool
    status: str | None = None
    recording_state: RecordingState | None = None
    analysis_complete: bool | None = None
    started_at: str | None = None
    ended_at: str | None = None
    event_count: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "session_id": self.session_id,
            "readable": self.readable,
            "status": self.status,
            "recording_state": None if self.recording_state is None else self.recording_state.value,
            "analysis_complete": self.analysis_complete,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "event_count": self.event_count,
        }


@dataclass(frozen=True, slots=True)
class SessionList:
    sessions: tuple[SessionSummary, ...]  # newest first

    def to_dict(self) -> JsonObject:
        return {
            "api_version": REPLAY_API_VERSION,
            "sessions": [session.to_dict() for session in self.sessions],
            "total": len(self.sessions),
        }


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    session_count: int

    def to_dict(self) -> JsonObject:
        return {"api_version": REPLAY_API_VERSION, "session_count": self.session_count}


@dataclass(frozen=True, slots=True)
class SessionDetail:
    """Header data for one session, taken from its forensic report."""

    session_id: str
    status: str
    recording_state: RecordingState
    analysis_complete: bool
    started_at: str
    ended_at: str | None
    event_count: int
    event_counts: tuple[tuple[str, int], ...]
    command_summary: CommandSummary
    observable_failure_count: int
    recovery_count: int
    unresolved_failure_count: int
    warning_count: int

    def to_dict(self) -> JsonObject:
        return {
            "api_version": REPLAY_API_VERSION,
            "session_id": self.session_id,
            "status": self.status,
            "recording_state": self.recording_state.value,
            "analysis_complete": self.analysis_complete,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "event_count": self.event_count,
            "event_counts": dict(self.event_counts),
            "command_summary": self.command_summary.to_dict(),
            "observable_failure_count": self.observable_failure_count,
            "recovery_count": self.recovery_count,
            "unresolved_failure_count": self.unresolved_failure_count,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """Events with ``after_sequence < sequence <= next_after_sequence``, in sequence order."""

    session_id: str
    after_sequence: int
    limit: int
    items: tuple[TimelineItem, ...]
    total: int  # events stored when the page was read

    @property
    def next_after_sequence(self) -> int:
        return self.items[-1].sequence if self.items else self.after_sequence

    @property
    def has_more(self) -> bool:
        return self.next_after_sequence < self.total

    def to_dict(self) -> JsonObject:
        return {
            "api_version": REPLAY_API_VERSION,
            "session_id": self.session_id,
            "after_sequence": self.after_sequence,
            "limit": self.limit,
            "items": [item.to_dict() for item in self.items],
            "next_after_sequence": self.next_after_sequence,
            "has_more": self.has_more,
            "total": self.total,
        }


def health() -> JsonObject:
    """The only unauthenticated response: no project, session, user or machine data."""
    return {"status": "ok", "api_version": REPLAY_API_VERSION, "product": PRODUCT_NAME}
