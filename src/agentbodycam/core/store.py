"""Local, append-only session and event store.

Layout, relative to the project root::

    .agentbodycam/
      .gitignore              ignores everything so recordings are never committed
      sessions/
        <session-id>/
          session.json        session metadata, rewritten atomically
          events.jsonl        one canonical JSON event per line, append-only

Every path the store writes to is checked to be a real (non-symlinked) location inside
``<project>/.agentbodycam``. A single writer per session is assumed.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from agentbodycam import __version__
from agentbodycam.models.events import (
    CATEGORY_BY_TYPE,
    MAX_EVENT_BYTES,
    Event,
    EventDraft,
    EventValidationError,
    EvidenceKind,
    JsonValue,
    normalize_object,
)
from agentbodycam.models.session import (
    PlatformInfo,
    Session,
    SessionStatus,
    new_session_id,
    validate_session_id,
)

STORE_DIRNAME = ".agentbodycam"
SESSIONS_DIRNAME = "sessions"
SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
GITIGNORE_FILENAME = ".gitignore"
GITIGNORE_CONTENT = (
    "# Created by AgentBodycam. Session recordings are local and must not be committed.\n*\n"
)

_CREATE_ATTEMPTS = 5


class StoreError(Exception):
    """Base class for event store failures."""


class UnsafePathError(StoreError):
    """A store path is a symlink, not a directory, or resolves outside the store."""


class SessionNotFoundError(StoreError):
    pass


class CorruptSessionError(StoreError):
    pass


class SessionStateError(StoreError):
    """A lifecycle transition is not allowed from the session's current status."""


class EvidenceRejectedError(StoreError):
    """The store only accepts observed evidence."""


class CorruptEventLogError(StoreError):
    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"{path}: line {line_number}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Tail:
    size: int
    last_sequence: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _strict_json_loads(text: str) -> object:
    return json.loads(
        text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
    )


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def parse_event_log(data: bytes, *, path: Path, session_id: str) -> list[Event]:
    """Parse and validate a complete ``events.jsonl`` payload, preserving file order."""
    if not data:
        return []
    lines = data.split(b"\n")
    if lines[-1] != b"":
        raise CorruptEventLogError(path, len(lines), "truncated final line (missing newline)")
    events: list[Event] = []
    seen_ids: set[str] = set()
    for number, raw in enumerate(lines[:-1], start=1):
        if not raw.strip():
            raise CorruptEventLogError(path, number, "blank line")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptEventLogError(path, number, f"invalid UTF-8: {exc}") from exc
        try:
            obj = _strict_json_loads(text)
        except ValueError as exc:
            raise CorruptEventLogError(path, number, f"malformed JSON: {exc}") from exc
        try:
            event = Event.from_dict(obj)
        except EventValidationError as exc:
            raise CorruptEventLogError(path, number, f"invalid event: {exc}") from exc
        if event.session_id != session_id:
            raise CorruptEventLogError(
                path, number, f"event belongs to session {event.session_id!r}"
            )
        if event.sequence != number:
            kind = "duplicate or out-of-order" if event.sequence < number else "gap in"
            raise CorruptEventLogError(
                path, number, f"{kind} sequence: got {event.sequence}, expected {number}"
            )
        if event.event_id in seen_ids:
            raise CorruptEventLogError(path, number, f"duplicate event_id {event.event_id}")
        seen_ids.add(event.event_id)
        events.append(event)
    return events


class EventStore:
    """Session and event storage rooted at ``<project_root>/.agentbodycam``."""

    def __init__(
        self,
        project_root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / STORE_DIRNAME
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._tails: dict[str, _Tail] = {}

    # -- path safety -----------------------------------------------------------------

    def _check_real(self, path: Path, *, want_dir: bool) -> Path:
        """Ensure an existing ``path`` is a real file/dir that resolves to itself."""
        if path.is_symlink():
            raise UnsafePathError(f"refusing to use symlinked path: {path}")
        resolved = path.resolve()
        if not _same_path(resolved, path) or not resolved.is_relative_to(self.root):
            raise UnsafePathError(f"path resolves outside the AgentBodycam store: {path}")
        if want_dir and not path.is_dir():
            raise UnsafePathError(f"expected a directory: {path}")
        if not want_dir and not path.is_file():
            raise UnsafePathError(f"expected a regular file: {path}")
        return path

    def _existing_dir(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        self._check_real(path, want_dir=True)
        return True

    def _ensure_dir(self, path: Path) -> None:
        if not self._existing_dir(path):
            path.mkdir()
            self._check_real(path, want_dir=True)

    def session_dir(self, session_id: str) -> Path:
        """Directory for ``session_id``. Rejects malformed IDs before touching the disk."""
        return self.sessions_dir / validate_session_id(session_id)

    def ensure_layout(self) -> None:
        """Create ``.agentbodycam/`` (with its ``.gitignore``) and ``sessions/``."""
        if not self.project_root.is_dir():
            raise StoreError(f"project root is not a directory: {self.project_root}")
        self._ensure_dir(self.root)
        gitignore = self.root / GITIGNORE_FILENAME
        if gitignore.exists() or gitignore.is_symlink():
            self._check_real(gitignore, want_dir=False)
        else:
            _atomic_write_text(gitignore, GITIGNORE_CONTENT)
        self._ensure_dir(self.sessions_dir)

    def _require_session_dir(self, session_id: str) -> Path:
        directory = self.session_dir(session_id)
        if not (
            self._existing_dir(self.root)
            and self._existing_dir(self.sessions_dir)
            and self._existing_dir(directory)
        ):
            raise SessionNotFoundError(f"session not found: {session_id}")
        return directory

    def _require_file(self, path: Path, session_id: str) -> Path:
        if not path.exists() and not path.is_symlink():
            raise CorruptSessionError(f"session {session_id} is missing {path.name}")
        return self._check_real(path, want_dir=False)

    # -- sessions --------------------------------------------------------------------

    def create_session(self) -> Session:
        """Create an empty session: metadata plus an empty event log. Records nothing."""
        self.ensure_layout()
        for _ in range(_CREATE_ATTEMPTS):
            started = self._clock()
            session_id = new_session_id(started)
            directory = self.session_dir(session_id)
            try:
                directory.mkdir()
            except FileExistsError:
                continue
            self._check_real(directory, want_dir=True)
            session = Session(
                session_id=session_id,
                project_root=str(self.project_root),
                started_at=started,
                agentbodycam_version=__version__,
                platform=PlatformInfo.current(),
            )
            with (directory / EVENTS_FILENAME).open("xb"):
                pass
            # session.json is written last so its presence marks a complete session.
            self._write_session(directory, session)
            return session
        raise StoreError("could not allocate a unique session ID")

    def _write_session(self, directory: Path, session: Session) -> None:
        text = json.dumps(session.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        _atomic_write_text(directory / SESSION_FILENAME, text + "\n")

    def load_session(self, session_id: str) -> Session:
        directory = self._require_session_dir(session_id)
        path = self._require_file(directory / SESSION_FILENAME, session_id)
        try:
            session = Session.from_dict(_strict_json_loads(path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptSessionError(f"{path}: {exc}") from exc
        if session.session_id != session_id:
            raise CorruptSessionError(
                f"{path}: session_id {session.session_id!r} does not match directory"
            )
        return session

    def _transition(
        self, session_id: str, expected: SessionStatus, target: SessionStatus
    ) -> Session:
        session = self.load_session(session_id)
        if session.status is not expected:
            raise SessionStateError(
                f"session {session_id} is {session.status.value!r}; cannot mark it {target.value!r}"
            )
        ended_at = None
        if target is SessionStatus.STOPPED:
            ended_at = max(self._clock(), session.started_at)
        updated = replace(session, status=target, ended_at=ended_at)
        self._write_session(self.session_dir(session_id), updated)
        return updated

    def mark_recording(self, session_id: str) -> Session:
        """``created`` -> ``recording``. Rewrites ``session.json`` atomically."""
        return self._transition(session_id, SessionStatus.CREATED, SessionStatus.RECORDING)

    def mark_stopped(self, session_id: str) -> Session:
        """``recording`` -> ``stopped`` with ``ended_at``. Rewrites ``session.json`` atomically."""
        return self._transition(session_id, SessionStatus.RECORDING, SessionStatus.STOPPED)

    def list_session_ids(self) -> list[str]:
        """IDs of session directories, oldest first. Unrecognised entries are ignored."""
        if not (self._existing_dir(self.root) and self._existing_dir(self.sessions_dir)):
            return []
        ids: list[str] = []
        for entry in self.sessions_dir.iterdir():
            try:
                validate_session_id(entry.name)
            except ValueError:
                continue
            if entry.is_dir() or entry.is_symlink():
                ids.append(entry.name)
        return sorted(ids)

    def list_sessions(self) -> list[Session]:
        sessions = [self.load_session(session_id) for session_id in self.list_session_ids()]
        return sorted(sessions, key=lambda s: (s.started_at, s.session_id))

    # -- events ----------------------------------------------------------------------

    def events_path(self, session_id: str) -> Path:
        directory = self._require_session_dir(session_id)
        return self._require_file(directory / EVENTS_FILENAME, session_id)

    def read_events(self, session_id: str) -> list[Event]:
        """All events in sequence order. Raises :class:`CorruptEventLogError` on bad data."""
        path = self.events_path(session_id)
        data = path.read_bytes()
        events = parse_event_log(data, path=path, session_id=session_id)
        self._tails[session_id] = _Tail(size=len(data), last_sequence=len(events))
        return events

    def count_events(self, session_id: str) -> int:
        return len(self.read_events(session_id))

    def append_event(self, session_id: str, draft: EventDraft) -> Event:
        """Stamp ``draft`` with identity, sequence and time, then append it durably."""
        if draft.evidence is not EvidenceKind.OBSERVED:
            raise EvidenceRejectedError(
                f"the event store only accepts observed evidence, got {draft.evidence.value!r}"
            )
        path = self.events_path(session_id)
        tail = self._tails.get(session_id)
        if tail is None or tail.size != path.stat().st_size:
            self.load_session(session_id)
            self.read_events(session_id)
            tail = self._tails[session_id]

        event = Event(
            event_id=secrets.token_hex(16),
            session_id=session_id,
            sequence=tail.last_sequence + 1,
            timestamp=self._clock(),
            monotonic_ns=self._monotonic_ns(),
            type=draft.type,
            source=draft.source,
            category=CATEGORY_BY_TYPE[draft.type],
            evidence=draft.evidence,
            summary=draft.summary,
            path=draft.path,
            payload=normalize_object(draft.payload),
        )
        line = (event.to_json_line() + "\n").encode("utf-8")
        if len(line) > MAX_EVENT_BYTES:
            raise EventValidationError(
                f"event is {len(line)} bytes; the limit is {MAX_EVENT_BYTES} bytes"
            )
        with path.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._tails[session_id] = _Tail(size=tail.size + len(line), last_sequence=event.sequence)
        return event
