"""POSIX owner-only permissions for the AgentBodycam evidence store (ABD-006).

These tests assert mode bits on a real POSIX filesystem. They are skipped on Windows,
where AgentBodycam does not treat numeric modes as an access-control mechanism.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agentbodycam.core.active import acquire_active_lease
from agentbodycam.core.filelock import FileLock
from agentbodycam.core.store import (
    EVENTS_FILENAME,
    GITIGNORE_FILENAME,
    SESSION_FILENAME,
    STORE_DIR_MODE,
    STORE_DIRNAME,
    STORE_FILE_MODE,
    EventStore,
    atomic_write_text,
)
from agentbodycam.models.events import EventDraft, EventSource, EventType

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX store mode bits")

StoreFactory = Callable[[Path], EventStore]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _assert_dir(path: Path) -> None:
    assert path.is_dir() and not path.is_symlink()
    assert _mode(path) == STORE_DIR_MODE, f"{path}: {_mode(path):04o}"


def _assert_file(path: Path) -> None:
    assert path.is_file() and not path.is_symlink()
    assert _mode(path) == STORE_FILE_MODE, f"{path}: {_mode(path):04o}"


def _assert_private_store(store: EventStore, session_id: str) -> None:
    _assert_dir(store.root)
    _assert_dir(store.sessions_dir)
    _assert_dir(store.session_dir(session_id))
    _assert_file(store.root / GITIGNORE_FILENAME)
    _assert_file(store.session_dir(session_id) / SESSION_FILENAME)
    _assert_file(store.session_dir(session_id) / EVENTS_FILENAME)


@pytest.fixture
def posix_umask() -> Iterator[int]:
    """Run under umask 0o022 unless the test overrides it."""
    previous = os.umask(0o022)
    try:
        yield 0o022
    finally:
        os.umask(previous)


def test_new_store_is_owner_only_under_umask_022(
    project: Path, make_store: StoreFactory, posix_umask: int
) -> None:
    assert posix_umask == 0o022
    store = make_store(project)
    session = store.create_session()
    store.ensure_layout()
    store._ensure_dir(store.locks_dir)
    lock_path = store.event_lock_path(session.session_id)
    with FileLock(lock_path):
        pass
    recording = store.mark_recording(session.session_id)
    with acquire_active_lease(store) as lease:
        lease.publish(recording)
        _assert_file(store.root / "active.json")
    _assert_private_store(store, session.session_id)
    _assert_dir(store.locks_dir)
    _assert_file(lock_path)
    _assert_file(store.root / "active.lock")


def test_new_store_is_owner_only_under_umask_000(project: Path, make_store: StoreFactory) -> None:
    previous = os.umask(0o000)
    try:
        # Fixture created the project before umask 000; force a known permissive root.
        project.chmod(0o777)
        store = make_store(project)
        session = store.create_session()
        store._ensure_dir(store.locks_dir)
        lock_path = store.event_lock_path(session.session_id)
        with FileLock(lock_path):
            pass
        recording = store.mark_recording(session.session_id)
        with acquire_active_lease(store) as lease:
            lease.publish(recording)
            _assert_file(store.root / "active.json")
        _assert_private_store(store, session.session_id)
        _assert_dir(store.locks_dir)
        _assert_file(lock_path)
        _assert_file(store.root / "active.lock")
        # AgentBodycam must never chmod the project root.
        assert _mode(project) == 0o777
    finally:
        os.umask(previous)


def test_append_and_metadata_replace_keep_owner_only(
    project: Path, make_store: StoreFactory, posix_umask: int
) -> None:
    del posix_umask
    store = make_store(project)
    session = store.create_session()
    store.mark_recording(session.session_id)
    store.append_recording_event(
        session.session_id,
        EventDraft(
            type=EventType.FILE_CREATED,
            source=EventSource.FILESYSTEM,
            summary="Created a.txt",
            path="a.txt",
            payload={},
        ),
    )
    stopped = store.mark_stopped(session.session_id)
    assert stopped.status.value == "stopped"
    _assert_private_store(store, session.session_id)
    assert (store.session_dir(session.session_id) / EVENTS_FILENAME).stat().st_size > 0


def test_atomic_replacement_preserves_0600(tmp_path: Path, posix_umask: int) -> None:
    del posix_umask
    path = tmp_path / "session.json"
    atomic_write_text(path, '{"a": 1}\n')
    _assert_file(path)
    # Make it briefly permissive, then replace again — the new inode must be 0600.
    path.chmod(0o644)
    assert _mode(path) == 0o644
    atomic_write_text(path, '{"a": 2}\n')
    _assert_file(path)
    assert path.read_text(encoding="utf-8") == '{"a": 2}\n'


def test_existing_permissive_store_is_hardened(
    project: Path, make_store: StoreFactory, posix_umask: int
) -> None:
    """A synthetic valid store left at 0755/0644 is tightened on the legitimate open path."""
    del posix_umask
    store = make_store(project)
    session = store.create_session()
    sid = session.session_id
    # Intentionally open the store up as an older/umask-022 layout would have.
    for directory in (store.root, store.sessions_dir, store.session_dir(sid)):
        directory.chmod(0o755)
    for path in (
        store.root / GITIGNORE_FILENAME,
        store.session_dir(sid) / SESSION_FILENAME,
        store.session_dir(sid) / EVENTS_FILENAME,
    ):
        path.chmod(0o644)
    assert _mode(store.root) == 0o755
    assert _mode(store.session_dir(sid) / EVENTS_FILENAME) == 0o644

    # Legitimate initialization / open path.
    store.ensure_layout()
    loaded = store.load_session(sid)
    assert loaded.session_id == sid
    store.append_event(
        sid,
        EventDraft(
            type=EventType.FILE_MODIFIED,
            source=EventSource.FILESYSTEM,
            summary="Modified a.txt",
            path="a.txt",
            payload={},
        ),
    )
    _assert_private_store(store, sid)


def test_lock_file_creation_is_0600(
    project: Path, make_store: StoreFactory, posix_umask: int
) -> None:
    del posix_umask
    previous = os.umask(0o000)
    try:
        store = make_store(project)
        store.ensure_layout()
        store._ensure_dir(store.locks_dir)
        path = store.root / "probe.lock"
        with FileLock(path):
            _assert_file(path)
        # Existing permissive lock file is hardened on the next open.
        path.chmod(0o644)
        assert _mode(path) == 0o644
        with FileLock(path):
            _assert_file(path)
    finally:
        os.umask(previous)


def test_project_root_mode_is_not_changed(project: Path, make_store: StoreFactory) -> None:
    project.chmod(0o755)
    before = _mode(project)
    store = make_store(project)
    store.create_session()
    assert _mode(project) == before
    assert (project / STORE_DIRNAME).is_dir()
    _assert_dir(project / STORE_DIRNAME)
