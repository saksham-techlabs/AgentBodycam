"""``agentbodycam run``: attachment, events, privacy and exit codes (real child processes)."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentbodycam import __version__
from agentbodycam.core.active import (
    ActiveRecord,
    ActiveSessionInfo,
    ActiveState,
    acquire_active_lease,
    active_metadata_path,
)
from agentbodycam.core.command import (
    EXIT_CANNOT_EXECUTE,
    EXIT_INTERRUPTED,
    EXIT_NOT_FOUND,
    EXIT_SETUP_FAILED,
    CommandResult,
    NoActiveRecordingError,
    Outcome,
    RecordingChangedError,
    ShellRequiredError,
    UnsafeWorkingDirectoryError,
    _InterruptRelay,
    append_to_recording,
    attach_active_session,
    confirm_attachment,
    execute,
    process_exit_status,
    resolve_executable,
    select_working_directory,
)
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore, atomic_write_text
from agentbodycam.models.events import (
    Event,
    EventDraft,
    EventSource,
    EventType,
    JsonValue,
    dumps_canonical,
)
from command_helpers import command_events, py, run_cli, wait_until
from recording_helpers import store_bytes

WINDOWS = sys.platform == "win32"
DUMP_ARGV = (
    "import json,sys; open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(sys.argv[2:]))"
)
TOUCH = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')"


@pytest.fixture
def live(project: Path) -> Iterator[Recorder]:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    yield recorder
    if recorder.is_active:
        recorder.stop()


def run(project: Path, *argv: str) -> tuple[int, str]:
    return run_cli("run", "--project", str(project), "--", *argv)


def events_bytes(store: EventStore, sid: str) -> bytes:
    return store.events_path(sid).read_bytes()


def recorded_args(started: Event) -> list[JsonValue]:
    """Recorded argv after `<python> -c <code> <output file>`."""
    argv = started.payload["argv"]
    assert isinstance(argv, list)
    return argv[4:]


# -- attachment ---------------------------------------------------------------------------


def test_no_active_recording_does_not_run_the_command(project: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    code, err = run(project, *py(TOUCH, str(marker)))
    assert code == EXIT_SETUP_FAILED
    assert "error: no active AgentBodycam recording for this project" in err
    assert "Start one with: agentbodycam record" in err
    assert not marker.exists()
    assert not (project / ".agentbodycam").exists()  # nothing created


def test_stale_metadata_is_never_attached(project: Path, tmp_path: Path) -> None:
    store = EventStore(project)
    session = store.mark_recording(store.create_session().session_id)
    record = ActiveRecord(
        session_id=session.session_id,
        pid=1,
        started_at=session.started_at,
        agentbodycam_version=__version__,
        lease_id="ab" * 16,
    )
    atomic_write_text(active_metadata_path(store), dumps_canonical(record.to_dict()))
    marker = tmp_path / "ran"
    code, err = run(project, *py(TOUCH, str(marker)))
    assert code == EXIT_SETUP_FAILED
    assert "stale metadata" in err
    assert not marker.exists()
    assert store.read_events(session.session_id) == []


def test_invalid_metadata_is_rejected_after_bounded_retry(project: Path, tmp_path: Path) -> None:
    store = EventStore(project)
    lease = acquire_active_lease(store)
    try:
        atomic_write_text(active_metadata_path(store), "{not json")
        marker = tmp_path / "ran"
        started = time.monotonic()
        code, err = run(project, *py(TOUCH, str(marker)))
        elapsed = time.monotonic() - started
    finally:
        lease.release()
    assert code == EXIT_SETUP_FAILED
    assert "a recorder holds the lock" in err
    assert not marker.exists()
    assert 0.9 <= elapsed < 10


def test_startup_transient_is_retried(project: Path, tmp_path: Path) -> None:
    """The recorder holds the lease but has not published active.json yet."""
    store = EventStore(project)
    lease = acquire_active_lease(store)
    try:
        session = store.mark_recording(store.create_session().session_id)
        publisher = threading.Timer(0.3, lease.publish, args=(session,))
        publisher.start()
        marker = tmp_path / "ran"
        code, err = run(project, *py(TOUCH, str(marker)))
        publisher.join()
    finally:
        lease.release()
    assert code == 0, err
    assert marker.exists()
    started, finished = command_events(store, session.session_id)
    assert started.type is EventType.COMMAND_STARTED
    assert finished.type is EventType.COMMAND_FINISHED


def test_attach_retries_only_while_invalid() -> None:
    sleeps: list[float] = []
    states = iter([ActiveState.INVALID, ActiveState.INVALID, ActiveState.INACTIVE])

    def probe(_root: Path) -> ActiveSessionInfo:
        return ActiveSessionInfo(next(states))

    with pytest.raises(NoActiveRecordingError) as caught:
        attach_active_session(Path(), probe=probe, sleep=sleeps.append)
    assert caught.value.info.state is ActiveState.INACTIVE
    assert len(sleeps) == 2


@pytest.mark.parametrize("state", [ActiveState.INACTIVE, ActiveState.STALE])
def test_attach_fails_immediately_without_a_live_recorder(state: ActiveState) -> None:
    sleeps: list[float] = []
    with pytest.raises(NoActiveRecordingError):
        attach_active_session(
            Path(), probe=lambda _r: ActiveSessionInfo(state), sleep=sleeps.append
        )
    assert sleeps == []


def test_attach_retry_is_bounded() -> None:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    with pytest.raises(NoActiveRecordingError):
        attach_active_session(
            Path(),
            timeout=1.0,
            probe=lambda _r: ActiveSessionInfo(ActiveState.INVALID),
            sleep=sleep,
            monotonic=lambda: clock[0],
        )
    assert 1.0 <= clock[0] <= 1.2


def test_lease_mismatch_is_rejected(live: Recorder, project: Path) -> None:
    store = live.store
    attachment = attach_active_session(project)
    assert attachment.session_id == live.session.session_id
    assert live.lease is not None and attachment.lease_id == live.lease.lease_id
    record = live.lease.record
    assert record is not None
    forged = {**record.to_dict(), "lease_id": "cd" * 16}
    atomic_write_text(active_metadata_path(store), dumps_canonical(forged))
    with pytest.raises(RecordingChangedError, match="lease"):
        confirm_attachment(attachment, timeout=0.2)
    draft = EventDraft(type=EventType.COMMAND_STARTED, source=EventSource.COMMAND, summary="x")
    with pytest.raises(RecordingChangedError, match="lease"):
        append_to_recording(store, attachment, draft)
    assert command_events(store, attachment.session_id) == []


def test_no_append_after_the_recording_stopped(live: Recorder, project: Path) -> None:
    attachment = attach_active_session(project)
    live.stop()
    draft = EventDraft(type=EventType.COMMAND_FINISHED, source=EventSource.COMMAND, summary="x")
    with pytest.raises(RecordingChangedError):
        confirm_attachment(attachment, timeout=0.2)
    with pytest.raises(RecordingChangedError):
        append_to_recording(live.store, attachment, draft)
    assert live.store.read_events(attachment.session_id)[-1].type is EventType.SESSION_STOPPED


def test_no_append_into_a_newer_recording(live: Recorder, project: Path) -> None:
    attachment = attach_active_session(project)
    live.stop()
    newer = Recorder(EventStore(project), record_git=False)
    newer.start()
    try:
        draft = EventDraft(type=EventType.COMMAND_FINISHED, source=EventSource.COMMAND, summary="x")
        with pytest.raises(RecordingChangedError, match="another recording"):
            confirm_attachment(attachment, timeout=0.2)
        with pytest.raises(RecordingChangedError):
            append_to_recording(newer.store, attachment, draft)
        assert command_events(newer.store, newer.session.session_id) == []
    finally:
        newer.stop()


# -- recorded events ----------------------------------------------------------------------


def test_command_is_run_and_recorded(live: Recorder, project: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    code, err = run(project, *py(TOUCH, str(marker)))
    assert code == 0, err
    assert marker.read_text(encoding="utf-8") == "ran"
    sid = live.session.session_id
    started, finished = command_events(live.store, sid)
    assert started.sequence < finished.sequence
    assert started.source is EventSource.COMMAND and started.path is None
    assert finished.source is EventSource.COMMAND and finished.path is None
    command_id = started.payload["command_id"]
    assert isinstance(command_id, str) and re.fullmatch(r"[0-9a-f]{32}", command_id)
    assert finished.payload["command_id"] == command_id
    payload = started.payload
    assert payload["executable"] == Path(sys.executable).name
    assert payload["argument_count"] == 3
    assert payload["argv"] == ["<external-path>", "-c", TOUCH, "<external-path>"]
    assert payload["working_directory"] in (".",)
    assert payload["shell"] is False
    assert payload["stdio"] == "inherited"
    assert payload["output_recorded"] is False
    assert payload["environment_recorded"] is False
    assert payload["argv_redacted"] is False
    assert payload["argv_truncated"] is False
    assert started.summary.startswith("Command started: <external-path> -c ")
    result = finished.payload
    assert result["outcome"] == "exited"
    assert result["exit_code"] == 0
    assert result["signal"] is None
    assert result["error"] is None
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert isinstance(result["pid"], int)
    assert finished.summary.endswith("(exit 0)")


def test_command_ids_are_unique(live: Recorder, project: Path) -> None:
    for _ in range(3):
        assert run(project, *py("pass"))[0] == 0
    ids = [e.payload["command_id"] for e in command_events(live.store, live.session.session_id)]
    assert len(ids) == 6 and len(set(ids)) == 3


@pytest.mark.parametrize("exit_code", [1, 7, 255])
def test_child_exit_code_is_propagated(live: Recorder, project: Path, exit_code: int) -> None:
    code, _ = run(project, *py(f"import sys; sys.exit({exit_code})"))
    assert code == exit_code
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["exit_code"] == exit_code
    assert finished.payload["outcome"] == "exited"
    assert finished.summary.endswith(f"(exit {exit_code})")


def test_missing_executable_is_a_paired_spawn_error(live: Recorder, project: Path) -> None:
    code, err = run(project, "no-such-program-4f1e", "--token", "S3CRET")
    assert code == EXIT_NOT_FOUND
    assert "executable not found" in err
    started, finished = command_events(live.store, live.session.session_id)
    assert started.payload["command_id"] == finished.payload["command_id"]
    assert finished.payload["outcome"] == "spawn_error"
    assert finished.payload["exit_code"] is None
    assert finished.payload["pid"] is None
    assert finished.payload["error"] == {"type": "FileNotFoundError", "errno": 2}
    assert b"S3CRET" not in events_bytes(live.store, live.session.session_id)


def test_missing_explicit_path_records_no_absolute_path(live: Recorder, project: Path) -> None:
    missing = project / "bin" / "nothing-here"
    code, _ = run(project, str(missing))
    assert code == EXIT_NOT_FOUND
    data = events_bytes(live.store, live.session.session_id)
    assert str(project).encode() not in data
    assert json.dumps(str(project))[1:-1].encode() not in data
    started, _finished = command_events(live.store, live.session.session_id)
    assert started.payload["argv"] == ["<project>/bin/nothing-here"]


def test_unicode_and_spaces_reach_the_child_untouched(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    out = tmp_path / "argv.json"
    args = ["héllo wörld", "日本語 ✓", "two  spaces", "", 'quote"inside', "trailing\\"]
    code, err = run(project, *py(DUMP_ARGV, str(out), *args))
    assert code == 0, err
    assert json.loads(out.read_text(encoding="utf-8")) == args
    started, _ = command_events(live.store, live.session.session_id)
    assert recorded_args(started) == args


def test_shell_metacharacters_are_literal_arguments(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    out = tmp_path / "argv.json"
    args = [
        "a && b",
        "||",
        "|",
        "c",
        ">",
        "redirected.txt",
        "<",
        "in",
        ";",
        "$(id)",
        "%PATH%",
        "`x`",
    ]
    code, err = run(project, *py(DUMP_ARGV, str(out), *args))
    assert code == 0, err
    assert json.loads(out.read_text(encoding="utf-8")) == args
    assert not (project / "redirected.txt").exists()
    assert not (tmp_path / "redirected.txt").exists()


def test_working_directory_inside_project(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = project / "pkg" / "sub dir"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    out = tmp_path / "cwd.txt"
    code, _ = run(project, *py("import os,sys; open(sys.argv[1],'w').write(os.getcwd())", str(out)))
    assert code == 0
    assert Path(out.read_text()).resolve() == sub.resolve()
    started, _ = command_events(live.store, live.session.session_id)
    assert started.payload["working_directory"] == "pkg/sub dir"


def test_working_directory_outside_project_falls_back_to_root(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "cwd.txt"
    code, _ = run(project, *py("import os,sys; open(sys.argv[1],'w').write(os.getcwd())", str(out)))
    assert code == 0
    assert Path(out.read_text()).resolve() == project.resolve()
    started, _ = command_events(live.store, live.session.session_id)
    assert started.payload["working_directory"] == "."


def test_working_directory_inside_store_is_refused(project: Path) -> None:
    (project / ".agentbodycam" / "sessions").mkdir(parents=True)
    with pytest.raises(UnsafeWorkingDirectoryError):
        select_working_directory(project, project / ".agentbodycam" / "sessions")
    assert select_working_directory(project, project.parent) == (project.resolve(), ".")


# -- privacy ------------------------------------------------------------------------------


def test_stdout_and_stderr_are_inherited_not_recorded(
    live: Recorder, project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # The markers are assembled by the child so they never appear in its (recorded) argv.
    code, _ = run(
        project,
        *py(
            "import sys; print('OUT-' + 'MARK-7f3a'); print('ERR-' + 'MARK-9c1d', file=sys.stderr)"
        ),
    )
    assert code == 0
    captured = capfd.readouterr()
    assert "OUT-MARK-7f3a" in captured.out  # the child really wrote to our terminal
    assert "ERR-MARK-9c1d" in captured.err
    data = store_bytes(project)
    assert b"OUT-MARK-7f3a" not in data
    assert b"ERR-MARK-9c1d" not in data


def test_environment_is_inherited_not_recorded(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ABC_TEST_ENV_NAME_5e2", "env-value-secret-8b4")
    out = tmp_path / "env.txt"
    name = "'ABC_TEST_' + 'ENV_NAME_5e2'"  # assembled by the child, never in its argv
    code, _ = run(
        project,
        *py(f"import os,sys; open(sys.argv[1],'w').write(os.environ[{name}])", str(out)),
    )
    assert code == 0
    assert out.read_text() == "env-value-secret-8b4"
    data = store_bytes(project)
    assert b"env-value-secret-8b4" not in data
    assert b"ABC_TEST_ENV_NAME_5e2" not in data


def test_secrets_are_redacted_but_the_child_gets_them(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    secrets = {
        "flag": "flag-secret-111aaa",
        "inline": "inline-secret-222bbb",
        "assign": "assign-secret-333ccc",
        "url": "url-secret-444ddd",
        "token": "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1",
    }
    args = [
        "--token",
        secrets["flag"],
        f"--api-key={secrets['inline']}",
        f"password={secrets['assign']}",
        f"https://bob:{secrets['url']}@example.com/repo.git",
        secrets["token"],
    ]
    out = tmp_path / "argv.json"
    code, err = run(project, *py(DUMP_ARGV, str(out), *args))
    assert code == 0, err
    assert json.loads(out.read_text(encoding="utf-8")) == args  # untouched for the child
    data = store_bytes(project)
    for value in secrets.values():
        assert value.encode() not in data
    started, finished = command_events(live.store, live.session.session_id)
    assert started.payload["argv_redacted"] is True
    assert recorded_args(started) == [
        "--token",
        "<redacted>",
        "--api-key=<redacted>",
        "password=<redacted>",
        "https://<redacted>@example.com/repo.git",
        "<redacted>",
    ]
    for event in (started, finished):
        assert all(value not in event.summary for value in secrets.values())
        assert "<redacted>" in event.summary


# -- running: unit-level ------------------------------------------------------------------


def test_interrupt_before_spawn_does_not_start_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    relay = _InterruptRelay(received=["SIGINT"])
    result, error = execute(py(TOUCH, str(marker)), tmp_path, relay)
    assert error is None
    assert result.outcome is Outcome.INTERRUPTED
    assert result.exit_code is None and result.pid is None
    assert result.wrapper_exit_code == EXIT_INTERRUPTED
    assert result.describe() == "interrupted, not started"
    assert not marker.exists()


def test_wrapper_exit_codes() -> None:
    def result(outcome: Outcome, **kw: object) -> CommandResult:
        base: dict[str, object] = {
            "exit_code": None,
            "signal": None,
            "duration_ms": 1,
            "pid": None,
            "interrupts": (),
        }
        base.update(kw)
        return CommandResult(outcome, **base)  # type: ignore[arg-type]

    assert result(Outcome.EXITED, exit_code=3).wrapper_exit_code == 3
    assert result(Outcome.SIGNALED, signal=15).wrapper_exit_code == 143
    assert result(Outcome.INTERRUPTED, signal=2).wrapper_exit_code == 130
    assert result(Outcome.INTERRUPTED, exit_code=1).wrapper_exit_code == 1
    spawn = Outcome.SPAWN_ERROR
    assert result(spawn, error_type="FileNotFoundError").wrapper_exit_code == EXIT_NOT_FOUND
    assert result(spawn, error_type="PermissionError").wrapper_exit_code == EXIT_CANNOT_EXECUTE
    ntstatus = 0xC000013A
    assert process_exit_status(ntstatus) == (ntstatus - 2**32 if WINDOWS else ntstatus)
    assert process_exit_status(7) == 7


def _make_program(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_executable_searches_only_absolute_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "abc-tool.exe" if WINDOWS else "abc-tool"
    program = _make_program(tmp_path / "bin", name)
    assert Path(resolve_executable("abc-tool", tmp_path, str(tmp_path / "bin"))) == program
    _make_program(tmp_path / "cwd", name)
    monkeypatch.chdir(tmp_path / "cwd")
    with pytest.raises(FileNotFoundError):  # "." and the current directory are never searched
        resolve_executable("abc-tool", tmp_path / "cwd", ".")
    explicit = Path("sub") / name
    _make_program(tmp_path / "sub", name)
    assert Path(resolve_executable(str(explicit), tmp_path, "")) == tmp_path / explicit


@pytest.mark.skipif(not WINDOWS, reason="batch scripts are a Windows concept")
def test_batch_scripts_are_refused_on_windows(tmp_path: Path) -> None:
    _make_program(tmp_path / "bin", "abcnpm.cmd")
    with pytest.raises(ShellRequiredError):
        resolve_executable("abcnpm", tmp_path, str(tmp_path / "bin"))
    with pytest.raises(ShellRequiredError):
        resolve_executable("abcnpm.cmd", tmp_path, str(tmp_path / "bin"))
    with pytest.raises(ShellRequiredError):
        resolve_executable(str(tmp_path / "bin" / "abcnpm.cmd"), tmp_path, "")


@pytest.mark.skipif(not WINDOWS, reason="batch scripts are a Windows concept")
def test_batch_script_is_not_run_through_a_hidden_shell(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "batch-ran"
    (project / "build.cmd").write_text(f'@echo ran > "{marker}"\r\n', encoding="utf-8")
    monkeypatch.chdir(project)
    code, err = run(project, ".\\build.cmd")
    assert code == EXIT_CANNOT_EXECUTE
    assert "cmd /c" in err
    assert not marker.exists()
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["outcome"] == "spawn_error"
    assert finished.payload["error"] == {"type": "ShellRequiredError", "errno": None}


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_non_executable_file_is_a_permission_spawn_error(live: Recorder, project: Path) -> None:
    script = project / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    code, _ = run(project, str(script))
    assert code == EXIT_CANNOT_EXECUTE
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["error"] == {"type": "PermissionError", "errno": 13}


# -- CLI parsing --------------------------------------------------------------------------


def test_run_requires_double_dash(project: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        run_cli("run", "--project", str(project), "python")
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        run_cli("run", "--project", str(project))
    assert caught.value.code == 2


def test_run_requires_a_command_after_double_dash(project: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        run_cli("run", "--project", str(project), "--")
    assert caught.value.code == 2


def test_everything_after_double_dash_belongs_to_the_command(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    out = tmp_path / "argv.json"
    args = ["--project", "elsewhere", "--", "--help", "-h"]
    code, err = run(project, *py(DUMP_ARGV, str(out), *args))
    assert code == 0, err
    assert json.loads(out.read_text(encoding="utf-8")) == args


def test_run_defaults_to_the_current_directory(
    live: Recorder, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    marker = tmp_path / "ran"
    code, err = run_cli("run", "--", *py(TOUCH, str(marker)))
    assert code == 0, err
    assert marker.exists()


# -- recording ends while the child runs (in-process wrapper, real child) ------------------

WAIT_FOR_GO = (
    "import pathlib,sys,time\n"
    "go = pathlib.Path(sys.argv[1])\n"
    "deadline = time.monotonic() + 60\n"
    "while not go.exists() and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "sys.exit(3)\n"
)


def _run_in_thread(project: Path, argv: list[str]) -> tuple[threading.Thread, list[object]]:
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.extend(run(project, *argv)))
    thread.start()
    return thread, result


def test_recording_ending_mid_command_drops_only_the_finish(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    sid = live.session.session_id
    thread, result = _run_in_thread(project, py(WAIT_FOR_GO, str(go)))
    wait_until(lambda: len(command_events(live.store, sid)) == 1, what="command.started")
    live.stop()
    go.write_text("go")
    thread.join(timeout=60)
    code, err = result
    assert code == 3  # the child still ran to completion and its status is propagated
    assert "warning: the AgentBodycam recording ended before this command finished" in str(err)
    events = live.store.read_events(sid)
    assert events[-1].type is EventType.SESSION_STOPPED
    assert [e.type for e in events if e.type.value.startswith("command.")] == [
        EventType.COMMAND_STARTED
    ]


def test_finish_never_goes_into_a_newer_recording(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    first = live.session.session_id
    thread, result = _run_in_thread(project, py(WAIT_FOR_GO, str(go)))
    wait_until(lambda: len(command_events(live.store, first)) == 1, what="command.started")
    live.stop()
    newer = Recorder(EventStore(project), record_git=False)
    second = newer.start().session_id
    try:
        go.write_text("go")
        thread.join(timeout=60)
    finally:
        newer.stop()
    assert result[0] == 3
    assert "finish event was not recorded" in str(result[1])
    assert len(command_events(live.store, first)) == 1
    assert command_events(newer.store, second) == []


def test_started_at_is_not_backdated(live: Recorder, project: Path) -> None:
    before = datetime.now(UTC)
    assert run(project, *py("pass"))[0] == 0
    started, finished = command_events(live.store, live.session.session_id)
    assert before <= started.timestamp <= finished.timestamp
