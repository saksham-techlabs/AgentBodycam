"""Universal recorder: polls the project and appends observed file and Git events.

The recorder only observes. It reads the project through :class:`ProjectScanner`, reads Git
state through :class:`GitObserver` (read-only Git commands), and writes nothing except
through :class:`EventStore` (which only writes inside ``.agentbodycam``). It attributes
changes to no one: events say *what* changed and *when it was detected*.
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

from agentbodycam.core.active import ActiveLease, acquire_active_lease
from agentbodycam.core.git_observer import (
    DEFAULT_GIT_INTERVAL_SECONDS,
    GitObserver,
    GitProblem,
    GitUnavailable,
    describe_snapshot,
    unavailable_payload,
)
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
    git_snapshots: int = 0

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


def _head_commit(payload: JsonObject | None) -> str | None:
    head = None if payload is None else payload.get("head")
    commit = head.get("commit") if isinstance(head, dict) else None
    return commit if isinstance(commit, str) else None


def _git_problem_draft(problem: GitProblem, scan: int) -> EventDraft:
    return EventDraft(
        type=EventType.ERROR_OBSERVED,
        source=EventSource.GIT,
        summary=_summary(f"Git error during {problem.operation}", problem.error),
        payload={**problem.payload(), "scan": scan},
    )


class GitStatus(StrEnum):
    """What the recorder currently knows about Git for this project."""

    DISABLED = "disabled"
    PENDING = "pending"  # not observed yet
    REPOSITORY = "repository"
    NOT_A_REPOSITORY = GitUnavailable.NOT_A_REPOSITORY.value
    GIT_NOT_FOUND = GitUnavailable.GIT_NOT_FOUND.value
    ERROR = "error"


class Recorder:
    """Polling file-system recorder bound to one new session.

    Lifecycle: :meth:`start` -> :meth:`take_baseline` -> :meth:`scan_once`* -> :meth:`stop`,
    or simply :meth:`run`, which does all of it. Git state is observed on the first scan
    (or an explicit :meth:`observe_git`) and then at most every ``git_interval`` seconds.

    :meth:`start` first takes the project's active-recording lease (one recorder per
    project) and :meth:`stop` releases it; a second recorder fails with
    :class:`~agentbodycam.core.active.RecordingInProgressError` before creating a session.
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
        record_git: bool = True,
        git_observer: GitObserver | None = None,
        git_interval: float = DEFAULT_GIT_INTERVAL_SECONDS,
    ) -> None:
        if not interval > 0:
            raise ValueError("interval must be positive")
        if not git_interval >= 0:
            raise ValueError("git_interval must be non-negative")
        self.store = store
        self.interval = interval
        self.policy = policy
        self.scanner = ProjectScanner(
            store.project_root, policy=policy, max_hash_bytes=max_hash_bytes
        )
        self.git: GitObserver | None = None
        if record_git:
            self.git = git_observer or GitObserver.discover(store.project_root, policy=policy)
        self.git_interval = git_interval
        self.stats = RecorderStats()
        self._on_event = on_event
        self._sleep = sleep
        self._monotonic = monotonic
        self._session: Session | None = None
        self._snapshot: dict[str, FileState] | None = None
        self._reported: set[tuple[str, str, str | None]] = set()
        self._git_payload: JsonObject | None = None
        self._git_status = GitStatus.PENDING if self.git is not None else GitStatus.DISABLED
        self._git_last_poll: float | None = None
        self._git_reported: set[tuple[str, str, int | None]] = set()
        self._stopped = False
        self._stop_requested = False
        self._lease: ActiveLease | None = None

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
    def git_status(self) -> GitStatus:
        return self._git_status

    @property
    def git_snapshot(self) -> JsonObject | None:
        """Payload of the last recorded ``git.snapshot`` (without per-event fields)."""
        return None if self._git_payload is None else dict(self._git_payload)

    @property
    def is_active(self) -> bool:
        """Started and not yet stopped."""
        return self._session is not None and not self._stopped

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def lease(self) -> ActiveLease | None:
        """The active-recording lease, while it is held."""
        return self._lease

    def release_lease(self) -> None:
        """Clear ``active.json`` and drop the active lock. Idempotent."""
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def request_stop(self) -> None:
        """Ask :meth:`run` to finish after its current sleep or scan. Signal-safe."""
        self._stop_requested = True

    def _require_active(self) -> str:
        if self._stopped:
            raise RecorderError("recorder has already stopped")
        return self.session.session_id

    def _emit(self, draft: EventDraft) -> Event:
        event = self.store.append_recording_event(self._require_active(), draft)
        self._notify(event)
        return event

    def _notify(self, event: Event) -> None:
        if self._on_event is not None:
            self._on_event(event)

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> Session:
        """Take the active lease, create a ``recording`` session, append ``session.started``.

        ``active.json`` is published only after ``session.started``, so anything that
        discovers the session sees it already started.
        """
        if self._session is not None or self._lease is not None:
            raise RecorderError("recorder has already been started")
        self._lease = acquire_active_lease(self.store)
        try:
            created = self.store.create_session()
            self._session = self.store.mark_recording(created.session_id)
            self._emit_started()
            self._lease.publish(self._session)
        except BaseException:
            self.release_lease()
            raise
        return self._session

    def _emit_started(self) -> None:
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
                    "git": {
                        "enabled": self.git is not None,
                        "executable_found": self.git is not None
                        and self.git.executable is not None,
                        "interval_seconds": self.git_interval,
                    },
                },
            )
        )

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

    def scan_once(self, *, force_git: bool = False) -> list[Event]:
        """Scan, append one event per observed change, and return the appended events.

        Files are always scanned; Git is observed when due (or when ``force_git``).
        """
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
        if force_git or self._git_due():
            events += self.observe_git()
        return events

    # -- git ---------------------------------------------------------------------------

    def _git_due(self) -> bool:
        if self.git is None:
            return False
        if self._git_last_poll is None:
            return True
        if self._git_status is GitStatus.GIT_NOT_FOUND:
            return False
        return self._monotonic() - self._git_last_poll >= self.git_interval

    def observe_git(self) -> list[Event]:
        """Look at Git once; append a ``git.snapshot`` only if the observed state changed.

        Never raises for Git problems: they become (de-duplicated) ``error.observed`` events
        so file recording always continues.
        """
        self._require_active()
        if self.git is None:
            return []
        first = self._git_last_poll is None
        self._git_last_poll = self._monotonic()
        try:
            observation = self.git.observe()
        except Exception as exc:  # isolation: a Git-side bug must never stop file recording
            self._git_status = GitStatus.ERROR
            return self._report_git_problems([GitProblem("observe", type(exc).__name__)])

        if observation.problem is not None:
            self._git_status = GitStatus.ERROR
            return self._report_git_problems([observation.problem])

        events = self._report_git_problems([])
        payload: JsonObject | None = None
        if observation.state is not None:
            self._git_status = GitStatus.REPOSITORY
            payload = observation.state.payload()
        elif observation.unavailable is not None:
            self._git_status = GitStatus(observation.unavailable.value)
            if self._git_payload is not None:
                payload = unavailable_payload(observation.unavailable)
            missing = observation.unavailable is GitUnavailable.GIT_NOT_FOUND
            if first and missing and (self.store.project_root / ".git").exists():
                events += self._report_git_problems(
                    [GitProblem("observe", GitUnavailable.GIT_NOT_FOUND.value)]
                )
        if payload is not None and payload != self._git_payload:
            events.append(self._emit(self._git_snapshot_draft(payload)))
            self._git_payload = payload
            self.stats.git_snapshots += 1
        return events

    def _git_snapshot_draft(self, payload: JsonObject) -> EventDraft:
        previous = self._git_payload
        summary, changed = describe_snapshot(previous, payload)
        data: JsonObject = {
            **payload,
            "changed": list(changed),
            "detected_by": RECORDER_KIND,
            "scan": self.stats.scans,
        }
        old_commit = _head_commit(previous)
        new_commit = _head_commit(payload)
        if (
            self.git is not None
            and previous is not None
            and previous.get("repository") is True
            and payload.get("repository") is True
            and old_commit != new_commit
        ):
            try:
                data["head_change"] = self.git.head_change(old_commit, new_commit)
            except Exception as exc:  # the snapshot itself must still be recorded
                data["head_change"] = {
                    "previous": old_commit,
                    "current": new_commit,
                    "relation": "unknown",
                    "commits": None,
                    "commits_error": type(exc).__name__,
                }
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[: MAX_SUMMARY_CHARS - 3] + "..."
        return EventDraft(
            type=EventType.GIT_SNAPSHOT, source=EventSource.GIT, summary=summary, payload=data
        )

    def _report_git_problems(self, problems: list[GitProblem]) -> list[Event]:
        """One ``error.observed`` per distinct ongoing Git problem, not one per poll."""
        events: list[Event] = []
        current = {problem.key() for problem in problems}
        for problem in problems:
            if problem.key() in self._git_reported:
                continue
            self._git_reported.add(problem.key())
            events.append(self._emit(_git_problem_draft(problem, self.stats.scans)))
            self.stats.errors += 1
        self._git_reported &= current
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
        """Append ``session.stopped``, mark the session ``stopped``, release the lease.

        The first two happen atomically with respect to every other writer of the session
        (:meth:`EventStore.stop_recording_session`). The lease is released even if closing
        the session fails.
        """
        session_id = self._require_active()
        try:
            return self._close_session(session_id, reason)
        finally:
            self.release_lease()

    def _close_session(self, session_id: str, reason: StopReason) -> Session:
        stats = self.stats
        draft = EventDraft(
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
                "git_snapshots": stats.git_snapshots,
                "git_status": self._git_status.value,
            },
        )
        event, self._session = self.store.stop_recording_session(session_id, draft)
        self._stopped = True
        self._notify(event)
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
            if self._git_status is GitStatus.PENDING:
                self.observe_git()
            deadline = None if duration is None else self._monotonic() + duration
            while not self._stop_requested:
                wait = self.interval
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - self._monotonic()))
                self._sleep(wait)
                expired = deadline is not None and self._monotonic() >= deadline
                # The last scan always looks at Git too, so the final state is captured.
                self.scan_once(force_git=expired or self._stop_requested)
                if not expired and deadline is not None and self._monotonic() >= deadline:
                    expired = True
                    self.observe_git()
                if expired:
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
