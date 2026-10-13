"""Universal recorder: polls the project and appends observed file events to a session.

The recorder only observes. It reads the project through :class:`ProjectScanner` and writes
nothing except through :class:`EventStore` (which only writes inside ``.agentbodycam``).
It attributes changes to no one: events say *what* changed and *when it was detected*.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from types import FrameType

from agentbodycam.core.ignore import DEFAULT_IGNORE_POLICY, IgnorePolicy
from agentbodycam.core.scanner import (
    DEFAULT_MAX_HASH_BYTES,
    ChangeKind,
    FileChange,
    FileState,
    ProjectScanner,
    ScanIssue,
    diff_snapshots,
)
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import (
    MAX_SUMMARY_CHARS,
    Event,
    EventDraft,
    EventSource,
    EventType,
    JsonObject,
)
from agentbodycam.models.session import Session

DEFAULT_INTERVAL_SECONDS = 0.5
RECORDER_KIND = "polling"

_EVENT_TYPE = {
    ChangeKind.CREATED: EventType.FILE_CREATED,
    ChangeKind.MODIFIED: EventType.FILE_MODIFIED,
    ChangeKind.DELETED: EventType.FILE_DELETED,
}


class RecorderError(Exception):
    """The recorder was used out of order (e.g. scanning before starting)."""


class StopReason(StrEnum):
    REQUESTED = "requested"  # Ctrl+C / termination signal / request_stop()
    DURATION = "duration"
    INTERRUPTED = "interrupted"  # KeyboardInterrupt (e.g. a second Ctrl+C)
    ERROR = "error"


@dataclass(slots=True)
class RecorderStats:
    scans: int = 0
    baseline_files: int = 0
    created: int = 0
    modified: int = 0
    deleted: int = 0
    errors: int = 0

    @property
    def file_events(self) -> int:
        return self.created + self.modified + self.deleted


def _summary(prefix: str, path: str | None) -> str:
    if path is None:
        return prefix
    text = f"{prefix}: {path}"
    return text if len(text) <= MAX_SUMMARY_CHARS else prefix


def _change_draft(change: FileChange, scan: int) -> EventDraft:
    payload: JsonObject = {"detected_by": RECORDER_KIND, "scan": scan}
    if change.after is not None:
        payload.update(change.after.evidence())
    if change.before is not None:
        payload["previous"] = change.before.evidence()
    if change.evidence is not None:
        payload["change_evidence"] = change.evidence
    return EventDraft(
        type=_EVENT_TYPE[change.kind],
        source=EventSource.FILESYSTEM,
        summary=_summary(f"File {change.kind.value}", change.path),
        path=change.path,
        payload=payload,
    )


def _issue_draft(issue: ScanIssue, scan: int) -> EventDraft:
    return EventDraft(
        type=EventType.ERROR_OBSERVED,
        source=EventSource.FILESYSTEM,
        summary=_summary(f"Filesystem error during {issue.operation}", issue.path),
        path=issue.path,
        payload={
            "operation": issue.operation,
            "error": issue.error,
            "errno": issue.errno,
            "detail": issue.detail,
            "scan": scan,
        },
    )


class Recorder:
    """Polling file-system recorder bound to one new session.

    Lifecycle: :meth:`start` -> :meth:`take_baseline` -> :meth:`scan_once`* -> :meth:`stop`,
    or simply :meth:`run`, which does all of it.
    """

    def __init__(
        self,
        store: EventStore,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        policy: IgnorePolicy = DEFAULT_IGNORE_POLICY,
        max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
        on_event: Callable[[Event], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not interval > 0:
            raise ValueError("interval must be positive")
        self.store = store
        self.interval = interval
        self.policy = policy
        self.scanner = ProjectScanner(
            store.project_root, policy=policy, max_hash_bytes=max_hash_bytes
        )
        self.stats = RecorderStats()
        self._on_event = on_event
        self._sleep = sleep
        self._monotonic = monotonic
        self._session: Session | None = None
        self._snapshot: dict[str, FileState] | None = None
        self._reported: set[tuple[str, str, str | None]] = set()
        self._stopped = False
        self._stop_requested = False

    # -- state -------------------------------------------------------------------------

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RecorderError("recorder has not been started")
        return self._session

    @property
    def snapshot(self) -> dict[str, FileState]:
        return dict(self._snapshot or {})

    @property
    def is_active(self) -> bool:
        """Started and not yet stopped."""
        return self._session is not None and not self._stopped

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def request_stop(self) -> None:
        """Ask :meth:`run` to finish after its current sleep or scan. Signal-safe."""
        self._stop_requested = True

    def _require_active(self) -> str:
        if self._stopped:
            raise RecorderError("recorder has already stopped")
        return self.session.session_id

    def _emit(self, draft: EventDraft) -> Event:
        event = self.store.append_event(self._require_active(), draft)
        if self._on_event is not None:
            self._on_event(event)
        return event

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> Session:
        """Create a new session, mark it ``recording`` and append ``session.started``."""
        if self._session is not None:
            raise RecorderError("recorder has already been started")
        created = self.store.create_session()
        self._session = self.store.mark_recording(created.session_id)
        self._emit(
            EventDraft(
                type=EventType.SESSION_STARTED,
                source=EventSource.AGENTBODYCAM,
                summary="Recording started",
                payload={
                    "recorder": RECORDER_KIND,
                    "interval_seconds": self.interval,
                    "max_hash_bytes": self.scanner.max_hash_bytes,
                    "ignore_policy": {
                        "names": sorted(self.policy.ignored_names),
                        "sensitive_patterns": list(self.policy.sensitive_patterns),
                        "noise_patterns": list(self.policy.noise_patterns),
                    },
                },
            )
        )
        return self._session

    def take_baseline(self) -> int:
        """Snapshot the project as it is now. Pre-existing files produce no file events."""
        self._require_active()
        if self._snapshot is not None:
            raise RecorderError("baseline has already been taken")
        result = self.scanner.scan({}, defer=False)
        self._snapshot = result.files
        self.stats.baseline_files = len(result.files)
        self._report_issues(result.issues)
        return len(result.files)

    def scan_once(self) -> list[Event]:
        """Scan, append one event per observed change, and return the appended events."""
        self._require_active()
        if self._snapshot is None:
            raise RecorderError("take_baseline() must be called before scan_once()")
        result = self.scanner.scan(self._snapshot)
        self.stats.scans += 1
        events: list[Event] = []
        for change in diff_snapshots(self._snapshot, result.files):
            events.append(self._emit(_change_draft(change, self.stats.scans)))
            # Updated per event so an interruption mid-scan never re-emits recorded changes.
            if change.after is None:
                self._snapshot.pop(change.path, None)
            else:
                self._snapshot[change.path] = change.after
            if change.kind is ChangeKind.CREATED:
                self.stats.created += 1
            elif change.kind is ChangeKind.MODIFIED:
                self.stats.modified += 1
            else:
                self.stats.deleted += 1
        self._snapshot = result.files
        events += self._report_issues(result.issues)
        return events

    def _report_issues(self, issues: list[ScanIssue]) -> list[Event]:
        """Append ``error.observed`` once per distinct ongoing problem, not once per scan."""
        events: list[Event] = []
        current = {issue.key() for issue in issues}
        for issue in issues:
            if issue.key() in self._reported:
                continue
            self._reported.add(issue.key())
            events.append(self._emit(_issue_draft(issue, self.stats.scans)))
            self.stats.errors += 1
        self._reported &= current
        return events

    def stop(self, reason: StopReason = StopReason.REQUESTED) -> Session:
        """Append ``session.stopped``, then mark the session ``stopped`` with ``ended_at``."""
        session_id = self._require_active()
        stats = self.stats
        self._emit(
            EventDraft(
                type=EventType.SESSION_STOPPED,
                source=EventSource.AGENTBODYCAM,
                summary="Recording stopped",
                payload={
                    "reason": reason.value,
                    "scans": stats.scans,
                    "baseline_files": stats.baseline_files,
                    "files_tracked": len(self._snapshot or {}),
                    "file_events": {
                        "created": stats.created,
                        "modified": stats.modified,
                        "deleted": stats.deleted,
                    },
                    "errors_observed": stats.errors,
                },
            )
        )
        self._stopped = True
        self._session = self.store.mark_stopped(session_id)
        return self._session

    def run(self, *, duration: float | None = None) -> Session:
        """Record until :meth:`request_stop`, ``duration`` seconds, or KeyboardInterrupt.

        On a requested stop a final scan captures changes made since the previous one.
        """
        if duration is not None and duration < 0:
            raise ValueError("duration must be non-negative")
        if self._session is None:
            self.start()
        reason = StopReason.REQUESTED
        try:
            if self._snapshot is None:
                self.take_baseline()
            deadline = None if duration is None else self._monotonic() + duration
            while not self._stop_requested:
                wait = self.interval
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - self._monotonic()))
                self._sleep(wait)
                self.scan_once()
                if deadline is not None and self._monotonic() >= deadline:
                    reason = StopReason.DURATION
                    break
        except KeyboardInterrupt:
            reason = StopReason.INTERRUPTED
        except BaseException:
            with contextlib.suppress(Exception):
                self.stop(StopReason.ERROR)
            raise
        return self.stop(reason)


@contextlib.contextmanager
def stop_on_signals(recorder: Recorder) -> Iterator[None]:
    """Turn Ctrl+C (and SIGTERM / Ctrl+Break) into a clean :meth:`Recorder.request_stop`.

    A second signal raises KeyboardInterrupt so a stuck scan can still be abandoned; the
    recorder then stops without a final scan. No-op outside the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    def handler(_signum: int, _frame: FrameType | None) -> None:
        if recorder.stop_requested:
            raise KeyboardInterrupt
        recorder.request_stop()

    previous = {sig: signal.signal(sig, handler) for sig in signals}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
