"""Process- and thread-safe appends to one session (real processes, real OS locks)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from agentbodycam.core.filelock import FileLock
from agentbodycam.core.store import (
    EVENTS_FILENAME,
    CorruptEventLogError,
    EventStore,
    StoreLockError,
    UnsafePathError,
)
from agentbodycam.models.events import EventDraft, EventSource, EventType
from agentbodycam.models.session import InvalidSessionIdError
from recording_helpers import make_junction, try_symlink

WRITERS = 6
EVENTS_PER_WRITER = 150

WORKER = r"""
import sys, time
from pathlib import Path
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventDraft, EventSource, EventType

project, sid, writer, count, go = sys.argv[1:6]
writer, count, go = int(writer), int(count), Path(go)
deadline = time.monotonic() + 60
while not go.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.002)
store = EventStore(Path(project), lock_timeout=60)
for n in range(count):
    store.append_event(sid, EventDraft(
        type=EventType.FILE_MODIFIED,
        source=EventSource.FILESYSTEM,
        summary=f"writer {writer} event {n}",
        path=f"w{writer}/f{n}.txt",
        payload={"writer": writer, "n": n, "pad": "x" * (n % 7) * 300},
    ))
"""


def draft(writer: int, n: int) -> EventDraft:
    return EventDraft(
        type=EventType.FILE_MODIFIED,
        source=EventSource.FILESYSTEM,
        summary=f"writer {writer} event {n}",
        payload={"writer": writer, "n": n},
    )


def assert_complete_log(store: EventStore, sid: str, writers: int, per_writer: int) -> None:
    total = writers * per_writer
    raw = store.events_path(sid).read_bytes()
    lines = raw.split(b"\n")
    assert lines[-1] == b""
    assert len(lines) - 1 == total
    for line in lines[:-1]:  # every line is one whole JSON object
        assert isinstance(json.loads(line), dict)

    events = store.read_events(sid)  # strict parse: sequence, IDs, session, schema
    assert [e.sequence for e in events] == list(range(1, total + 1))
    assert len({e.event_id for e in events}) == total
    origins = Counter((e.payload["writer"], e.payload["n"]) for e in events)
    assert origins == Counter({(w, n): 1 for w in range(writers) for n in range(per_writer)})
    for writer in range(writers):  # each writer's events keep its own order
        own = [e.payload["n"] for e in events if e.payload["writer"] == writer]
        assert own == list(range(per_writer))


def test_many_processes_append_to_one_session(tmp_path: Path) -> None:
    project = tmp_path / "stress ünïcødé project"
    project.mkdir()
    store = EventStore(project)
    sid = store.create_session().session_id
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
            [
                sys.executable,
                "-c",
                WORKER,
                str(project),
                sid,
                str(writer),
                str(EVENTS_PER_WRITER),
                str(go),
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        for writer in range(WRITERS)
    ]
    time.sleep(0.5)  # let every interpreter start so the appends really overlap
    go.write_text("go", encoding="utf-8")
    failures = []
    for proc in procs:
        _, stderr = proc.communicate(timeout=300)
        if proc.returncode != 0:
            failures.append(stderr)
    assert failures == []
    assert_complete_log(store, sid, WRITERS, EVENTS_PER_WRITER)


def test_directory_created_by_another_process_in_between_is_accepted(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer creates locks/ after our existence check but before our mkdir."""
    store = EventStore(project)
    store.ensure_layout()
    store.locks_dir.mkdir()
    monkeypatch.setattr(store, "_existing_dir", lambda _path: False)
    store._ensure_dir(store.locks_dir)  # must not raise FileExistsError
    assert store.locks_dir.is_dir()


def test_many_threads_append_through_separate_and_shared_stores(project: Path) -> None:
    shared = EventStore(project)
    sid = shared.create_session().session_id
    errors: list[BaseException] = []

    def work(writer: int) -> None:
        store = shared if writer % 2 else EventStore(project)
        try:
            for n in range(60):
                store.append_event(sid, draft(writer, n))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert_complete_log(shared, sid, 6, 60)


def test_event_lock_layout(store: EventStore, project: Path) -> None:
    sid = store.create_session().session_id
    assert not store.locks_dir.exists()  # creating a session takes no lock
    store.append_event(sid, draft(0, 0))
    lock = project.resolve() / ".agentbodycam" / "locks" / f"{sid}.events.lock"
    assert store.event_lock_path(sid) == lock
    assert lock.is_file()
    assert sorted(p.name for p in store.locks_dir.iterdir()) == [lock.name]


@pytest.mark.parametrize("bad_id", ["..", "../x", "a/b", "C:\\x", "", "CON"])
def test_event_lock_path_requires_valid_session_id(store: EventStore, bad_id: str) -> None:
    with pytest.raises(InvalidSessionIdError):
        store.event_lock_path(bad_id)


def test_append_times_out_while_lock_is_held_elsewhere(project: Path) -> None:
    store = EventStore(project, lock_timeout=0.2)
    sid = store.create_session().session_id
    store.append_event(sid, draft(0, 0))
    before = store.events_path(sid).read_bytes()
    with FileLock(store.event_lock_path(sid)), pytest.raises(StoreLockError, match="timed out"):
        store.append_event(sid, draft(0, 1))
    assert store.events_path(sid).read_bytes() == before
    assert store.append_event(sid, draft(0, 1)).sequence == 2


def test_incremental_revalidation_detects_corrupt_foreign_append(
    store: EventStore, project: Path
) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(0, 0))
    EventStore(project).append_event(sid, draft(1, 0))
    path = store.events_path(sid)
    with path.open("ab") as fh:
        fh.write(b'{"not": "an event"}\n')
    corrupted = path.read_bytes()
    with pytest.raises(CorruptEventLogError, match="line 3"):
        store.append_event(sid, draft(0, 1))
    assert path.read_bytes() == corrupted


def test_incremental_revalidation_detects_duplicated_line(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(0, 0))
    path = store.events_path(sid)
    line = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(line)
    with pytest.raises(CorruptEventLogError, match="duplicate or out-of-order"):
        store.append_event(sid, draft(0, 1))


def test_replaced_log_is_fully_revalidated(store: EventStore, project: Path) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(0, 0))
    path = store.events_path(sid)
    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(path.read_bytes() + b"garbage\n")
    replacement.replace(path)
    with pytest.raises(CorruptEventLogError, match="line 2"):
        store.append_event(sid, draft(0, 1))


def test_symlinked_locks_dir_is_rejected(store: EventStore, tmp_path: Path) -> None:
    sid = store.create_session().session_id
    outside = tmp_path / "outside"
    outside.mkdir()
    if not try_symlink(store.locks_dir, outside, directory=True):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        store.append_event(sid, draft(0, 0))
    assert list(outside.iterdir()) == []
    assert store.events_path(sid).read_bytes() == b""


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junctioned_locks_dir_is_rejected(store: EventStore, tmp_path: Path) -> None:
    sid = store.create_session().session_id
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(store.locks_dir, outside)
    with pytest.raises(UnsafePathError):
        store.append_event(sid, draft(0, 0))
    assert list(outside.iterdir()) == []


def test_symlinked_event_lock_file_is_rejected(store: EventStore, tmp_path: Path) -> None:
    sid = store.create_session().session_id
    store.locks_dir.mkdir()
    target = tmp_path / "outside.lock"
    if not try_symlink(store.event_lock_path(sid), target):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        store.append_event(sid, draft(0, 0))
    assert not target.exists()


def test_event_lock_that_is_a_directory_is_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.locks_dir.mkdir()
    store.event_lock_path(sid).mkdir()
    with pytest.raises(UnsafePathError):
        store.append_event(sid, draft(0, 0))
    assert (store.session_dir(sid) / EVENTS_FILENAME).read_bytes() == b""


def test_lock_paths_must_be_inside_the_store(store: EventStore, tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        store.open_lock(tmp_path / "elsewhere.lock")
    with pytest.raises(UnsafePathError):
        store.open_lock(store.sessions_dir / "x.lock")
