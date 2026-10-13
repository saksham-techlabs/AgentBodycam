"""The project's active recording: an OS-backed lease plus small, untrusted metadata.

Layout, relative to the project root::

    .agentbodycam/
      active.lock     held exclusively (OS lock) by the one live recorder; never deleted
      active.json     which session that recorder is writing (written atomically)

The OS lock on ``active.lock`` is the only authority for "a recorder is alive": the
operating system drops it when the recorder exits or is killed. ``active.json`` is only a
pointer and may outlive a crashed recorder; without a held lock it is stale. PIDs are
recorded for display only and are never used to decide liveness (PIDs are reused).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType

from agentbodycam import __version__
from agentbodycam.core.filelock import FileLock, FileLockError, LockTimeoutError, is_locked
from agentbodycam.core.store import (
    EventStore,
    SessionNotFoundError,
    StoreError,
    atomic_write_text,
    strict_json_loads,
)
from agentbodycam.models.events import (
    EventValidationError,
    JsonObject,
    dumps_canonical,
    format_timestamp,
    parse_timestamp,
    require_int,
    require_str,
)
from agentbodycam.models.session import Session, SessionStatus, validate_session_id

ACTIVE_LOCK_FILENAME = "active.lock"
ACTIVE_FILENAME = "active.json"
ACTIVE_SCHEMA_VERSION = 1
DEFAULT_LEASE_TIMEOUT_SECONDS = 1.0
MAX_ACTIVE_BYTES = 4096

_LEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_FIELDS = frozenset(
    {"schema_version", "session_id", "pid", "started_at", "agentbodycam_version", "lease_id"}
)


class ActiveSessionError(StoreError):
    """Problem with the project's active-recording lease or metadata."""


class RecordingInProgressError(ActiveSessionError):
    """Another live recorder holds the project's active-recording lease."""

    def __init__(self, record: ActiveRecord | None) -> None:
        super().__init__("AgentBodycam is already recording this project")
        self.record = record


@dataclass(frozen=True, slots=True)
class ActiveRecord:
    """Contents of ``active.json``. No hostname, username, environment or absolute paths."""

    session_id: str
    pid: int
    started_at: datetime
    agentbodycam_version: str
    lease_id: str
    schema_version: int = ACTIVE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if require_int(self.schema_version, "schema_version") != ACTIVE_SCHEMA_VERSION:
            raise EventValidationError(f"unsupported active schema_version: {self.schema_version}")
        validate_session_id(self.session_id)
        if require_int(self.pid, "pid") <= 0:
            raise EventValidationError("pid must be positive")
        if not require_str(self.agentbodycam_version, "agentbodycam_version"):
            raise EventValidationError("agentbodycam_version must not be empty")
        if not _LEASE_ID_PATTERN.fullmatch(require_str(self.lease_id, "lease_id")):
            raise EventValidationError(f"invalid lease_id: {self.lease_id!r}")
        format_timestamp(self.started_at)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "pid": self.pid,
            "started_at": format_timestamp(self.started_at),
            "agentbodycam_version": self.agentbodycam_version,
            "lease_id": self.lease_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> ActiveRecord:
        if not isinstance(data, dict):
            raise EventValidationError("active metadata must be a JSON object")
        if set(data) != _ACTIVE_FIELDS:
            unknown = sorted(set(data) - _ACTIVE_FIELDS)
            missing = sorted(_ACTIVE_FIELDS - set(data))
            raise EventValidationError(
                f"active metadata fields mismatch (unknown: {unknown}, missing: {missing})"
            )
        return cls(
            schema_version=require_int(data["schema_version"], "schema_version"),
            session_id=validate_session_id(data["session_id"]),
            pid=require_int(data["pid"], "pid"),
            started_at=parse_timestamp(data["started_at"], "started_at"),
            agentbodycam_version=require_str(data["agentbodycam_version"], "agentbodycam_version"),
            lease_id=require_str(data["lease_id"], "lease_id"),
        )


def active_lock_path(store: EventStore) -> Path:
    return store.root / ACTIVE_LOCK_FILENAME


def active_metadata_path(store: EventStore) -> Path:
    return store.root / ACTIVE_FILENAME


def read_active_record(store: EventStore) -> ActiveRecord | None:
    """Parse ``active.json`` strictly. ``None`` if absent; raises if unsafe or malformed."""
    path = active_metadata_path(store)
    if not store.store_root_exists() or not store.existing_store_file(path):
        return None
    data = path.read_bytes()
    if len(data) > MAX_ACTIVE_BYTES:
        raise ActiveSessionError(f"{path}: active metadata is too large")
    try:
        return ActiveRecord.from_dict(strict_json_loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ActiveSessionError(f"{path}: {exc}") from exc


def _read_record_quietly(store: EventStore) -> ActiveRecord | None:
    with contextlib.suppress(StoreError, OSError):
        return read_active_record(store)
    return None


class ActiveLease:
    """Exclusive right to record this project, held until :meth:`release`.

    Obtain one with :func:`acquire_active_lease`.
    """

    def __init__(self, store: EventStore, lock: FileLock, stale: ActiveRecord | None) -> None:
        self.store = store
        self.lease_id = secrets.token_hex(16)
        self.stale_record = stale
        """Metadata left behind by a recorder that died without cleaning up, if any."""
        self.record: ActiveRecord | None = None
        self._lock = lock

    @property
    def held(self) -> bool:
        return self._lock.locked

    def publish(self, session: Session) -> ActiveRecord:
        """Atomically (re)write ``active.json`` to point at ``session``."""
        if not self.held:
            raise ActiveSessionError("the active-recording lease has been released")
        record = ActiveRecord(
            session_id=session.session_id,
            pid=os.getpid(),
            started_at=session.started_at,
            agentbodycam_version=__version__,
            lease_id=self.lease_id,
        )
        path = active_metadata_path(self.store)
        self.store.ensure_layout()
        self.store.existing_store_file(path)
        atomic_write_text(path, dumps_canonical(record.to_dict()) + "\n")
        self.record = record
        return record

    def release(self) -> None:
        """Remove our ``active.json`` (only if it is still ours), then drop the OS lock.

        Idempotent. The lock is released even if the metadata cannot be removed.
        """
        if not self.held:
            return
        try:
            path = active_metadata_path(self.store)
            current = _read_record_quietly(self.store)
            if current is not None and current.lease_id == self.lease_id:
                with contextlib.suppress(OSError):
                    path.unlink()
        finally:
            self._lock.release()

    def __enter__(self) -> ActiveLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def acquire_active_lease(
    store: EventStore, *, timeout: float = DEFAULT_LEASE_TIMEOUT_SECONDS
) -> ActiveLease:
    """Become the project's only recorder, or raise :class:`RecordingInProgressError`.

    Waits at most ``timeout`` seconds, which only matters if a discovery probe happens to
    hold the lock for an instant. Any ``active.json`` found once the lock is held is stale
    by definition (its writer no longer holds the lock); it is reported on the lease and
    replaced when :meth:`ActiveLease.publish` runs.
    """
    lock = store.open_lock(active_lock_path(store), timeout=timeout)
    try:
        lock.acquire()
    except LockTimeoutError:
        raise RecordingInProgressError(_read_record_quietly(store)) from None
    except FileLockError as exc:
        raise ActiveSessionError(str(exc)) from exc
    try:
        # Unsafe metadata paths fail here, before the caller creates a session.
        store.existing_store_file(active_metadata_path(store))
        stale = _read_record_quietly(store)
    except BaseException:
        lock.release()
        raise
    return ActiveLease(store, lock, stale)


class ActiveState(StrEnum):
    ACTIVE = "active"  # lock held, metadata valid, session exists and is recording
    INACTIVE = "inactive"  # no live recorder and no metadata
    STALE = "stale"  # metadata present (valid or not) but no live recorder holds the lock
    INVALID = "invalid"  # a live recorder holds the lock but the metadata cannot be trusted


@dataclass(frozen=True, slots=True)
class ActiveSessionInfo:
    state: ActiveState
    record: ActiveRecord | None = None
    session: Session | None = None
    reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state is ActiveState.ACTIVE


def _lock_held(store: EventStore) -> bool:
    path = active_lock_path(store)
    if not store.existing_store_file(path):
        return False
    try:
        return is_locked(path)
    except FileLockError as exc:
        raise ActiveSessionError(str(exc)) from exc


def _inspect(store: EventStore) -> ActiveSessionInfo:
    if not store.store_root_exists():
        return ActiveSessionInfo(ActiveState.INACTIVE)
    held = _lock_held(store)
    metadata_present = store.existing_store_file(active_metadata_path(store))
    try:
        record = read_active_record(store)
        problem = None
    except ActiveSessionError as exc:
        record, problem = None, f"malformed active metadata: {exc}"

    if not held:
        if not metadata_present:
            return ActiveSessionInfo(ActiveState.INACTIVE)
        return ActiveSessionInfo(
            ActiveState.STALE,
            record=record,
            reason=problem or "no live recorder holds the active-recording lock",
        )
    if record is None:
        return ActiveSessionInfo(
            ActiveState.INVALID,
            reason=problem or "the lock is held but no active metadata exists yet",
        )
    try:
        session = store.load_session(record.session_id)
    except SessionNotFoundError:
        return ActiveSessionInfo(
            ActiveState.INVALID,
            record=record,
            reason=f"referenced session {record.session_id} does not exist",
        )
    except (StoreError, EventValidationError) as exc:
        return ActiveSessionInfo(
            ActiveState.INVALID, record=record, reason=f"referenced session is unreadable: {exc}"
        )
    if session.status is not SessionStatus.RECORDING:
        return ActiveSessionInfo(
            ActiveState.INVALID,
            record=record,
            session=session,
            reason=f"referenced session is {session.status.value!r}, not 'recording'",
        )
    if session.started_at != record.started_at:
        return ActiveSessionInfo(
            ActiveState.INVALID,
            record=record,
            session=session,
            reason="active metadata does not match the referenced session's start time",
        )
    return ActiveSessionInfo(ActiveState.ACTIVE, record=record, session=session)


def get_active_session(project_root: Path) -> ActiveSessionInfo:
    """Which session is being recorded in ``project_root`` right now, if any. Read-only.

    Reports :attr:`ActiveState.ACTIVE` only when the active lock is held by a live recorder,
    ``active.json`` is valid, and it names an existing session whose status is
    ``recording``. Creates and modifies nothing. Raises :class:`StoreError` only for unsafe
    store paths (symlinks, junctions, escapes).
    """
    store = EventStore(project_root)
    info = _inspect(store)
    if info.state in (ActiveState.ACTIVE, ActiveState.INVALID) and not _lock_held(store):
        # The recorder stopped while we looked; describe what is left behind instead.
        info = _inspect(store)
    return info
