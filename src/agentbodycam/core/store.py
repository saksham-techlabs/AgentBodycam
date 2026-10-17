"""Local, append-only session and event store.

Layout, relative to the project root::

    .agentbodycam/
      .gitignore              ignores everything so recordings are never committed
      locks/
        <session-id>.events.lock   OS lock serialising appends to that session
      sessions/
        <session-id>/
          session.json        session metadata, rewritten atomically
          events.jsonl        one canonical JSON event per line, append-only

Every path the store writes to is checked to be a real (non-symlinked) location inside
``<project>/.agentbodycam``. On POSIX, AgentBodycam-managed store directories are created
and hardened to mode ``0700`` and regular store files to ``0600`` (owner-only), independent
of the process umask; this is access control via permissions, not encryption. On Windows,
local access continues to follow the filesystem ACLs of the project directory. Any number
of threads and processes may append to the same session: each append holds the session's
event lock while it revalidates the log tail, assigns the next sequence number and writes
one line.

Live-recording producers use :meth:`EventStore.append_recording_event`, which checks that
the session is ``recording`` while holding that same lock, and the recorder closes a session
with :meth:`EventStore.stop_recording_session`, which appends ``session.stopped`` and marks
the session ``stopped`` without releasing it. No append of any kind is accepted once a log
contains ``session.stopped``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from agentbodycam import __version__
from agentbodycam.core.filelock import DEFAULT_TIMEOUT_SECONDS, FileLock, FileLockError
from agentbodycam.models.events import (
    CATEGORY_BY_TYPE,
    MAX_EVENT_BYTES,
    Event,
    EventDraft,
    EventType,
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
LOCKS_DIRNAME = "locks"
EVENT_LOCK_SUFFIX = ".events.lock"
SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
GITIGNORE_FILENAME = ".gitignore"
GITIGNORE_CONTENT = (
    "# Created by AgentBodycam. Session recordings are local and must not be committed.\n*\n"
)

# POSIX owner-only modes for AgentBodycam-managed store nodes. Creation requests these
# modes and then fchmod/chmod to the same value so a permissive umask cannot leave
# evidence group/world-readable. Windows ACLs are unchanged (mode bits are not security).
STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600
_POSIX_STORE_MODES = os.name == "posix"

_CREATE_ATTEMPTS = 5
# Windows refuses to replace a file another process has open for an instant (e.g. a
# reader of session.json or active.json); such a replace is retried for this long.
_REPLACE_RETRY_SECONDS = 2.0
# A plain bool (not an inline platform check) so that type checkers on every platform
# still analyse the retry loop below.
_RETRY_REPLACE = sys.platform == "win32"


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


class SessionNotRecordingError(SessionStateError):
    """A live-recording append or stop was refused: the session is not ``recording``."""


class SessionClosedError(SessionNotRecordingError):
    """The session's log already contains ``session.stopped``; nothing may follow it."""


class EvidenceRejectedError(StoreError):
    """The store only accepts observed evidence."""


class CorruptEventLogError(StoreError):
    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        super().__init__(f"{path}: line {line_number}: {reason}")
        self.path = path
        self.line_number = line_number
        self.reason = reason


class StoreLockError(StoreError):
    """A store lock could not be acquired (timeout or unsafe lock file)."""


@dataclass(slots=True)
class _Tail:
    """What is known to be valid in ``events.jsonl``: its first ``size`` bytes."""

    identity: tuple[int, int]
    size: int
    last_sequence: int
    event_ids: set[str] = field(default_factory=set)
    closed: bool = False  # the log contains session.stopped


# OS locks are per handle; this keeps threads of one process from polling each other.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _identity(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _closes(events: list[Event]) -> bool:
    return any(event.type is EventType.SESSION_STOPPED for event in events)


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


def strict_json_loads(text: str) -> object:
    return json.loads(
        text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
    )


def _open_nofollow_flags(*extra: int) -> int:
    flags = 0
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_BINARY", "O_NOINHERIT"):
        flags |= getattr(os, name, 0)
    for value in extra:
        flags |= value
    return flags


def _fchmod(fd: int, mode: int) -> None:
    """Set mode on an open FD. No-op on Windows (``os.fchmod`` is POSIX-only)."""
    if not _POSIX_STORE_MODES:
        return
    # ``os.fchmod`` is missing from Windows stdlib stubs; look it up dynamically.
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        raise StoreError("fchmod is required to harden store permissions on POSIX")
    fchmod(fd, mode)


def harden_store_node(path: Path, *, want_dir: bool) -> None:
    """Tighten a verified AgentBodycam store node to owner-only modes on POSIX.

    No-op on Windows. Callers must already have rejected symlinks/junctions and confirmed
    ``path`` is inside the store. Uses ``O_NOFOLLOW`` + ``fchmod`` where available so the
    mode change applies to the opened inode, not a swapped path.
    """
    if not _POSIX_STORE_MODES:
        return
    desired = STORE_DIR_MODE if want_dir else STORE_FILE_MODE
    flags = _open_nofollow_flags(os.O_RDONLY)
    if want_dir:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StoreError(f"cannot open store path to set permissions: {path}") from exc
    try:
        st = os.fstat(fd)
        if want_dir and not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(f"expected a directory: {path}")
        if not want_dir and not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(f"expected a regular file: {path}")
        if stat.S_IMODE(st.st_mode) == desired:
            return
        try:
            _fchmod(fd, desired)
        except OSError as exc:
            raise StoreError(
                f"refusing to use store path with insecure permissions "
                f"({oct(stat.S_IMODE(st.st_mode))}): cannot harden {path}"
            ) from exc
        after = stat.S_IMODE(os.fstat(fd).st_mode)
        if after != desired:
            raise StoreError(
                f"refusing to use store path with insecure permissions ({oct(after)}): {path}"
            )
    finally:
        os.close(fd)


def secure_create_file(path: Path) -> None:
    """Create an empty regular file with owner-only permissions (``O_EXCL``, no follow)."""
    flags = _open_nofollow_flags(os.O_WRONLY, os.O_CREAT, os.O_EXCL)
    fd = os.open(path, flags, STORE_FILE_MODE)
    try:
        _fchmod(fd, STORE_FILE_MODE)
    finally:
        os.close(fd)


def open_store_file_append(path: Path) -> int:
    """Open an existing store file for append; harden to owner-only on POSIX."""
    flags = _open_nofollow_flags(os.O_WRONLY, os.O_APPEND)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(f"expected a regular file: {path}")
        if _POSIX_STORE_MODES and stat.S_IMODE(st.st_mode) != STORE_FILE_MODE:
            try:
                _fchmod(fd, STORE_FILE_MODE)
            except OSError as exc:
                raise StoreError(
                    f"refusing to use store path with insecure permissions "
                    f"({oct(stat.S_IMODE(st.st_mode))}): cannot harden {path}"
                ) from exc
            if stat.S_IMODE(os.fstat(fd).st_mode) != STORE_FILE_MODE:
                raise StoreError(f"refusing to use store path with insecure permissions: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        _fchmod(fd, STORE_FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
        if _POSIX_STORE_MODES:
            harden_store_node(path, want_dir=False)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _replace(tmp: Path, path: Path) -> None:
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    backoff = 0.001
    while True:
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if not _RETRY_REPLACE or time.monotonic() >= deadline:
                raise
        time.sleep(backoff)
        backoff = min(backoff * 2, 0.05)


def parse_event_log(data: bytes, *, path: Path, session_id: str) -> list[Event]:
    """Parse and validate a complete ``events.jsonl`` payload, preserving file order."""
    return _parse_lines(data, path=path, session_id=session_id, first_line=1, seen_ids=set())


def _parse_lines(
    data: bytes, *, path: Path, session_id: str, first_line: int, seen_ids: set[str]
) -> list[Event]:
    """Validate lines that start at line ``first_line``; ``seen_ids`` is updated in place."""
    if not data:
        return []
    lines = data.split(b"\n")
    if lines[-1] != b"":
        raise CorruptEventLogError(
            path, first_line + len(lines) - 1, "truncated final line (missing newline)"
        )
    events: list[Event] = []
    for number, raw in enumerate(lines[:-1], start=first_line):
        if not raw.strip():
            raise CorruptEventLogError(path, number, "blank line")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorruptEventLogError(path, number, f"invalid UTF-8: {exc}") from exc
        try:
            obj = strict_json_loads(text)
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
        lock_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / STORE_DIRNAME
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self.locks_dir = self.root / LOCKS_DIRNAME
        self.lock_timeout = lock_timeout
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._tails: dict[str, _Tail] = {}

    # -- path safety -----------------------------------------------------------------

    def _check_real(self, path: Path, *, want_dir: bool) -> Path:
        """Ensure an existing ``path`` is a real file/dir that resolves to itself.

        On POSIX, verified store nodes are hardened to owner-only modes (directories
        ``0700``, files ``0600``). Symlinks are rejected before any mode change.
        """
        if path.is_symlink():
            raise UnsafePathError(f"refusing to use symlinked path: {path}")
        resolved = path.resolve()
        if not _same_path(resolved, path) or not resolved.is_relative_to(self.root):
            raise UnsafePathError(f"path resolves outside the AgentBodycam store: {path}")
        if want_dir and not path.is_dir():
            raise UnsafePathError(f"expected a directory: {path}")
        if not want_dir and not path.is_file():
            raise UnsafePathError(f"expected a regular file: {path}")
        harden_store_node(path, want_dir=want_dir)
        return path

    def _existing_dir(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        self._check_real(path, want_dir=True)
        return True

    def _ensure_dir(self, path: Path) -> None:
        if not self._existing_dir(path):
            # Another process may create it between the check and mkdir.
            path.mkdir(mode=STORE_DIR_MODE, exist_ok=True)
            self._check_real(path, want_dir=True)

    def existing_store_file(self, path: Path) -> bool:
        """False if ``path`` is absent; True if it is a real file inside the store.

        Raises :class:`UnsafePathError` for symlinks, junctions, escapes and non-files.
        """
        if not path.exists() and not path.is_symlink():
            return False
        self._check_real(path, want_dir=False)
        return True

    def store_root_exists(self) -> bool:
        """Whether ``.agentbodycam/`` exists (raises if it is unsafe). Creates nothing."""
        if not self.project_root.is_dir():
            return False
        return self._existing_dir(self.root)

    def open_lock(self, path: Path, *, timeout: float | None = None) -> FileLock:
        """A :class:`FileLock` for ``path``, which must be directly in the store or ``locks/``.

        Creates the containing directory if needed. Does not acquire the lock.
        """
        if path.parent not in (self.root, self.locks_dir):
            raise UnsafePathError(f"lock files must live in the AgentBodycam store: {path}")
        self.ensure_layout()
        if path.parent == self.locks_dir:
            self._ensure_dir(self.locks_dir)
        self.existing_store_file(path)
        return FileLock(path, timeout=self.lock_timeout if timeout is None else timeout)

    def event_lock_path(self, session_id: str) -> Path:
        """Lock file serialising appends to ``session_id``; derived only from a valid ID."""
        return self.locks_dir / f"{validate_session_id(session_id)}{EVENT_LOCK_SUFFIX}"

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
            atomic_write_text(gitignore, GITIGNORE_CONTENT)
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
                directory.mkdir(mode=STORE_DIR_MODE)
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
            secure_create_file(directory / EVENTS_FILENAME)
            self._check_real(directory / EVENTS_FILENAME, want_dir=False)
            # session.json is written last so its presence marks a complete session.
            self._write_session(directory, session)
            return session
        raise StoreError("could not allocate a unique session ID")

    def _write_session(self, directory: Path, session: Session) -> None:
        text = json.dumps(session.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        atomic_write_text(directory / SESSION_FILENAME, text + "\n")

    def load_session(self, session_id: str) -> Session:
        directory = self._require_session_dir(session_id)
        path = self._require_file(directory / SESSION_FILENAME, session_id)
        try:
            session = Session.from_dict(strict_json_loads(path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptSessionError(f"{path}: {exc}") from exc
        if session.session_id != session_id:
            raise CorruptSessionError(
                f"{path}: session_id {session.session_id!r} does not match directory"
            )
        return session

    def _transition_locked(
        self, session: Session, expected: SessionStatus, target: SessionStatus
    ) -> Session:
        """Rewrite ``session.json`` with ``target``. Must hold the session's event lock."""
        if session.status is not expected:
            raise SessionStateError(
                f"session {session.session_id} is {session.status.value!r}; "
                f"cannot mark it {target.value!r}"
            )
        ended_at = None
        if target is SessionStatus.STOPPED:
            ended_at = max(self._clock(), session.started_at)
        updated = replace(session, status=target, ended_at=ended_at)
        self._write_session(self.session_dir(session.session_id), updated)
        return updated

    def _transition(
        self, session_id: str, expected: SessionStatus, target: SessionStatus
    ) -> Session:
        self.load_session(session_id)
        with self._event_lock(session_id):
            return self._transition_locked(self.load_session(session_id), expected, target)

    def mark_recording(self, session_id: str) -> Session:
        """``created`` -> ``recording``. Rewrites ``session.json`` atomically."""
        return self._transition(session_id, SessionStatus.CREATED, SessionStatus.RECORDING)

    def mark_stopped(self, session_id: str) -> Session:
        """``recording`` -> ``stopped`` with ``ended_at``. Rewrites ``session.json`` atomically.

        Appends nothing; a recorder closes its session with :meth:`stop_recording_session`.
        """
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
        with path.open("rb") as fh:
            identity = _identity(os.fstat(fh.fileno()))
            data = fh.read()
        events = parse_event_log(data, path=path, session_id=session_id)
        self._tails[session_id] = _Tail(
            identity=identity,
            size=len(data),
            last_sequence=len(events),
            event_ids={event.event_id for event in events},
            closed=_closes(events),
        )
        return events

    def count_events(self, session_id: str) -> int:
        return len(self.read_events(session_id))

    @contextlib.contextmanager
    def _event_lock(self, session_id: str) -> Iterator[None]:
        path = self.event_lock_path(session_id)
        thread_lock = _thread_lock(path)
        if not thread_lock.acquire(timeout=self.lock_timeout):
            raise StoreLockError(
                f"timed out after {self.lock_timeout:g}s waiting for the event lock of "
                f"session {session_id}"
            )
        try:
            try:
                file_lock = self.open_lock(path)
                file_lock.acquire()
            except FileLockError as exc:
                raise StoreLockError(str(exc)) from exc
            try:
                yield
            finally:
                file_lock.release()
        finally:
            thread_lock.release()

    def _sync_tail(self, session_id: str, path: Path) -> _Tail:
        """Bring the cached tail up to date with the file. Must hold the event lock.

        Bytes another writer appended since the last look are validated incrementally with
        the same rules as a full read; anything else (first use, a replaced or shrunken
        file) triggers a full revalidation of the session.
        """
        tail = self._tails.get(session_id)
        with path.open("rb") as fh:
            st = os.fstat(fh.fileno())
            if tail is not None and tail.identity == _identity(st):
                if st.st_size == tail.size:
                    return tail
                if st.st_size > tail.size:
                    fh.seek(tail.size)
                    data = fh.read(st.st_size - tail.size)
                    seen = set(tail.event_ids)
                    events = _parse_lines(
                        data,
                        path=path,
                        session_id=session_id,
                        first_line=tail.last_sequence + 1,
                        seen_ids=seen,
                    )
                    tail = _Tail(
                        identity=tail.identity,
                        size=tail.size + len(data),
                        last_sequence=tail.last_sequence + len(events),
                        event_ids=seen,
                        closed=tail.closed or _closes(events),
                    )
                    self._tails[session_id] = tail
                    return tail
        self._tails.pop(session_id, None)
        self.load_session(session_id)
        self.read_events(session_id)
        return self._tails[session_id]

    def append_event(self, session_id: str, draft: EventDraft) -> Event:
        """Stamp ``draft`` with identity, sequence and time, then append it durably.

        Safe to call concurrently from any number of threads and processes. Works for a
        session in any status (e.g. a developer-created empty session), but never after
        ``session.stopped`` (:class:`SessionClosedError`). Producers writing into a live
        recording use :meth:`append_recording_event` instead.
        """
        _require_observed(draft)
        self.events_path(session_id)
        with self._event_lock(session_id):
            return self._append_locked(session_id, draft)

    def append_recording_event(
        self,
        session_id: str,
        draft: EventDraft,
        *,
        check: Callable[[], None] | None = None,
    ) -> Event:
        """Append ``draft`` only if the session is ``recording`` at the moment of the append.

        The status is read while holding the session's event lock, the same lock
        :meth:`stop_recording_session` holds while it appends ``session.stopped`` and marks
        the session stopped, so a stop can never slip in between the check and the append.
        ``check``, if given, also runs while the lock is held, after the status check; it
        raises to veto the append. Raises :class:`SessionNotRecordingError` otherwise.
        ``session.stopped`` itself must go through :meth:`stop_recording_session`.
        """
        _require_observed(draft)
        if draft.type is EventType.SESSION_STOPPED:
            raise EventValidationError("session.stopped is appended by stop_recording_session()")
        self.events_path(session_id)
        with self._event_lock(session_id):
            self._require_recording(session_id)
            if check is not None:
                check()
            return self._append_locked(session_id, draft)

    def stop_recording_session(self, session_id: str, draft: EventDraft) -> tuple[Event, Session]:
        """Append the ``session.stopped`` ``draft`` and mark the session ``stopped``.

        Both happen under one continuous hold of the session's event lock, so once this
        returns no :meth:`append_recording_event` can succeed, and once ``session.stopped``
        is in the log no append of any kind can follow it (even if marking the session
        failed). Raises :class:`SessionNotRecordingError` if the session is not recording.
        """
        _require_observed(draft)
        if draft.type is not EventType.SESSION_STOPPED:
            raise EventValidationError(
                f"stop_recording_session() appends session.stopped, got {draft.type.value!r}"
            )
        self.events_path(session_id)
        with self._event_lock(session_id):
            session = self._require_recording(session_id)
            event = self._append_locked(session_id, draft)
            stopped = self._transition_locked(
                session, SessionStatus.RECORDING, SessionStatus.STOPPED
            )
            return event, stopped

    def _require_recording(self, session_id: str) -> Session:
        """The session, if it is ``recording``. Must hold the session's event lock."""
        session = self.load_session(session_id)
        if session.status is not SessionStatus.RECORDING:
            raise SessionNotRecordingError(
                f"session {session_id} is {session.status.value!r}, not 'recording'"
            )
        return session

    def _append_locked(self, session_id: str, draft: EventDraft) -> Event:
        path = self.events_path(session_id)
        tail = self._sync_tail(session_id, path)
        if tail.closed:
            raise SessionClosedError(
                f"session {session_id} has already recorded session.stopped; "
                "no event may be appended after it"
            )
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
        with os.fdopen(open_store_file_append(path), "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
            st = os.fstat(fh.fileno())
        if _identity(st) != tail.identity or st.st_size != tail.size + len(line):
            self._tails.pop(session_id, None)
            raise StoreError(
                f"{path} changed while event {event.sequence} was appended "
                "(written to without the event lock?)"
            )
        tail.event_ids.add(event.event_id)
        self._tails[session_id] = _Tail(
            identity=tail.identity,
            size=st.st_size,
            last_sequence=event.sequence,
            event_ids=tail.event_ids,
            closed=event.type is EventType.SESSION_STOPPED,
        )
        return event


def _require_observed(draft: EventDraft) -> None:
    if draft.evidence is not EvidenceKind.OBSERVED:
        raise EvidenceRejectedError(
            f"the event store only accepts observed evidence, got {draft.evidence.value!r}"
        )
