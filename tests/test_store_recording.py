"""Recording-only appends and the atomic stop: nothing may ever follow session.stopped."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from agentbodycam.core.filelock import FileLock
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import (
    EventStore,
    SessionClosedError,
    SessionNotRecordingError,
    SessionStateError,
    StoreLockError,
)
from agentbodycam.models.events import EventDraft, EventSource, EventType, EventValidationError
from agentbodycam.models.session import SessionStatus


def draft(n: int = 0, writer: int = 0) -> EventDraft:
    return EventDraft(
        type=EventType.FILE_MODIFIED,
        source=EventSource.FILESYSTEM,
        summary=f"writer {writer} event {n}",
        payload={"writer": writer, "n": n},
    )


def stopped_draft() -> EventDraft:
    return EventDraft(
        type=EventType.SESSION_STOPPED, source=EventSource.AGENTBODYCAM, summary="stopped"
    )


def recording_session(store: EventStore) -> str:
    sid = store.create_session().session_id
    store.mark_recording(sid)
    return sid


def assert_closed_log(store: EventStore, sid: str) -> None:
    events = store.read_events(sid)  # strict: contiguous sequence, valid JSON lines
    stops = [e for e in events if e.type is EventType.SESSION_STOPPED]
    assert len(stops) == 1
    assert events[-1].type is EventType.SESSION_STOPPED
    assert store.load_session(sid).status is SessionStatus.STOPPED


# -- API contract -------------------------------------------------------------------------


def test_recording_append_requires_recording_status(store: EventStore) -> None:
    sid = store.create_session().session_id
    with pytest.raises(SessionNotRecordingError, match="'created'"):
        store.append_recording_event(sid, draft())
    assert store.read_events(sid) == []
    store.mark_recording(sid)
    assert store.append_recording_event(sid, draft()).sequence == 1


def test_stop_appends_and_marks_atomically(store: EventStore) -> None:
    sid = recording_session(store)
    store.append_recording_event(sid, draft())
    event, session = store.stop_recording_session(sid, stopped_draft())
    assert event.type is EventType.SESSION_STOPPED and event.sequence == 2
    assert session.status is SessionStatus.STOPPED and session.ended_at is not None
    assert store.load_session(sid) == session
    assert_closed_log(store, sid)


def test_nothing_can_be_appended_after_stop(store: EventStore, project: Path) -> None:
    sid = recording_session(store)
    store.stop_recording_session(sid, stopped_draft())
    before = store.events_path(sid).read_bytes()
    with pytest.raises(SessionNotRecordingError):
        store.append_recording_event(sid, draft())
    with pytest.raises(SessionClosedError):
        store.append_event(sid, draft())  # even the developer API
    with pytest.raises(SessionClosedError):
        EventStore(project).append_event(sid, draft())  # and a fresh instance
    with pytest.raises(SessionNotRecordingError):
        store.stop_recording_session(sid, stopped_draft())
    assert store.events_path(sid).read_bytes() == before


def test_session_stopped_cannot_bypass_stop_api(store: EventStore) -> None:
    sid = recording_session(store)
    with pytest.raises(EventValidationError, match="stop_recording_session"):
        store.append_recording_event(sid, stopped_draft())
    with pytest.raises(EventValidationError):
        store.stop_recording_session(sid, draft())
    assert store.read_events(sid) == []


def test_crash_between_stop_append_and_mark_still_closes_the_log(
    store: EventStore, project: Path
) -> None:
    """session.stopped written but status left 'recording' (e.g. a crash): still closed."""
    sid = recording_session(store)
    EventStore(project).append_event(sid, stopped_draft())
    assert store.load_session(sid).status is SessionStatus.RECORDING
    with pytest.raises(SessionClosedError):
        store.append_recording_event(sid, draft())
    with pytest.raises(SessionClosedError):
        store.stop_recording_session(sid, stopped_draft())
    assert len(store.read_events(sid)) == 1


def test_plain_append_still_works_for_developer_sessions(store: EventStore) -> None:
    sid = store.create_session().session_id  # status 'created'
    assert store.append_event(sid, draft()).sequence == 1


def test_check_runs_while_the_event_lock_is_held(store: EventStore) -> None:
    sid = recording_session(store)
    held: list[bool] = []

    def check() -> None:
        probe = FileLock(store.event_lock_path(sid), timeout=0)
        held.append(not probe.try_acquire())

    store.append_recording_event(sid, draft(), check=check)
    assert held == [True]


def test_check_can_veto_the_append(store: EventStore) -> None:
    sid = recording_session(store)

    def check() -> None:
        raise RuntimeError("not the same recording")

    with pytest.raises(RuntimeError):
        store.append_recording_event(sid, draft(), check=check)
    assert store.read_events(sid) == []


def test_status_transitions_take_the_event_lock(project: Path) -> None:
    store = EventStore(project, lock_timeout=0.2)
    sid = recording_session(store)
    with FileLock(store.event_lock_path(sid)), pytest.raises(StoreLockError):
        store.mark_stopped(sid)
    assert store.load_session(sid).status is SessionStatus.RECORDING


def test_stop_and_status_check_cannot_interleave(project: Path) -> None:
    """A recording append blocked on the lock during a stop sees the stopped status."""
    store = EventStore(project)
    sid = recording_session(store)
    lock = FileLock(store.event_lock_path(sid))
    lock.acquire()
    outcome: list[BaseException | int] = []

    def append() -> None:
        try:
            outcome.append(EventStore(project).append_recording_event(sid, draft()).sequence)
        except SessionStateError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=append)
    thread.start()
    time.sleep(0.2)  # the appender is now waiting for the lock
    lock.release()
    EventStore(project).stop_recording_session(sid, stopped_draft())
    thread.join(timeout=30)
    # Either it won the lock first (and precedes session.stopped) or it was refused.
    events = store.read_events(sid)
    assert events[-1].type is EventType.SESSION_STOPPED
    if isinstance(outcome[0], int):
        assert outcome[0] < events[-1].sequence
    else:
        assert isinstance(outcome[0], SessionNotRecordingError)


# -- real concurrency ---------------------------------------------------------------------

HAMMER = r"""
import sys, time
from pathlib import Path
from agentbodycam.core.store import EventStore, SessionNotRecordingError
from agentbodycam.models.events import EventDraft, EventSource, EventType

project, sid, writer, go = sys.argv[1:5]
writer, go = int(writer), Path(go)
deadline = time.monotonic() + 60
while not go.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.002)
store = EventStore(Path(project), lock_timeout=60)
n = 0
while time.monotonic() < deadline:
    try:
        store.append_recording_event(sid, EventDraft(
            type=EventType.FILE_MODIFIED, source=EventSource.FILESYSTEM,
            summary=f"writer {writer} event {n}", payload={"writer": writer, "n": n}))
    except SessionNotRecordingError:
        print(n)
        sys.exit(0)
    n += 1
sys.exit(4)
"""


def _start_hammers(project: Path, sid: str, go: Path, count: int) -> list[subprocess.Popen[str]]:
    return [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
            [sys.executable, "-c", HAMMER, str(project), sid, str(writer), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for writer in range(count)
    ]


def _assert_hammered_log(
    store: EventStore, sid: str, procs: list[subprocess.Popen[str]], other_events: int
) -> None:
    appended: dict[int, int] = {}
    for writer, proc in enumerate(procs):
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err  # every writer was eventually refused cleanly
        appended[writer] = int(out.strip())
    assert_closed_log(store, sid)
    events = store.read_events(sid)
    origins = Counter(e.payload["writer"] for e in events if "writer" in e.payload)
    # Every append a writer saw succeed is in the log, before session.stopped.
    assert origins == Counter({w: n for w, n in appended.items() if n})
    assert len(events) == sum(appended.values()) + other_events
    for writer, n in appended.items():
        own = [e.payload["n"] for e in events if e.payload.get("writer") == writer]
        assert own == list(range(n))


def test_stop_under_concurrent_append_pressure(tmp_path: Path) -> None:
    project = tmp_path / "pressure prøject"
    project.mkdir()
    store = EventStore(project)
    sid = recording_session(store)
    go = tmp_path / "go"
    procs = _start_hammers(project, sid, go, 5)
    time.sleep(0.5)
    go.write_text("go", encoding="utf-8")
    deadline = time.monotonic() + 60
    while store.count_events(sid) < 100 and time.monotonic() < deadline:
        time.sleep(0.01)
    store.stop_recording_session(sid, stopped_draft())
    _assert_hammered_log(store, sid, procs, other_events=1)


def test_recorder_stop_under_concurrent_append_pressure(tmp_path: Path) -> None:
    project = tmp_path / "recorder pressure"
    project.mkdir()
    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    sid = recorder.start().session_id
    recorder.take_baseline()
    go = tmp_path / "go"
    procs = _start_hammers(project, sid, go, 4)
    time.sleep(0.5)
    go.write_text("go", encoding="utf-8")
    deadline = time.monotonic() + 60
    while store.count_events(sid) < 80 and time.monotonic() < deadline:
        recorder.scan_once()
    recorder.stop()
    _assert_hammered_log(store, sid, procs, other_events=2)  # session.started + .stopped


def test_threads_racing_a_stop(project: Path) -> None:
    store = EventStore(project)
    sid = recording_session(store)
    counts: dict[int, int] = {}
    errors: list[BaseException] = []
    start = threading.Event()

    def work(writer: int) -> None:
        own = EventStore(project)
        start.wait()
        n = 0
        try:
            while True:
                own.append_recording_event(sid, draft(n, writer))
                n += 1
        except SessionNotRecordingError:
            counts[writer] = n
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(6)]
    for thread in threads:
        thread.start()
    start.set()
    time.sleep(0.3)
    store.stop_recording_session(sid, stopped_draft())
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert_closed_log(store, sid)
    assert len(store.read_events(sid)) == sum(counts.values()) + 1
