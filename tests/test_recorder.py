"""Recorder lifecycle and change detection against real files in temporary directories."""

from __future__ import annotations

import hashlib
import json
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from agentbodycam.core.recorder import (
    GitStatus,
    Recorder,
    RecorderError,
    StopReason,
    stop_on_signals,
)
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventSource, EventType, EvidenceKind
from agentbodycam.models.session import SessionStatus
from recording_helpers import (
    all_events,
    bump_mtime,
    file_events,
    store_bytes,
    write,
)

MakeStore = Callable[[Path], EventStore]


@pytest.fixture
def recorder(store: EventStore) -> Recorder:
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    return rec


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- lifecycle ---------------------------------------------------------------------------


def test_start_creates_recording_session_with_started_event(store: EventStore) -> None:
    rec = Recorder(store)
    session = rec.start()
    assert session.status is SessionStatus.RECORDING
    assert session.ended_at is None
    assert store.load_session(session.session_id).status is SessionStatus.RECORDING
    events = all_events(store, session.session_id)
    assert len(events) == 1
    started = events[0]
    assert started.type is EventType.SESSION_STARTED
    assert started.source is EventSource.AGENTBODYCAM
    assert started.evidence is EvidenceKind.OBSERVED
    assert started.sequence == 1
    assert started.payload["recorder"] == "polling"
    assert started.payload["interval_seconds"] == 0.5
    policy = started.payload["ignore_policy"]
    assert isinstance(policy, dict)
    names, sensitive = policy["names"], policy["sensitive_patterns"]
    assert isinstance(names, list) and ".git" in names
    assert isinstance(sensitive, list) and ".env" in sensitive


def test_baseline_records_existing_files_without_events(project: Path, store: EventStore) -> None:
    write(project / "a.py", "a")
    write(project / "pkg" / "b.py", "b")
    rec = Recorder(store)
    sid = rec.start().session_id
    assert rec.take_baseline() == 2
    assert set(rec.snapshot) == {"a.py", "pkg/b.py"}
    assert file_events(all_events(store, sid)) == []
    assert rec.scan_once() == []


def test_scan_requires_start_and_baseline(store: EventStore) -> None:
    rec = Recorder(store)
    with pytest.raises(RecorderError):
        rec.scan_once()
    rec.start()
    with pytest.raises(RecorderError):
        rec.scan_once()
    with pytest.raises(RecorderError):
        rec.start()


def test_stop_appends_session_stopped_and_sets_ended_at(
    project: Path, store: EventStore, recorder: Recorder
) -> None:
    write(project / "new.txt", "x")
    recorder.scan_once()
    session = recorder.stop()
    assert session.status is SessionStatus.STOPPED
    assert session.ended_at is not None
    assert session.ended_at >= session.started_at
    reloaded = store.load_session(session.session_id)
    assert reloaded == session
    last = all_events(store, session.session_id)[-1]
    assert last.type is EventType.SESSION_STOPPED
    assert last.payload["reason"] == "requested"
    assert last.payload["file_events"] == {"created": 1, "modified": 0, "deleted": 0}
    assert last.payload["scans"] == 1
    assert session.ended_at >= last.timestamp


def test_cannot_use_recorder_after_stop(recorder: Recorder) -> None:
    recorder.stop()
    assert not recorder.is_active
    with pytest.raises(RecorderError):
        recorder.scan_once()
    with pytest.raises(RecorderError):
        recorder.stop()


def test_each_recording_creates_a_new_session(project: Path, store: EventStore) -> None:
    first = Recorder(store)
    first.start()
    first.take_baseline()
    write(project / "one.txt", "1")
    first.scan_once()
    first.stop()

    second = Recorder(store)
    second.start()
    assert second.take_baseline() == 1  # one.txt is now pre-existing
    second.stop()

    assert first.session.session_id != second.session.session_id
    sessions = store.list_sessions()
    assert len(sessions) == 2
    assert all(s.status is SessionStatus.STOPPED for s in sessions)
    assert file_events(all_events(store, second.session.session_id)) == []


# -- change detection --------------------------------------------------------------------


def test_file_created(project: Path, store: EventStore, recorder: Recorder) -> None:
    data = b"print('hello')\n"
    write(project / "src" / "app.py", data)
    [event] = recorder.scan_once()
    assert event.type is EventType.FILE_CREATED
    assert event.source is EventSource.FILESYSTEM
    assert event.evidence is EvidenceKind.OBSERVED
    assert event.path == "src/app.py"
    assert event.payload["sha256"] == sha(data)
    assert event.payload["hash_status"] == "complete"
    assert event.payload["size"] == len(data)
    assert isinstance(event.payload["mtime_ns"], int)
    assert isinstance(event.payload["mtime"], str)
    assert event.payload["detected_by"] == "polling"
    assert "previous" not in event.payload


def test_file_modified(project: Path, store: EventStore) -> None:
    target = write(project / "app.py", b"v1\n")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(target, b"version 2\n")
    [event] = rec.scan_once()
    assert event.type is EventType.FILE_MODIFIED
    assert event.path == "app.py"
    assert event.payload["sha256"] == sha(b"version 2\n")
    assert event.payload["change_evidence"] == "sha256"
    previous = event.payload["previous"]
    assert isinstance(previous, dict)
    assert previous["sha256"] == sha(b"v1\n")
    assert previous["size"] == 3


def test_same_size_content_change_detected_by_hash(project: Path, store: EventStore) -> None:
    target = write(project / "same.txt", b"aaaa")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(target, b"bbbb")
    bump_mtime(target)
    [event] = rec.scan_once()
    assert event.type is EventType.FILE_MODIFIED
    assert event.payload["size"] == 4


def test_touch_without_content_change_is_not_a_modification(
    project: Path, store: EventStore
) -> None:
    target = write(project / "same.txt", b"unchanged")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    bump_mtime(target)
    assert rec.scan_once() == []
    write(target, b"unchanged")  # rewritten with identical bytes
    assert rec.scan_once() == []


def test_atomic_replace_with_new_content_is_one_modification(
    project: Path, store: EventStore
) -> None:
    target = write(project / "config.toml", b"a = 1\n")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    tmp = write(project / "config.toml.tmp-save", b"a = 2\n")
    tmp.replace(target)
    assert file_events(rec.scan_once()) == [("file.modified", "config.toml")]


def test_file_deleted(project: Path, store: EventStore) -> None:
    data = b"old docs"
    target = write(project / "docs" / "old.md", data)
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    target.unlink()
    [event] = rec.scan_once()
    assert event.type is EventType.FILE_DELETED
    assert event.path == "docs/old.md"
    assert "sha256" not in event.payload
    previous = event.payload["previous"]
    assert isinstance(previous, dict)
    assert previous["sha256"] == sha(data)


def test_no_duplicate_events_when_nothing_changes(project: Path, recorder: Recorder) -> None:
    write(project / "a.txt", "a")
    assert len(recorder.scan_once()) == 1
    for _ in range(5):
        assert recorder.scan_once() == []
    assert recorder.stats.file_events == 1


def test_multiple_changes_in_one_scan_are_ordered(project: Path, store: EventStore) -> None:
    write(project / "keep.txt", "k")
    write(project / "edit.txt", "1")
    write(project / "gone.txt", "g")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(project / "z_new.txt", "z")
    write(project / "a_new.txt", "a")
    write(project / "edit.txt", "22")
    (project / "gone.txt").unlink()
    assert file_events(rec.scan_once()) == [
        ("file.created", "a_new.txt"),
        ("file.created", "z_new.txt"),
        ("file.modified", "edit.txt"),
        ("file.deleted", "gone.txt"),
    ]
    assert rec.stats.created == 2
    assert rec.stats.modified == 1
    assert rec.stats.deleted == 1


def test_changes_across_scans(project: Path, recorder: Recorder) -> None:
    target = project / "f.txt"
    write(target, "1")
    assert file_events(recorder.scan_once()) == [("file.created", "f.txt")]
    write(target, "22")
    assert file_events(recorder.scan_once()) == [("file.modified", "f.txt")]
    target.unlink()
    assert file_events(recorder.scan_once()) == [("file.deleted", "f.txt")]
    write(target, "333")
    assert file_events(recorder.scan_once()) == [("file.created", "f.txt")]


def test_rename_is_recorded_as_delete_plus_create(project: Path, store: EventStore) -> None:
    write(project / "old_name.py", "x = 1\n")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    (project / "old_name.py").rename(project / "new_name.py")
    events = rec.scan_once()
    assert sorted(file_events(events)) == [
        ("file.created", "new_name.py"),
        ("file.deleted", "old_name.py"),
    ]
    assert events[0].payload["sha256"] == sha(b"x = 1\n")


def test_directory_removal_deletes_each_file(project: Path, store: EventStore) -> None:
    write(project / "pkg" / "a.py", "a")
    write(project / "pkg" / "sub" / "b.py", "b")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    for path in (project / "pkg" / "sub" / "b.py", project / "pkg" / "a.py"):
        path.unlink()
    (project / "pkg" / "sub").rmdir()
    (project / "pkg").rmdir()
    assert file_events(rec.scan_once()) == [
        ("file.deleted", "pkg/a.py"),
        ("file.deleted", "pkg/sub/b.py"),
    ]


@pytest.mark.parametrize(
    "relative",
    [
        "with space/my file.py",
        "ünïcødé/naïve café.txt",
        "日本語/ファイル.md",
        "emoji/🚀 launch.txt",
    ],
)
def test_unicode_and_space_paths(project: Path, store: EventStore, relative: str) -> None:
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(project / relative, "content")
    [event] = rec.scan_once()
    assert event.path == relative
    reread = all_events(store, rec.session.session_id)[-1]
    assert reread.path == relative


def test_empty_file_hash(project: Path, recorder: Recorder) -> None:
    write(project / "empty", b"")
    [event] = recorder.scan_once()
    assert event.payload["sha256"] == sha(b"")
    assert event.payload["size"] == 0


def test_large_file_over_hash_limit_uses_metadata_evidence(
    project: Path, store: EventStore
) -> None:
    rec = Recorder(store, max_hash_bytes=1024)
    rec.start()
    rec.take_baseline()
    target = write(project / "big.bin", b"x" * 4096)
    [created] = rec.scan_once()
    assert created.payload["sha256"] is None
    assert created.payload["hash_status"] == "too_large"
    assert created.payload["size"] == 4096
    assert rec.scan_once() == []
    write(target, b"y" * 5000)
    [modified] = rec.scan_once()
    assert modified.type is EventType.FILE_MODIFIED
    assert modified.payload["change_evidence"] == "metadata"


# -- ignore rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        ".agentbodycam/sessions/fake/events.jsonl",
        ".git/HEAD",
        ".git/objects/ab/cdef",
        "sub/.git/config",
        "node_modules/pkg/index.js",
        ".venv/lib/site.py",
        "venv/bin/python",
        "__pycache__/m.cpython-311.pyc",
        "pkg/__pycache__/m.pyc",
        ".pytest_cache/v/cache",
        ".mypy_cache/3.11/x.json",
        ".ruff_cache/0.6/x",
        "notes.txt.swp",
    ],
)
def test_ignored_locations_produce_no_events(
    project: Path, recorder: Recorder, relative: str
) -> None:
    write(project / relative, "noise")
    assert recorder.scan_once() == []
    assert all(not p.startswith(relative.split("/")[0]) for p in recorder.snapshot)


@pytest.mark.parametrize(
    "contents",
    [
        "gitdir: ../elsewhere\n",
        "not-a-gitdir-line\n",
    ],
)
def test_git_worktree_file_is_ignored(project: Path, recorder: Recorder, contents: str) -> None:
    """A `.git` worktree pointer is ignored by the file scanner and is not a Git error.

    Malformed or dangling ``gitdir:`` targets are discovery failures (not a repository),
    not ``error.observed``. File recording of other paths must still work.
    """
    write(project / ".git", contents)
    events = recorder.scan_once()
    assert events == []
    assert file_events(events) == []
    assert recorder.git_status is GitStatus.NOT_A_REPOSITORY
    assert ".git" not in recorder.snapshot
    write(project / "tracked.txt", "hello")
    created = recorder.scan_once()
    assert file_events(created) == [("file.created", "tracked.txt")]
    assert all(e.type is not EventType.ERROR_OBSERVED for e in created)


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".ENV",
        ".env.local",
        ".env.production",
        "config/.env",
        "certs/server.pem",
        "certs/private.KEY",
        ".ssh/id_rsa",
        ".ssh/id_ed25519.pub",
        "secrets/store.p12",
    ],
)
def test_sensitive_files_never_recorded(
    project: Path, store: EventStore, recorder: Recorder, relative: str
) -> None:
    secret = b"SUPER_SECRET_TOKEN=abc123"
    target = write(project / relative, secret)
    assert recorder.scan_once() == []
    write(target, secret + b"-changed")
    assert recorder.scan_once() == []
    target.unlink()
    assert recorder.scan_once() == []
    recorder.stop()
    name = relative.rsplit("/", 1)[-1].casefold()
    for event in all_events(store, recorder.session.session_id):
        assert event.path is None or name not in event.path.casefold()
        assert name not in event.summary.casefold()
    written = store_bytes(project)
    assert b"SUPER_SECRET_TOKEN" not in written
    assert sha(secret).encode() not in written
    assert sha(secret + b"-changed").encode() not in written


def test_env_directory_contents_are_not_recorded(project: Path, recorder: Recorder) -> None:
    write(project / ".env" / "bin" / "activate", "x")
    assert recorder.scan_once() == []


# -- privacy -----------------------------------------------------------------------------


def test_file_contents_are_never_stored(project: Path, store: EventStore) -> None:
    marker = "CONTENT-MARKER-7f3a9 def secret_algorithm(): return 42"
    write(project / "existing.py", marker + "\nv1")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(project / "src" / "new.py", marker)
    write(project / "existing.py", marker + "\nv2")
    rec.scan_once()
    (project / "existing.py").unlink()
    rec.scan_once()
    rec.stop()
    written = store_bytes(project)
    assert b"CONTENT-MARKER-7f3a9" not in written
    assert b"secret_algorithm" not in written
    for event in all_events(store, rec.session.session_id):
        assert "CONTENT-MARKER" not in json.dumps(event.payload)


def test_recorder_writes_nothing_outside_the_store(tmp_path: Path, make_store: MakeStore) -> None:
    project = tmp_path / "proj"
    write(project / "a.txt", "a")
    write(tmp_path / "sibling.txt", "s")

    def outside_state() -> dict[str, tuple[int, int]]:
        return {
            str(p.relative_to(tmp_path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in tmp_path.rglob("*")
            if ".agentbodycam" not in p.parts and p.is_file()
        }

    before = outside_state()
    rec = Recorder(make_store(project))
    rec.start()
    rec.take_baseline()
    rec.scan_once()
    rec.stop()
    assert outside_state() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj", "sibling.txt"]


# -- ordering ----------------------------------------------------------------------------


def test_event_ordering_is_contiguous_and_bracketed(project: Path, store: EventStore) -> None:
    write(project / "a.txt", "a")
    rec = Recorder(store)
    rec.start()
    rec.take_baseline()
    write(project / "b.txt", "b")
    rec.scan_once()
    write(project / "a.txt", "aa")
    rec.scan_once()
    (project / "b.txt").unlink()
    rec.scan_once()
    rec.stop()
    events = all_events(store, rec.session.session_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert [e.type.value for e in events] == [
        "session.started",
        "file.created",
        "file.modified",
        "file.deleted",
        "session.stopped",
    ]
    assert [e.payload.get("scan") for e in events[1:4]] == [1, 2, 3]
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)
    monotonic = [e.monotonic_ns or 0 for e in events]
    assert all(monotonic) and monotonic == sorted(monotonic)


# -- run loop ----------------------------------------------------------------------------


class FakeTime:
    """Fake sleep/monotonic pair. ``actions`` run (in order) on successive sleeps."""

    def __init__(self, actions: list[Callable[[], object]] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.actions = list(actions or [])

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.actions:
            self.actions.pop(0)()


def test_run_with_duration_records_and_stops(project: Path, store: EventStore) -> None:
    clock = FakeTime(
        [lambda: write(project / "a.txt", "a"), lambda: write(project / "a.txt", "bb")]
    )
    rec = Recorder(store, interval=0.5, sleep=clock.sleep, monotonic=clock.monotonic)
    session = rec.run(duration=2.0)
    assert session.status is SessionStatus.STOPPED
    assert clock.sleeps == [0.5, 0.5, 0.5, 0.5]
    events = all_events(store, session.session_id)
    assert file_events(events) == [("file.created", "a.txt"), ("file.modified", "a.txt")]
    assert events[-1].payload["reason"] == "duration"
    assert events[-1].payload["scans"] == 4


def test_run_request_stop_performs_final_scan(project: Path, store: EventStore) -> None:
    rec: Recorder

    def change_then_stop() -> None:
        write(project / "last-second.txt", "x")
        rec.request_stop()

    clock = FakeTime([change_then_stop])
    rec = Recorder(store, sleep=clock.sleep, monotonic=clock.monotonic)
    session = rec.run()
    events = all_events(store, session.session_id)
    assert file_events(events) == [("file.created", "last-second.txt")]
    assert events[-1].payload["reason"] == "requested"
    assert session.status is SessionStatus.STOPPED


def test_run_keyboard_interrupt_stops_cleanly(project: Path, store: EventStore) -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt

    clock = FakeTime([lambda: write(project / "a.txt", "a"), interrupt])
    rec = Recorder(store, sleep=clock.sleep, monotonic=clock.monotonic)
    session = rec.run()
    assert session.status is SessionStatus.STOPPED
    assert session.ended_at is not None
    events = all_events(store, session.session_id)
    assert file_events(events) == [("file.created", "a.txt")]
    assert events[-1].type is EventType.SESSION_STOPPED
    assert events[-1].payload["reason"] == "interrupted"


def test_run_unexpected_error_still_closes_session(store: EventStore) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    clock = FakeTime([boom])
    rec = Recorder(store, sleep=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(RuntimeError, match="boom"):
        rec.run()
    session = store.load_session(rec.session.session_id)
    assert session.status is SessionStatus.STOPPED
    assert all_events(store, session.session_id)[-1].payload["reason"] == "error"


def test_run_real_sleep_short_duration(project: Path, store: EventStore) -> None:
    session = Recorder(store, interval=0.05).run(duration=0.2)
    assert session.status is SessionStatus.STOPPED
    assert all_events(store, session.session_id)[-1].payload["reason"] == "duration"


@pytest.mark.parametrize("interval", [0, -1.0])
def test_invalid_interval_rejected(store: EventStore, interval: float) -> None:
    with pytest.raises(ValueError, match="interval"):
        Recorder(store, interval=interval)


def test_on_event_callback_receives_every_event(project: Path, store: EventStore) -> None:
    seen: list[Event] = []
    rec = Recorder(store, on_event=seen.append)
    rec.start()
    rec.take_baseline()
    write(project / "x.txt", "x")
    rec.scan_once()
    rec.stop()
    assert seen == all_events(store, rec.session.session_id)


# -- signals -----------------------------------------------------------------------------


def test_sigint_requests_clean_stop_then_forces(store: EventStore) -> None:
    rec = Recorder(store)
    original = signal.getsignal(signal.SIGINT)
    with stop_on_signals(rec):
        assert signal.getsignal(signal.SIGINT) is not original
        signal.raise_signal(signal.SIGINT)
        assert rec.stop_requested
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    assert signal.getsignal(signal.SIGINT) is original


def test_sigint_during_run_stops_with_final_scan(project: Path, store: EventStore) -> None:
    def press_ctrl_c() -> None:
        write(project / "edited.txt", "x")
        signal.raise_signal(signal.SIGINT)

    clock = FakeTime([press_ctrl_c])
    rec = Recorder(store, sleep=clock.sleep, monotonic=clock.monotonic)
    with stop_on_signals(rec):
        session = rec.run()
    events = all_events(store, session.session_id)
    assert file_events(events) == [("file.created", "edited.txt")]
    assert events[-1].payload["reason"] == StopReason.REQUESTED.value
    assert session.status is SessionStatus.STOPPED
