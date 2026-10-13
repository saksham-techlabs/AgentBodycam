"""``agentbodycam run`` across real processes: recorder, wrappers, children and signals."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventType
from command_helpers import (
    CHECK_TYPES,
    ENV,
    RecorderProcess,
    cli,
    command_events,
    new_group_flags,
    py,
    wait_until,
)
from recording_helpers import FILE_TYPES, write

WINDOWS = sys.platform == "win32"
STATUS_CONTROL_C_EXIT = 0xC000013A


def wrapper(
    project: Path,
    *argv: str,
    cwd: Path | None = None,
    creationflags: int = 0,
    start_new_session: bool = False,
    check: str | None = None,
) -> subprocess.Popen[str]:
    options = [] if check is None else ["--check", check]
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
        cli("run", "--project", str(project), *options, "--", *argv),
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=ENV,
        cwd=cwd,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )


def index_of(events: list[Event], predicate: object) -> int:
    return next(i for i, e in enumerate(events) if predicate(e))  # type: ignore[operator]


@pytest.fixture
def recorder_process(tmp_path: Path) -> Iterator[RecorderProcess]:
    project = tmp_path / "e2e ünïcødé project"
    project.mkdir()
    write(project / "app.py", "v1\n")
    rec = RecorderProcess(project)
    try:
        yield rec
    finally:
        rec.kill()


def test_end_to_end_record_and_wrapped_command(recorder_process: RecorderProcess) -> None:
    project = recorder_process.project
    sid = recorder_process.session_id
    store = EventStore(project)
    edit = "import pathlib; pathlib.Path('app.py').write_text('v2\\n'); print('child done')"
    proc = wrapper(project, *py(edit), cwd=project)
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    assert (project / "app.py").read_text() == "v2\n"

    def file_seen() -> bool:
        return any(
            e.type is EventType.FILE_MODIFIED and e.path == "app.py" for e in store.read_events(sid)
        )

    wait_until(file_seen, what="file.modified app.py")
    recorder_process.stop()

    events = store.read_events(sid)  # strict parse of the whole log
    types = [e.type for e in events]
    assert types[0] is EventType.SESSION_STARTED
    assert types[-1] is EventType.SESSION_STOPPED
    assert types.count(EventType.SESSION_STOPPED) == 1
    started = index_of(events, lambda e: e.type is EventType.COMMAND_STARTED)
    finished = index_of(events, lambda e: e.type is EventType.COMMAND_FINISHED)
    modified = index_of(events, lambda e: e.type is EventType.FILE_MODIFIED)
    assert 0 < started < finished < len(events) - 1
    # The change happened after command.started was appended, so it is detected after it;
    # relative to command.finished the order depends on polling and is not asserted.
    assert started < modified < len(events) - 1
    assert events[started].payload["command_id"] == events[finished].payload["command_id"]
    assert events[finished].payload["exit_code"] == 0
    assert events[started].payload["working_directory"] == "."


def test_concurrent_wrapped_commands(recorder_process: RecorderProcess) -> None:
    project = recorder_process.project
    sid = recorder_process.session_id
    store = EventStore(project)
    count = 6
    body = (
        "import pathlib,random,sys,time\n"
        "i = int(sys.argv[1])\n"
        "pathlib.Path(f'out{i}.txt').write_text(str(i))\n"
        "time.sleep(random.random() * 0.3)\n"
        "sys.exit(10 + i)\n"
    )
    procs = [wrapper(project, *py(body, str(i)), cwd=project) for i in range(count)]
    codes = []
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        codes.append(proc.returncode)
        assert "warning" not in err and "error" not in err, err
    assert codes == [10 + i for i in range(count)]
    wait_until(
        lambda: sum(e.type in FILE_TYPES for e in store.read_events(sid)) >= count,
        what="file events",
    )
    recorder_process.stop()

    events = store.read_events(sid)
    assert events[-1].type is EventType.SESSION_STOPPED
    commands = command_events(store, sid)
    starts = {e.payload["command_id"]: e for e in commands if e.type is EventType.COMMAND_STARTED}
    ends = {e.payload["command_id"]: e for e in commands if e.type is EventType.COMMAND_FINISHED}
    assert len(starts) == len(ends) == count  # unique IDs, one finish per start
    assert starts.keys() == ends.keys()
    for command_id, start in starts.items():
        assert start.sequence < ends[command_id].sequence
    exit_codes = sorted(e.payload["exit_code"] for e in ends.values())  # type: ignore[type-var]
    assert exit_codes == [10 + i for i in range(count)]


def test_end_to_end_failing_then_passing_check(recorder_process: RecorderProcess) -> None:
    """record -> failing pytest -> file edit -> passing pytest -> stop, all real processes."""
    project = recorder_process.project
    sid = recorder_process.session_id
    store = EventStore(project)
    write(project / "test_app.py", "from app_value import VALUE\n\ndef test_value():\n")
    with (project / "test_app.py").open("a", encoding="utf-8") as handle:
        handle.write("    assert VALUE == 2\n")
    write(project / "app_value.py", "VALUE = 1\n")
    pytest_argv = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

    failing = wrapper(project, *pytest_argv, cwd=project)
    _, err = failing.communicate(timeout=120)
    assert failing.returncode == 1, err
    write(project / "app_value.py", "VALUE = 2\n")
    wait_until(
        lambda: any(
            e.type is EventType.FILE_MODIFIED and e.path == "app_value.py"
            for e in store.read_events(sid)
        ),
        what="file.modified app_value.py",
    )
    passing = wrapper(project, *pytest_argv, cwd=project)
    _, err = passing.communicate(timeout=120)
    assert passing.returncode == 0, err
    recorder_process.stop()

    events = store.read_events(sid)
    relevant = [
        (e.type, e.path)
        for e in events
        if e.type in CHECK_TYPES
        or e.type in (EventType.COMMAND_STARTED, EventType.COMMAND_FINISHED)
        or (e.type is EventType.FILE_MODIFIED and e.path == "app_value.py")
        or e.type in (EventType.SESSION_STARTED, EventType.SESSION_STOPPED)
    ]
    assert relevant == [
        (EventType.SESSION_STARTED, None),
        (EventType.COMMAND_STARTED, None),
        (EventType.COMMAND_FINISHED, None),
        (EventType.CHECK_FAILED, None),
        (EventType.FILE_MODIFIED, "app_value.py"),
        (EventType.COMMAND_STARTED, None),
        (EventType.COMMAND_FINISHED, None),
        (EventType.CHECK_PASSED, None),
        (EventType.SESSION_STOPPED, None),
    ]
    failed, passed = [e for e in events if e.type in CHECK_TYPES]
    assert failed.summary == "Test failed: pytest (exit 1)"
    assert passed.summary == "Test passed: pytest"
    for check in (failed, passed):
        assert check.payload["classification"] == "automatic"
        assert check.payload["kind"] == "test"
    # Observed outcomes only: nothing links the edit to the result.
    assert "app_value" not in passed.to_json_line()


def test_concurrent_checks_pair_with_their_commands(recorder_process: RecorderProcess) -> None:
    project = recorder_process.project
    sid = recorder_process.session_id
    store = EventStore(project)
    count = 6
    kinds = ["test", "build", "lint", "typecheck", "test", "build"]
    body = "import random,sys,time\ntime.sleep(random.random() * 0.3)\nsys.exit(int(sys.argv[1]))\n"
    # Even indices pass, odd ones fail with a distinct status.
    statuses = [0 if i % 2 == 0 else 20 + i for i in range(count)]
    procs = [
        wrapper(project, *py(body, str(statuses[i])), cwd=project, check=kinds[i])
        for i in range(count)
    ]
    for proc, status in zip(procs, statuses, strict=True):
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == status
        assert "warning" not in err and "error" not in err, err
    recorder_process.stop()

    events = store.read_events(sid)  # strict parse: contiguous, unique, valid
    assert events[-1].type is EventType.SESSION_STOPPED
    finished = {e.payload["command_id"]: e for e in events if e.type is EventType.COMMAND_FINISHED}
    checks = [e for e in events if e.type in CHECK_TYPES]
    assert len(finished) == len(checks) == count
    assert len({c.payload["check_id"] for c in checks}) == count
    assert {c.payload["command_id"] for c in checks} == finished.keys()
    seen = []
    for check in checks:
        end = finished[check.payload["command_id"]]
        assert end.sequence < check.sequence
        assert check.payload["exit_code"] == end.payload["exit_code"]
        assert check.payload["duration_ms"] == end.payload["duration_ms"]
        exit_code = end.payload["exit_code"]
        expected = EventType.CHECK_PASSED if exit_code == 0 else EventType.CHECK_FAILED
        assert check.type is expected
        seen.append((check.payload["kind"], exit_code))
    assert sorted(seen, key=str) == sorted(zip(kinds, statuses, strict=True), key=str)


def test_recorder_process_ending_mid_command(recorder_process: RecorderProcess) -> None:
    project = recorder_process.project
    sid = recorder_process.session_id
    store = EventStore(project)
    go = project.parent / "go"
    body = (
        "import pathlib,sys,time\n"
        "go = pathlib.Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 60\n"
        "while not go.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "sys.exit(4)\n"
    )
    proc = wrapper(project, *py(body, str(go)))
    wait_until(lambda: len(command_events(store, sid)) == 1, what="command.started")
    recorder_process.stop()
    go.write_text("go")
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 4
    assert "the AgentBodycam recording ended before this command finished" in err
    events = store.read_events(sid)
    assert events[-1].type is EventType.SESSION_STOPPED
    assert [e.type for e in command_events(store, sid)] == [EventType.COMMAND_STARTED]


# -- interrupts ---------------------------------------------------------------------------

CHILD_WAITS = (
    "import pathlib,sys,time\n"
    "ready, beat = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
    "beat.write_text(str(time.monotonic()))\n"
    "ready.write_text('ready')\n"
    "deadline = time.monotonic() + 60\n"
    "while time.monotonic() < deadline:\n"
    "    beat.write_text(str(time.monotonic()))\n"
    "    time.sleep(0.05)\n"
    "sys.exit(9)\n"
)
CHILD_HANDLES_INTERRUPT = (
    "import pathlib,signal,sys,time\n"
    "ready = pathlib.Path(sys.argv[1])\n"
    "def done(*_):\n"
    "    sys.exit(0)\n"
    "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGINT), done)\n"
    "signal.signal(signal.SIGINT, done)\n"
    "ready.write_text('ready')\n"
    "deadline = time.monotonic() + 60\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.05)  # Windows: a long sleep is not woken by Ctrl+Break\n"
    "sys.exit(9)\n"
)


@pytest.fixture
def live(project: Path) -> Iterator[Recorder]:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    yield recorder
    if recorder.is_active:
        recorder.stop()


def _interrupt_wrapper(proc: subprocess.Popen[str], sig: int | None = None) -> None:
    if WINDOWS:
        # Ctrl+Break to the wrapper's own process group, which (exactly like a console
        # Ctrl+C) reaches the wrapper and the child it started.
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        os.kill(proc.pid, signal.SIGINT if sig is None else sig)  # the wrapper only


def _start_interruptible(project: Path, *argv: str) -> subprocess.Popen[str]:
    if WINDOWS:
        return wrapper(project, *argv, creationflags=new_group_flags())
    return wrapper(project, *argv, start_new_session=True)  # no terminal: SIGINT is forwarded


def _assert_child_gone(beat: Path) -> None:
    before = beat.read_text()
    time.sleep(0.5)
    assert beat.read_text() == before, "the child kept running after the wrapper exited"


def test_interrupt_ends_the_child_and_is_recorded(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    ready, beat = tmp_path / "ready", tmp_path / "beat"
    proc = _start_interruptible(project, *py(CHILD_WAITS, str(ready), str(beat)))
    wait_until(ready.exists, what="child start")
    _interrupt_wrapper(proc)
    proc.communicate(timeout=60)
    _assert_child_gone(beat)
    _, finished = command_events(live.store, live.session.session_id)
    result = finished.payload
    assert result["outcome"] == "interrupted"
    if WINDOWS:
        # The child's default Ctrl+Break handling ends it with STATUS_CONTROL_C_EXIT.
        assert result["interrupts"] == ["SIGBREAK"]
        assert result["exit_code"] == STATUS_CONTROL_C_EXIT
        assert result["signal"] is None
        assert proc.returncode == STATUS_CONTROL_C_EXIT
    else:
        # Forwarded SIGINT: Python dies from it after its KeyboardInterrupt traceback.
        assert result["interrupts"] == ["SIGINT"]
        assert result["exit_code"] is None
        assert result["signal"] == signal.SIGINT
        assert result["signal_name"] == "SIGINT"
        assert proc.returncode == 128 + signal.SIGINT
    assert "interrupted" in finished.summary


def test_child_that_handles_the_interrupt_keeps_its_exit_code(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    ready = tmp_path / "ready"
    proc = _start_interruptible(project, *py(CHILD_HANDLES_INTERRUPT, str(ready)))
    wait_until(ready.exists, what="child start")
    _interrupt_wrapper(proc)
    proc.communicate(timeout=60)
    assert proc.returncode == 0
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["outcome"] == "interrupted"
    assert finished.payload["exit_code"] == 0


def test_interrupted_check_fails_even_with_exit_zero(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    ready = tmp_path / "ready"
    if WINDOWS:
        proc = wrapper(
            project,
            *py(CHILD_HANDLES_INTERRUPT, str(ready)),
            creationflags=new_group_flags(),
            check="test",
        )
    else:
        proc = wrapper(
            project,
            *py(CHILD_HANDLES_INTERRUPT, str(ready)),
            start_new_session=True,
            check="test",
        )
    wait_until(ready.exists, what="child start")
    _interrupt_wrapper(proc)
    proc.communicate(timeout=60)
    assert proc.returncode == 0
    events = live.store.read_events(live.session.session_id)
    finished = next(e for e in events if e.type is EventType.COMMAND_FINISHED)
    [check] = [e for e in events if e.type in CHECK_TYPES]
    assert check.sequence > finished.sequence
    assert check.type is EventType.CHECK_FAILED
    assert check.payload["reason"] == "interrupted"
    assert check.payload["exit_code"] == 0
    assert check.summary == "Test failed: python (interrupted, exit 0)"


@pytest.mark.skipif(WINDOWS, reason="POSIX signals")
def test_check_killed_by_a_signal_fails(live: Recorder, project: Path) -> None:
    code = "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"
    proc = wrapper(project, *py(code), check="test")
    proc.communicate(timeout=60)
    events = live.store.read_events(live.session.session_id)
    [check] = [e for e in events if e.type in CHECK_TYPES]
    assert check.type is EventType.CHECK_FAILED
    assert check.payload["reason"] == "signaled"
    assert check.payload["exit_code"] is None
    assert check.summary == "Test failed: python (signal SIGKILL)"


@pytest.mark.skipif(WINDOWS, reason="SIGTERM cannot be delivered to a Windows process")
def test_sigterm_is_forwarded_to_the_child(live: Recorder, project: Path, tmp_path: Path) -> None:
    ready, beat = tmp_path / "ready", tmp_path / "beat"
    proc = _start_interruptible(project, *py(CHILD_WAITS, str(ready), str(beat)))
    wait_until(ready.exists, what="child start")
    _interrupt_wrapper(proc, signal.SIGTERM)
    proc.communicate(timeout=60)
    _assert_child_gone(beat)
    assert proc.returncode == 128 + signal.SIGTERM
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["outcome"] == "interrupted"
    assert finished.payload["signal"] == signal.SIGTERM


@pytest.mark.skipif(WINDOWS, reason="POSIX signals")
def test_child_killed_by_a_signal_is_signaled(live: Recorder, project: Path) -> None:
    code = "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"
    proc = wrapper(project, *py(code))
    proc.communicate(timeout=60)
    assert proc.returncode == 128 + signal.Signals["SIGKILL"]
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["outcome"] == "signaled"
    assert finished.payload["signal_name"] == "SIGKILL"
