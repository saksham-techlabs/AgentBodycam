"""Active-recording lease (one recorder per project) and active-session discovery."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentbodycam import __version__
from agentbodycam.cli import main
from agentbodycam.core.active import (
    ACTIVE_FILENAME,
    ACTIVE_LOCK_FILENAME,
    ActiveLease,
    ActiveRecord,
    ActiveState,
    RecordingInProgressError,
    acquire_active_lease,
    get_active_session,
)
from agentbodycam.core.filelock import is_locked
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore, UnsafePathError
from agentbodycam.models.events import JsonObject
from agentbodycam.models.session import SessionStatus
from git_helpers import commit_all, init_repo, requires_git
from recording_helpers import all_events, file_events, hard_kill_pid, try_symlink, write

SESSION_LINE = re.compile(r"^Session: (\S+)$", re.MULTILINE)


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def active_json(project: Path) -> Path:
    return project / ".agentbodycam" / ACTIVE_FILENAME


def active_lock(project: Path) -> Path:
    return project / ".agentbodycam" / ACTIVE_LOCK_FILENAME


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


@pytest.fixture
def recording(store: EventStore) -> Iterator[Recorder]:
    rec = Recorder(store, record_git=False)
    rec.start()
    rec.take_baseline()
    yield rec
    if rec.is_active:
        rec.stop()


# -- lease lifecycle ----------------------------------------------------------------------


def test_fresh_project_has_no_active_session_and_nothing_is_created(project: Path) -> None:
    info = get_active_session(project)
    assert info.state is ActiveState.INACTIVE
    assert not info.is_active
    assert info.record is None
    assert tree(project) == []


def test_missing_project_is_inactive(tmp_path: Path) -> None:
    assert get_active_session(tmp_path / "missing").state is ActiveState.INACTIVE
    assert not (tmp_path / "missing").exists()


def test_recording_is_discoverable(recording: Recorder, project: Path) -> None:
    info = get_active_session(project)
    assert info.state is ActiveState.ACTIVE
    assert info.session is not None and info.record is not None
    assert info.session == recording.session
    assert info.session.status is SessionStatus.RECORDING
    assert info.record.session_id == recording.session.session_id
    assert info.record.pid == os.getpid()
    assert info.record.started_at == recording.session.started_at
    assert info.record.agentbodycam_version == __version__
    assert is_locked(active_lock(project))


def test_active_json_schema_is_minimal(recording: Recorder, project: Path) -> None:
    text = active_json(project).read_text(encoding="utf-8")
    data = json.loads(text)
    assert set(data) == {
        "schema_version",
        "session_id",
        "pid",
        "started_at",
        "agentbodycam_version",
        "lease_id",
    }
    assert data["schema_version"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", data["lease_id"])
    assert str(project.resolve()) not in text
    assert project.name not in text
    for private in (os.environ.get("USERNAME"), os.environ.get("USER")):
        if private:
            assert f'"{private}"' not in text
    assert text.endswith("\n")


def test_session_started_precedes_publication(recording: Recorder, project: Path) -> None:
    sid = recording.session.session_id
    assert json.loads(active_json(project).read_text(encoding="utf-8"))["session_id"] == sid
    assert all_events(recording.store, sid)[0].type.value == "session.started"


def test_stop_clears_metadata_and_releases_lock(recording: Recorder, project: Path) -> None:
    recording.stop()
    assert not active_json(project).exists()
    assert active_lock(project).exists()  # lock files are never deleted
    assert not is_locked(active_lock(project))
    assert recording.lease is None
    assert get_active_session(project).state is ActiveState.INACTIVE


def test_second_recorder_is_rejected_without_creating_a_session(
    recording: Recorder, store: EventStore, project: Path
) -> None:
    before = store.list_session_ids()
    second = Recorder(EventStore(project), record_git=False)
    with pytest.raises(RecordingInProgressError) as exc:
        second.start()
    assert exc.value.record is not None
    assert exc.value.record.session_id == recording.session.session_id
    assert store.list_session_ids() == before
    assert not second.is_active
    # The live recording is untouched.
    assert get_active_session(project).session == recording.session


def test_next_recorder_can_start_after_clean_stop(store: EventStore, project: Path) -> None:
    for _ in range(3):
        rec = Recorder(store, record_git=False)
        session = rec.run(duration=0)
        assert session.status is SessionStatus.STOPPED
    assert len(store.list_session_ids()) == 3
    assert get_active_session(project).state is ActiveState.INACTIVE


def test_lease_released_when_start_fails(
    store: EventStore, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise OSError("disk full")

    rec = Recorder(store, record_git=False)
    monkeypatch.setattr(rec, "_emit_started", boom)
    with pytest.raises(OSError, match="disk full"):
        rec.start()
    assert not is_locked(active_lock(project))
    assert not active_json(project).exists()
    Recorder(store, record_git=False).run(duration=0)


def test_lease_released_when_run_fails(store: EventStore, project: Path) -> None:
    def sleep(_seconds: float) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        Recorder(store, record_git=False, sleep=sleep).run()
    assert not is_locked(active_lock(project))
    assert get_active_session(project).state is ActiveState.INACTIVE


def test_publish_is_atomic(
    store: EventStore, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with acquire_active_lease(store) as lease:
        first = store.mark_recording(store.create_session().session_id)
        lease.publish(first)
        before = active_json(project).read_bytes()
        second = store.mark_recording(store.create_session().session_id)

        def fail_replace(self: Path, target: Path) -> Path:
            raise OSError("simulated crash during rename")

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="simulated"):
            lease.publish(second)
        monkeypatch.undo()
        assert active_json(project).read_bytes() == before
        assert [p.name for p in (project / ".agentbodycam").iterdir() if ".tmp" in p.name] == []


def test_release_keeps_metadata_it_does_not_own(store: EventStore, project: Path) -> None:
    lease = acquire_active_lease(store)
    session = store.mark_recording(store.create_session().session_id)
    record = lease.publish(session)
    foreign = {**record.to_dict(), "lease_id": "f" * 32}
    active_json(project).write_text(json.dumps(foreign), encoding="utf-8")
    lease.release()
    assert json.loads(active_json(project).read_text(encoding="utf-8")) == foreign
    assert not lease.held
    with pytest.raises(Exception, match="released"):
        lease.publish(session)


# -- discovery of bad / stale metadata ----------------------------------------------------


def _held_lease_with(store: EventStore, project: Path, content: str) -> ActiveLease:
    lease = acquire_active_lease(store)
    active_json(project).write_text(content, encoding="utf-8")
    return lease


def _record_for(store: EventStore, session_id: str) -> JsonObject:
    session = store.load_session(session_id)
    return ActiveRecord(
        session_id=session_id,
        pid=12345,
        started_at=session.started_at,
        agentbodycam_version=__version__,
        lease_id="a" * 32,
    ).to_dict()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        "{}",
        '{"schema_version": 1}',
        "\xff",
    ],
)
def test_malformed_metadata_while_locked_is_invalid(
    store: EventStore, project: Path, content: str
) -> None:
    with _held_lease_with(store, project, content):
        info = get_active_session(project)
    assert info.state is ActiveState.INVALID
    assert info.reason is not None and "malformed" in info.reason
    assert info.session is None


def test_malformed_metadata_without_lock_is_stale(store: EventStore, project: Path) -> None:
    store.ensure_layout()
    active_json(project).write_text("{not json", encoding="utf-8")
    info = get_active_session(project)
    assert info.state is ActiveState.STALE
    assert active_json(project).read_text(encoding="utf-8") == "{not json"  # read-only


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "../../etc"),
        ("session_id", "20261014T024500Z-ZZZZ"),
        ("pid", -1),
        ("pid", "123"),
        ("lease_id", "short"),
        ("schema_version", 2),
        ("started_at", "yesterday"),
        ("hostname", "box"),
    ],
)
def test_metadata_fields_are_validated_strictly(
    store: EventStore, project: Path, field: str, value: object
) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    data = {**_record_for(store, sid), field: value}
    with pytest.raises(ValueError):  # EventValidationError or InvalidSessionIdError
        ActiveRecord.from_dict(data)
    with _held_lease_with(store, project, json.dumps(data)):
        assert get_active_session(project).state is ActiveState.INVALID


def test_lock_held_before_metadata_is_written(store: EventStore, project: Path) -> None:
    with acquire_active_lease(store):
        info = get_active_session(project)
    assert info.state is ActiveState.INVALID
    assert info.reason is not None and "no active metadata" in info.reason


def test_metadata_for_missing_session_is_invalid(store: EventStore, project: Path) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    data = {**_record_for(store, sid), "session_id": "20991231T235959Z-0000000000000000"}
    with _held_lease_with(store, project, json.dumps(data)):
        info = get_active_session(project)
    assert info.state is ActiveState.INVALID
    assert info.reason is not None and "does not exist" in info.reason


def test_metadata_for_mismatched_session_is_invalid(store: EventStore, project: Path) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    data = {**_record_for(store, sid), "started_at": "2001-01-01T00:00:00.000000Z"}
    with _held_lease_with(store, project, json.dumps(data)):
        info = get_active_session(project)
    assert info.state is ActiveState.INVALID
    assert info.reason is not None and "does not match" in info.reason


@pytest.mark.parametrize("status", [SessionStatus.CREATED, SessionStatus.STOPPED])
def test_session_that_is_not_recording_cannot_be_active(
    store: EventStore, project: Path, status: SessionStatus
) -> None:
    sid = store.create_session().session_id
    if status is SessionStatus.STOPPED:
        store.mark_recording(sid)
        store.mark_stopped(sid)
    with _held_lease_with(store, project, json.dumps(_record_for(store, sid))):
        info = get_active_session(project)
    assert info.state is ActiveState.INVALID
    assert info.reason is not None and "not 'recording'" in info.reason


def test_valid_looking_metadata_without_lock_is_stale(store: EventStore, project: Path) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    active_json(project).write_text(json.dumps(_record_for(store, sid)), encoding="utf-8")
    info = get_active_session(project)
    assert info.state is ActiveState.STALE
    assert info.record is not None and info.record.session_id == sid


def test_live_pid_does_not_make_stale_metadata_active(store: EventStore, project: Path) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    data = {**_record_for(store, sid), "pid": os.getpid()}  # a PID that certainly exists
    active_json(project).write_text(json.dumps(data), encoding="utf-8")
    assert get_active_session(project).state is ActiveState.STALE


def test_new_recorder_replaces_stale_metadata(store: EventStore, project: Path) -> None:
    old = store.mark_recording(store.create_session().session_id).session_id
    active_json(project).write_text(json.dumps(_record_for(store, old)), encoding="utf-8")
    rec = Recorder(store, record_git=False)
    new = rec.start().session_id
    assert rec.lease is not None and rec.lease.stale_record is not None
    assert rec.lease.stale_record.session_id == old
    info = get_active_session(project)
    assert info.state is ActiveState.ACTIVE
    assert info.record is not None and info.record.session_id == new
    rec.stop()
    assert get_active_session(project).state is ActiveState.INACTIVE


def test_new_recorder_replaces_corrupt_metadata(store: EventStore, project: Path) -> None:
    store.ensure_layout()
    active_json(project).write_text("garbage", encoding="utf-8")
    rec = Recorder(store, record_git=False)
    rec.start()
    assert get_active_session(project).state is ActiveState.ACTIVE
    rec.stop()


# -- path safety --------------------------------------------------------------------------


def test_symlinked_active_json_is_rejected(
    store: EventStore, project: Path, tmp_path: Path
) -> None:
    store.ensure_layout()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    if not try_symlink(active_json(project), target):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        get_active_session(project)
    with pytest.raises(UnsafePathError):
        Recorder(store, record_git=False).start()
    assert target.read_text(encoding="utf-8") == "{}"
    assert store.list_session_ids() == []
    assert not is_locked(active_lock(project))


def test_symlinked_active_lock_is_rejected(
    store: EventStore, project: Path, tmp_path: Path
) -> None:
    store.ensure_layout()
    target = tmp_path / "outside.lock"
    if not try_symlink(active_lock(project), target):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        Recorder(store, record_git=False).start()
    with pytest.raises(UnsafePathError):
        get_active_session(project)
    assert not target.exists()
    assert store.list_session_ids() == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junctioned_store_is_rejected(project: Path, tmp_path: Path) -> None:
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(project / ".agentbodycam"))
    with pytest.raises(UnsafePathError):
        get_active_session(project)
    with pytest.raises(UnsafePathError):
        Recorder(EventStore(project), record_git=False).start()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["with spaces", "ünïcødé プロジェクト"])
def test_awkward_project_paths(tmp_path: Path, name: str) -> None:
    project = tmp_path / name / "nested dir"
    project.mkdir(parents=True)
    rec = Recorder(EventStore(project), record_git=False)
    sid = rec.start().session_id
    info = get_active_session(project)
    assert info.state is ActiveState.ACTIVE and info.session is not None
    assert info.session.session_id == sid
    rec.stop()
    assert get_active_session(project).state is ActiveState.INACTIVE


# -- recorder evidence is unaffected ------------------------------------------------------


def test_lock_and_active_files_never_become_file_events(recording: Recorder) -> None:
    store = recording.store
    write(store.project_root / "app.py", "x")
    recording.scan_once()
    assert store.event_lock_path(recording.session.session_id).is_file()
    assert (store.root / ACTIVE_LOCK_FILENAME).is_file()
    recording.scan_once()
    session = recording.stop()
    events = all_events(recording.store, session.session_id)
    assert file_events(events) == [("file.created", "app.py")]
    assert all(".agentbodycam" not in (e.path or "") for e in events)


@requires_git
def test_git_recording_still_works_and_ignores_lock_files(project: Path) -> None:
    init_repo(project)
    write(project / "a.txt", "a")
    commit_all(project, "First")
    rec = Recorder(EventStore(project))
    rec.start()
    rec.take_baseline()
    write(project / "b.txt", "b")
    rec.scan_once(force_git=True)
    session = rec.stop()
    events = all_events(rec.store, session.session_id)
    snapshots = [e for e in events if e.type.value == "git.snapshot"]
    assert snapshots
    dumped = json.dumps([e.payload for e in snapshots])
    assert "b.txt" in dumped
    assert ".agentbodycam" not in dumped
    assert ACTIVE_LOCK_FILENAME not in dumped


# -- CLI ----------------------------------------------------------------------------------


def test_cli_session_active_when_nothing_records(project: Path) -> None:
    code, out, err = run("session", "active", "--project", str(project))
    assert (code, out, err) == (0, "No active AgentBodycam recording.\n", "")
    assert tree(project) == []


def test_cli_session_active_defaults_to_cwd(
    recording: Recorder, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    code, out, _ = run("session", "active")
    assert code == 0
    assert f"Session: {recording.session.session_id}" in out


def test_cli_session_active_while_recording(recording: Recorder, project: Path) -> None:
    before = tree(project)
    code, out, err = run("session", "active", "--project", str(project))
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "Active AgentBodycam recording"
    assert lines[1] == f"Session: {recording.session.session_id}"
    assert lines[2].startswith("Started: ") and lines[2].endswith(" UTC")
    assert lines[3] == f"PID: {os.getpid()}"
    assert tree(project) == before  # read-only


def test_cli_session_active_reports_stale(store: EventStore, project: Path) -> None:
    sid = store.mark_recording(store.create_session().session_id).session_id
    active_json(project).write_text(json.dumps(_record_for(store, sid)), encoding="utf-8")
    code, out, _ = run("session", "active", "--project", str(project))
    assert code == 0
    assert out.startswith("No active AgentBodycam recording.\n")
    assert f"Stale active-recording metadata found (session {sid})" in out
    assert active_json(project).exists()


def test_cli_session_active_reports_invalid(store: EventStore, project: Path) -> None:
    with _held_lease_with(store, project, "{not json"):
        code, out, err = run("session", "active", "--project", str(project))
    assert code == 1
    assert out == ""
    assert "cannot be trusted" in err


def test_cli_second_record_fails_cleanly(recording: Recorder, store: EventStore) -> None:
    before = store.list_session_ids()
    code, out, err = run("record", "--project", str(store.project_root), "--duration", "0")
    assert code == 1
    assert out == ""
    assert err == (
        "error: AgentBodycam is already recording this project\n"
        f"Session: {recording.session.session_id}\n"
    )
    assert store.list_session_ids() == before


def test_cli_record_notes_replaced_stale_metadata(store: EventStore, project: Path) -> None:
    old = store.mark_recording(store.create_session().session_id).session_id
    active_json(project).write_text(json.dumps(_record_for(store, old)), encoding="utf-8")
    code, out, err = run("record", "--project", str(project), "--duration", "0")
    assert code == 0, err
    assert f"replaced stale active-recording metadata from session {old}" in out
    assert not active_json(project).exists()


# -- real processes: second recorder, hard kill, recovery ---------------------------------


def _reader(stream: io.TextIOBase, lines: queue.Queue[str]) -> None:
    for line in iter(stream.readline, ""):
        lines.put(line)


def _wait_for(lines: queue.Queue[str], needle: str, timeout: float = 60) -> str:
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(line)
        if needle in line:
            return "".join(seen)
    raise AssertionError(f"timed out waiting for {needle!r}; output so far: {''.join(seen)!r}")


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "agentbodycam", *args]


ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def test_killed_recorder_releases_lease_and_next_recorder_recovers(tmp_path: Path) -> None:
    project = tmp_path / "crash ünïcødé project" / "with spaces"
    project.mkdir(parents=True)
    write(project / "app.py", "v1\n")
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
        _cli("record", "--project", str(project), "--interval", "0.1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=ENV,
    )
    assert proc.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()
    recorder_pid: int | None = None
    try:
        crashed_sid = SESSION_LINE.findall(_wait_for(lines, "Press Ctrl+C to stop."))[0]

        # Another process sees the recording...
        shown = subprocess.run(  # noqa: S603
            _cli("session", "active", "--project", str(project)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ENV,
            timeout=60,
            check=False,
        )
        assert shown.returncode == 0, shown.stderr
        assert f"Session: {crashed_sid}" in shown.stdout
        recorder_pid = int(re.findall(r"^PID: (\d+)$", shown.stdout, re.MULTILINE)[0])
        if sys.platform != "win32":  # a Windows venv python.exe is a launcher process
            assert recorder_pid == proc.pid

        # ...and a second recorder in another process is refused without a new session.
        second = subprocess.run(  # noqa: S603
            _cli("record", "--project", str(project), "--duration", "0"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ENV,
            timeout=60,
            check=False,
        )
        assert second.returncode == 1
        assert "error: AgentBodycam is already recording this project" in second.stderr
        assert f"Session: {crashed_sid}" in second.stderr
        assert EventStore(project).list_session_ids() == [crashed_sid]
    finally:
        # SIGKILL / TerminateProcess on the real recorder: no cleanup handlers run.
        if recorder_pid is not None and recorder_pid != proc.pid:
            hard_kill_pid(recorder_pid)
        proc.kill()
        proc.wait(timeout=30)

    # The crash leaves metadata and a "recording" session behind, but no OS lock.
    assert active_json(project).exists()
    store = EventStore(project)
    assert store.load_session(crashed_sid).status is SessionStatus.RECORDING
    deadline = time.monotonic() + 10
    while is_locked(active_lock(project)) and time.monotonic() < deadline:
        time.sleep(0.05)
    info = get_active_session(project)
    assert info.state is ActiveState.STALE
    assert info.record is not None and info.record.session_id == crashed_sid

    # A new recorder starts with no manual cleanup and replaces the stale metadata.
    rec = Recorder(store, record_git=False)
    new_sid = rec.start().session_id
    assert rec.lease is not None and rec.lease.stale_record is not None
    assert rec.lease.stale_record.session_id == crashed_sid
    info = get_active_session(project)
    assert info.state is ActiveState.ACTIVE
    assert info.record is not None and info.record.session_id == new_sid
    assert info.record.pid == os.getpid()
    rec.stop()
    assert not active_json(project).exists()
    assert get_active_session(project).state is ActiveState.INACTIVE
    assert store.read_events(crashed_sid)  # the crashed session's evidence is intact
