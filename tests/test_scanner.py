"""Scanner safety, hashing and race handling with real files in temporary directories."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agentbodycam.core import scanner as scanner_mod
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.scanner import (
    HASH_CHUNK_BYTES,
    ChangeKind,
    FileState,
    HashStatus,
    ProjectScanner,
    ScanIssue,
    diff_snapshots,
    hash_file,
)
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType
from recording_helpers import (
    all_events,
    file_events,
    make_junction,
    store_bytes,
    try_symlink,
    write,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")


def hash_status(rec: Recorder, path: str) -> HashStatus:
    return rec.snapshot[path].hash_status


def started(store: EventStore) -> Recorder:
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    return rec


# -- hashing -----------------------------------------------------------------------------


def test_hash_streams_multi_chunk_file(tmp_path: Path) -> None:
    data = os.urandom(HASH_CHUNK_BYTES * 2 + 12345)
    target = write(tmp_path / "big.bin", data)
    digest, status = hash_file(str(target), target.lstat(), max_bytes=10 * HASH_CHUNK_BYTES)
    assert status is HashStatus.COMPLETE
    assert digest == hashlib.sha256(data).hexdigest()


def test_hash_respects_size_limit(tmp_path: Path) -> None:
    target = write(tmp_path / "big.bin", b"x" * 100)
    assert hash_file(str(target), target.lstat(), max_bytes=99) == (None, HashStatus.TOO_LARGE)
    assert hash_file(str(target), target.lstat(), max_bytes=100)[1] is HashStatus.COMPLETE


def test_hash_detects_file_replaced_after_stat(tmp_path: Path) -> None:
    target = write(tmp_path / "f.txt", b"one")
    stale = target.lstat()
    write(tmp_path / "other.txt", b"two").replace(target)
    with pytest.raises(scanner_mod._Vanished):
        hash_file(str(target), stale, max_bytes=1024)


def test_hash_detects_file_changed_after_stat(tmp_path: Path) -> None:
    target = write(tmp_path / "f.txt", b"one")
    stale = target.lstat()
    with target.open("ab") as fh:
        fh.write(b" and more")
    with pytest.raises(scanner_mod._Unstable):
        hash_file(str(target), stale, max_bytes=1024)


def test_hash_missing_file_is_vanished(tmp_path: Path) -> None:
    target = write(tmp_path / "f.txt", b"one")
    st = target.lstat()
    target.unlink()
    with pytest.raises(scanner_mod._Vanished):
        hash_file(str(target), st, max_bytes=1024)


@WINDOWS_ONLY
def test_hashing_never_blocks_user_delete_or_rename(tmp_path: Path) -> None:
    target = write(tmp_path / "busy.txt", b"data")
    fd = scanner_mod._open_no_follow(str(target))
    try:
        moved = target.rename(tmp_path / "moved.txt")
        moved.unlink()
        assert not moved.exists()
        assert not target.exists()
    finally:
        os.close(fd)


# -- symlink / junction safety -----------------------------------------------------------


def test_symlinks_are_never_followed(
    tmp_path: Path, make_store: Callable[[Path], EventStore]
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    write(outside / "secret.txt", "outside data")
    write(project / "real.txt", "inside")
    if not try_symlink(project / "link_dir", outside, directory=True):
        pytest.skip("symlinks are not available on this system")
    assert try_symlink(project / "link_file", outside / "secret.txt")
    assert try_symlink(project / "inner_link", project / "real.txt")
    assert try_symlink(project / "dangling", tmp_path / "nowhere")

    scanner = ProjectScanner(project)
    result = scanner.scan({})
    assert set(result.files) == {"real.txt"}
    assert result.issues == []

    store = make_store(project)
    rec = started(store)
    write(outside / "added-later.txt", "x")
    write(outside / "secret.txt", "changed outside")
    assert rec.scan_once() == []
    rec.stop()
    assert b"outside" not in store_bytes(project)
    assert b"link_" not in store_bytes(project)


@WINDOWS_ONLY
def test_junctions_are_never_followed(
    tmp_path: Path, make_store: Callable[[Path], EventStore]
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    write(outside / "secret.txt", "outside data")
    write(project / "real.txt", "inside")
    make_junction(project / "junction", outside)
    assert (project / "junction" / "secret.txt").exists()  # the junction really works

    assert set(ProjectScanner(project).scan({}).files) == {"real.txt"}
    rec = started(make_store(project))
    write(outside / "new.txt", "x")
    assert rec.scan_once() == []


def test_file_replaced_by_symlink_is_not_followed(
    tmp_path: Path, make_store: Callable[[Path], EventStore]
) -> None:
    project = tmp_path / "project"
    outside = write(tmp_path / "outside.txt", "outside data")
    target = write(project / "file.txt", "inside")
    rec = started(make_store(project))
    target.unlink()
    if not try_symlink(target, outside):
        pytest.skip("symlinks are not available on this system")
    assert file_events(rec.scan_once()) == [("file.deleted", "file.txt")]


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX-only")
def test_fifo_is_skipped_without_blocking(project: Path, store: EventStore) -> None:
    if sys.platform != "win32":
        os.mkfifo(project / "pipe")
        rec = started(store)
        write(project / "a.txt", "a")
        assert file_events(rec.scan_once()) == [("file.created", "a.txt")]
        assert "pipe" not in rec.snapshot


# -- races and unreadable files ----------------------------------------------------------


@contextmanager
def patched_hash(
    monkeypatch: pytest.MonkeyPatch, before_hash: Callable[[str], None]
) -> Iterator[list[str]]:
    calls: list[str] = []
    real = scanner_mod.hash_file

    def wrapper(path: str, expected: os.stat_result, max_bytes: int) -> Any:
        calls.append(path)
        before_hash(path)
        return real(path, expected, max_bytes)

    monkeypatch.setattr(scanner_mod, "hash_file", wrapper)
    yield calls
    monkeypatch.setattr(scanner_mod, "hash_file", real)


def test_new_file_disappearing_during_scan(
    project: Path, store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = started(store)
    write(project / "flash.tmp", "short-lived")
    write(project / "stays.txt", "x")

    def vanish(path: str) -> None:
        if path.endswith("flash.tmp"):
            Path(path).unlink()

    with patched_hash(monkeypatch, vanish):
        events = rec.scan_once()
    assert file_events(events) == [("file.created", "stays.txt")]
    assert rec.scan_once() == []


def test_known_file_vanishing_mid_hash_is_not_misreported(
    project: Path, store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write(project / "doc.md", "v1")
    rec = started(store)
    write(target, "version two")

    with patched_hash(monkeypatch, lambda path: Path(path).unlink()):
        assert rec.scan_once() == []  # state unknown this scan: no guess is recorded
    assert file_events(rec.scan_once()) == [("file.deleted", "doc.md")]


def test_file_replaced_mid_hash_is_resolved_next_scan(
    project: Path, store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = write(project / "doc.md", "v1")
    rec = started(store)
    write(target, "v2!")
    replacement = write(project / "doc.md.new", "final content")
    replaced: list[bool] = []

    def swap(path: str) -> None:
        if not replaced:
            replacement.replace(path)
            replaced.append(True)

    with patched_hash(monkeypatch, swap):
        assert rec.scan_once() == []
        [event] = rec.scan_once()
    assert event.type is EventType.FILE_MODIFIED
    assert event.payload["sha256"] == hashlib.sha256(b"final content").hexdigest()


def test_transient_permission_error_is_retried_silently(
    project: Path, store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = started(store)
    write(project / "locked.txt", "data")
    failures = [PermissionError(errno.EACCES, "Permission denied")]

    def maybe_fail(_path: str) -> None:
        if failures:
            raise failures.pop()

    with patched_hash(monkeypatch, maybe_fail):
        assert rec.scan_once() == []
        assert file_events(rec.scan_once()) == [("file.created", "locked.txt")]
    assert rec.stats.errors == 0


@contextmanager
def unreadable(path: Path) -> Iterator[None]:
    """Make ``path`` genuinely unreadable for this process, using OS mechanisms."""
    if sys.platform == "win32":
        import msvcrt

        with path.open("r+b") as fh:
            size = path.stat().st_size
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, size)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, size)
    else:
        if os.geteuid() == 0:
            pytest.skip("root can read files regardless of permissions")
        path.chmod(0)
        try:
            yield
        finally:
            path.chmod(0o644)


def test_unreadable_file_reports_one_safe_error_and_recording_continues(
    project: Path, store: EventStore
) -> None:
    target = write(project / "private data.bin", b"TOP-SECRET-BYTES" * 64)
    rec = started(store)
    write(target, b"TOP-SECRET-BYTES" * 65)
    with unreadable(target):
        write(target.parent / "other.txt", "o")
        first = rec.scan_once()  # hash fails once: deferred, nothing guessed
        second = rec.scan_once()  # still failing: observed error + metadata evidence
        third = rec.scan_once()
        assert file_events(first) == [("file.created", "other.txt")]
        assert file_events(second) == [("file.modified", "private data.bin")]
        [modified] = [e for e in second if e.type is EventType.FILE_MODIFIED]
        assert modified.payload["change_evidence"] == "metadata"
        assert modified.payload["sha256"] is None
        assert modified.payload["hash_status"] == "unreadable"
        assert third == []
        errors = [e for e in first + second + third if e.type is EventType.ERROR_OBSERVED]
        assert len(errors) == 1
        [error] = errors
        assert error.path == "private data.bin"
        assert error.payload["operation"] == "hash"
        assert error.payload["error"] == "PermissionError"
        assert isinstance(error.payload["errno"], int)
        assert str(project) not in json.dumps(error.to_dict())
        write(target.parent / "other.txt", "changed")
        assert file_events(rec.scan_once()) == [("file.modified", "other.txt")]
    rec.stop()
    assert b"TOP-SECRET-BYTES" not in store_bytes(project)
    assert all_events(store, rec.session.session_id)[-1].payload["errors_observed"] == 1


def test_file_unreadable_at_baseline_is_not_reported_as_created(
    project: Path, store: EventStore
) -> None:
    target = write(project / "locked.bin", b"z" * 32)
    with unreadable(target):
        rec = Recorder(store)
        rec.start()
        rec.take_baseline()
        assert "locked.bin" in rec.snapshot
        assert hash_status(rec, "locked.bin") is HashStatus.UNREADABLE
        assert file_events(rec.scan_once()) == []
    # readable again with unchanged metadata: hash is filled in, no spurious event
    assert rec.scan_once() == []
    assert hash_status(rec, "locked.bin") is HashStatus.COMPLETE


def test_unlistable_directory_keeps_known_files(
    project: Path, store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(project / "locked" / "a.txt", "a")
    write(project / "open" / "b.txt", "b")
    rec = started(store)
    real_scandir = os.scandir

    def scandir(path: Any) -> Any:
        if Path(path).name == "locked":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    events = rec.scan_once()
    assert file_events(events) == []  # not misreported as deleted
    [error] = events
    assert error.type is EventType.ERROR_OBSERVED
    assert error.path == "locked"
    assert error.payload["operation"] == "list_directory"
    assert rec.scan_once() == []  # reported once, not every scan
    monkeypatch.setattr(os, "scandir", real_scandir)
    (project / "locked" / "a.txt").unlink()
    assert file_events(rec.scan_once()) == [("file.deleted", "locked/a.txt")]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_real_unreadable_directory(project: Path, store: EventStore) -> None:
    if sys.platform != "win32":
        if os.geteuid() == 0:
            pytest.skip("root can list directories regardless of permissions")
        write(project / "locked" / "a.txt", "a")
        rec = started(store)
        (project / "locked").chmod(0)
        try:
            events = rec.scan_once()
        finally:
            (project / "locked").chmod(0o755)
        assert [e.type for e in events] == [EventType.ERROR_OBSERVED]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids '\\' in file names")
def test_unrepresentable_name_is_skipped_without_path(project: Path, store: EventStore) -> None:
    rec = started(store)
    write(project / "back\\slash.txt", "x")
    events = rec.scan_once()
    assert [e.type for e in events] == [EventType.ERROR_OBSERVED]
    assert events[0].path is None


def test_scan_issue_never_contains_absolute_path(tmp_path: Path) -> None:
    exc = PermissionError(errno.EACCES, "Permission denied", str(tmp_path / "x"))
    issue = ScanIssue.from_os_error("hash", "x", exc)
    assert str(tmp_path) not in repr(issue)
    assert issue == ScanIssue("hash", "PermissionError", "x", errno.EACCES, "Permission denied")


def test_scanner_rejects_bad_settings(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProjectScanner(tmp_path, max_hash_bytes=-1)
    with pytest.raises(ValueError):
        ProjectScanner(tmp_path, deferral_limit=0)


# -- diff --------------------------------------------------------------------------------


def state(sha: str | None, size: int = 1, mtime: int = 1, file_id: int = 1) -> FileState:
    status = HashStatus.COMPLETE if sha else HashStatus.TOO_LARGE
    return FileState(size, mtime, file_id, sha, status)


def test_diff_uses_hash_when_available() -> None:
    before = {"a": state("h1"), "b": state("h2")}
    after = {"a": state("h1", mtime=9, file_id=7), "b": state("h3")}
    [change] = diff_snapshots(before, after)
    assert (change.kind, change.path, change.evidence) == (ChangeKind.MODIFIED, "b", "sha256")


def test_diff_falls_back_to_metadata_without_hash() -> None:
    before = {"a": state(None, size=1), "b": state(None)}
    after = {"a": state(None, size=2), "b": state(None, file_id=3)}
    [change] = diff_snapshots(before, after)
    assert (change.path, change.evidence) == ("a", "metadata")
