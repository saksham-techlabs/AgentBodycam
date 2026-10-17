"""``agentbodycam run`` check results: check.passed / check.failed with real child processes."""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentbodycam.core.checks import CheckClassification, CheckKind, ClassificationSource
from agentbodycam.core.command import (
    EXIT_NOT_FOUND,
    CommandResult,
    Outcome,
    check_draft,
    should_record_check,
)
from agentbodycam.core.forensics import analyze_session
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventSource, EventType
from command_helpers import CHECK_TYPES, command_events, py, run_cli, wait_until
from recording_helpers import store_bytes

HEX32 = re.compile(r"[0-9a-f]{32}")
PAYLOAD_KEYS = {
    "check_id",
    "command_id",
    "kind",
    "name",
    "classification",
    "result",
    "reason",
    "exit_code",
    "duration_ms",
}
EXIT = "import sys; sys.exit(int(sys.argv[1]))"


@pytest.fixture
def live(project: Path) -> Iterator[Recorder]:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    yield recorder
    if recorder.is_active:
        recorder.stop()


def run(project: Path, *argv: str, check: str | None = None) -> tuple[int, str]:
    options = [] if check is None else ["--check", check]
    return run_cli("run", "--project", str(project), *options, "--", *argv)


def events_of(live: Recorder) -> list[Event]:
    return live.store.read_events(live.session.session_id)


def checks_of(live: Recorder) -> list[Event]:
    return [e for e in events_of(live) if e.type in CHECK_TYPES]


def only_check(live: Recorder) -> tuple[Event, Event, Event]:
    """The single (command.started, command.finished, check event) triple."""
    started, finished = command_events(live.store, live.session.session_id)
    [check] = checks_of(live)
    return started, finished, check


def assert_paired(finished: Event, check: Event) -> None:
    assert finished.type is EventType.COMMAND_FINISHED
    assert check.sequence > finished.sequence
    assert check.payload["command_id"] == finished.payload["command_id"]
    assert check.payload["exit_code"] == finished.payload["exit_code"]
    assert check.payload["duration_ms"] == finished.payload["duration_ms"]


# -- explicit ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["test", "build", "lint", "typecheck"])
def test_explicit_check_passes(live: Recorder, project: Path, kind: str) -> None:
    code, err = run(project, *py(EXIT, "0"), check=kind)
    assert code == 0, err
    started, finished, check = only_check(live)
    assert started.sequence < finished.sequence < check.sequence
    assert check.type is EventType.CHECK_PASSED
    assert check.source is EventSource.COMMAND
    assert check.path is None
    payload = check.payload
    assert set(payload) == PAYLOAD_KEYS
    assert isinstance(payload["check_id"], str) and HEX32.fullmatch(payload["check_id"])
    assert payload["check_id"] != payload["command_id"]
    assert payload["command_id"] == started.payload["command_id"]
    assert payload["kind"] == kind
    assert payload["name"] == "python"
    assert payload["classification"] == "explicit"
    assert payload["result"] == "passed"
    assert payload["reason"] == "exit_zero"
    assert payload["exit_code"] == 0
    assert_paired(finished, check)
    assert check.summary == f"{kind.capitalize()} passed: python"


@pytest.mark.parametrize(("kind", "exit_code"), [("test", 1), ("build", 2), ("typecheck", 1)])
def test_explicit_check_fails_on_nonzero_exit(
    live: Recorder, project: Path, kind: str, exit_code: int
) -> None:
    code, _ = run(project, *py(EXIT, str(exit_code)), check=kind)
    assert code == exit_code  # the child's status is still propagated
    _, finished, check = only_check(live)
    assert check.type is EventType.CHECK_FAILED
    assert check.payload["result"] == "failed"
    assert check.payload["reason"] == "nonzero_exit"
    assert check.payload["exit_code"] == exit_code
    assert check.summary == f"{kind.capitalize()} failed: python (exit {exit_code})"
    assert_paired(finished, check)


def test_invalid_check_value_is_a_usage_error(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(SystemExit) as caught:
        run(project, *py(f"open({str(marker)!r}, 'w')"), check="compile")
    assert caught.value.code == 2
    assert not marker.exists()
    assert command_events(live.store, live.session.session_id) == []


def test_run_help_documents_checks(capsys: pytest.CaptureFixture[str]) -> None:
    from agentbodycam.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--help"])
    text = " ".join(capsys.readouterr().out.split())  # independent of terminal width
    assert "--check KIND" in text
    assert "test, build, lint, typecheck" in text
    assert "agentbodycam run --check test -- python -m pytest" in text
    assert "output is never analysed and no cause is inferred" in text


def test_check_option_must_come_before_double_dash(live: Recorder, project: Path) -> None:
    # After `--`, `--check` is just an argument of the command.
    code, _ = run_cli("run", "--project", str(project), "--", *py(EXIT, "0"), "--check", "test")
    assert code == 0
    assert checks_of(live) == []


def test_explicit_overrides_automatic(live: Recorder, project: Path) -> None:
    code, err = run(project, sys.executable, "-m", "pytest", "--version", check="build")
    assert code == 0, err
    _, _, check = only_check(live)
    assert check.payload["kind"] == "build"
    assert check.payload["name"] == "pytest"
    assert check.payload["classification"] == "explicit"
    assert check.summary == "Build passed: pytest"


# -- automatic ---------------------------------------------------------------------------


UNITTEST_FILE = (
    "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
)


@pytest.mark.parametrize(
    ("module", "kind", "name"),
    [
        (["pytest", "-q", "-p", "no:cacheprovider", "test_ok.py"], "test", "pytest"),
        (["unittest", "discover", "-p", "test_ok.py"], "test", "unittest"),
        (["mypy", "--cache-dir", "{cache}", "ok.py"], "typecheck", "mypy"),
        (["ruff", "check", "--no-cache", "--isolated", "ok.py"], "lint", "ruff"),
    ],
)
def test_automatic_classification_of_real_tools(
    live: Recorder, project: Path, tmp_path: Path, module: list[str], kind: str, name: str
) -> None:
    (project / "test_ok.py").write_text(UNITTEST_FILE, encoding="utf-8")
    (project / "ok.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    module = [str(tmp_path / "mypy-cache") if arg == "{cache}" else arg for arg in module]
    # Run from outside the project, so the child runs in the project root.
    code, err = run(project, sys.executable, "-B", "-m", *module)
    assert code == 0, err
    _, finished, check = only_check(live)
    assert check.type is EventType.CHECK_PASSED
    assert check.payload["kind"] == kind
    assert check.payload["name"] == name
    assert check.payload["classification"] == "automatic"
    assert_paired(finished, check)


@pytest.mark.parametrize(
    "module",
    [
        ["pytest", "--version"],
        ["pytest", "--help"],
        ["pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        ["unittest", "--help"],
        ["mypy", "--version"],
        ["ruff", "check", "--help"],
    ],
)
def test_informational_runs_are_commands_without_a_check(
    live: Recorder, project: Path, module: list[str]
) -> None:
    (project / "test_ok.py").write_text(UNITTEST_FILE, encoding="utf-8")
    code, err = run(project, sys.executable, "-B", "-m", *module)
    assert code == 0, err
    started, finished = command_events(live.store, live.session.session_id)
    assert started.type is EventType.COMMAND_STARTED
    assert finished.type is EventType.COMMAND_FINISHED
    assert finished.payload["exit_code"] == 0
    assert checks_of(live) == []


def test_pytest_executable_version_is_not_a_check(live: Recorder, project: Path) -> None:
    """`agentbodycam run -- pytest --version` (the pytest launcher next to this interpreter)."""
    scripts = Path(sys.executable).parent
    launcher = next((p for p in (scripts / "pytest.exe", scripts / "pytest") if p.is_file()), None)
    if launcher is None:
        pytest.skip("no pytest launcher next to the interpreter")
    code, err = run(project, str(launcher), "--version")
    assert code == 0, err
    assert len(command_events(live.store, live.session.session_id)) == 2
    assert checks_of(live) == []


def test_explicit_check_on_an_informational_run_is_honoured(live: Recorder, project: Path) -> None:
    code, err = run(project, sys.executable, "-m", "pytest", "--version", check="test")
    assert code == 0, err
    _, finished, check = only_check(live)
    assert check.type is EventType.CHECK_PASSED
    assert check.payload["kind"] == "test"
    assert check.payload["name"] == "pytest"
    assert check.payload["classification"] == "explicit"
    assert check.summary == "Test passed: pytest"
    assert_paired(finished, check)


def test_real_pytest_failure_then_success(live: Recorder, project: Path) -> None:
    # Same-length revisions can keep pytest's assertion-rewrite .pyc valid when the
    # filesystem does not advance mtime (common on hosted CI). Differing sizes force
    # importlib to invalidate the cache without sleeping for timestamp granularity.
    failing = "def test_value():\n    assert 1 == 2\n"
    passing = "def test_value():\n    assert 2 == 2  # recovered\n"
    assert len(failing.encode()) != len(passing.encode())
    test_file = project / "test_demo.py"
    test_file.write_text(failing, encoding="utf-8")
    # Run from outside the project, so the child runs in the project root.
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_demo.py"]
    first = run(project, *argv)
    test_file.write_text(passing, encoding="utf-8")
    assert test_file.read_text(encoding="utf-8") == passing
    second = run(project, *argv)
    assert first[0] == 1 and second[0] == 0, (first, second)
    failed, passed = checks_of(live)
    assert (
        failed.type is EventType.CHECK_FAILED and failed.summary == "Test failed: pytest (exit 1)"
    )
    assert passed.type is EventType.CHECK_PASSED and passed.summary == "Test passed: pytest"
    assert failed.payload["check_id"] != passed.payload["check_id"]
    assert failed.payload["command_id"] != passed.payload["command_id"]
    report = analyze_session(live.store, live.session.session_id)
    assert report.recoveries.total == 1
    [recovery] = report.recoveries.items
    assert recovery.recovered_event_id == passed.event_id
    assert recovery.first_failed_event_id == failed.event_id


@pytest.mark.parametrize(
    "argv",
    [
        ["-c", "pass"],
        ["script.py"],
        ["-m", "json.tool", "--help"],
    ],
)
def test_unclassified_commands_get_no_check_event(
    live: Recorder, project: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "script.py").write_text("print('hi')\n", encoding="utf-8")
    monkeypatch.chdir(project)
    code, err = run(project, sys.executable, *argv)
    assert code == 0, err
    assert len(command_events(live.store, live.session.session_id)) == 2
    assert checks_of(live) == []


def test_automatic_check_that_cannot_start_gets_no_check_event(
    live: Recorder, project: Path
) -> None:
    code, _ = run(project, "bin/pytest", "-q")  # recognised name, but nothing there
    assert code == EXIT_NOT_FOUND
    _, finished = command_events(live.store, live.session.session_id)
    assert finished.payload["outcome"] == "spawn_error"
    assert checks_of(live) == []


# -- spawn errors, interruption, signals -------------------------------------------------


def test_explicit_check_that_cannot_start_fails_with_spawn_error(
    live: Recorder, project: Path
) -> None:
    code, err = run(project, "no-such-program-4f1e", "--token", "S3CRET-77", check="test")
    assert code == EXIT_NOT_FOUND
    assert "executable not found" in err
    _, finished, check = only_check(live)
    assert finished.payload["outcome"] == "spawn_error"
    assert check.type is EventType.CHECK_FAILED
    assert check.payload["result"] == "failed"
    assert check.payload["reason"] == "spawn_error"
    assert check.payload["exit_code"] is None
    assert check.payload["name"] == "no-such-program-4f1e"
    assert check.summary == "Test failed: no-such-program-4f1e (could not start)"
    assert_paired(finished, check)
    assert b"S3CRET-77" not in store_bytes(project)


def _result(outcome: Outcome, **overrides: object) -> CommandResult:
    fields: dict[str, object] = {
        "exit_code": None,
        "signal": None,
        "duration_ms": 42,
        "pid": 1234,
        "interrupts": (),
    }
    fields.update(overrides)
    return CommandResult(outcome, **fields)  # type: ignore[arg-type]


AUTOMATIC = CheckClassification(CheckKind.TEST, "pytest", ClassificationSource.AUTOMATIC)
EXPLICIT = CheckClassification(CheckKind.BUILD, "npm build", ClassificationSource.EXPLICIT)


@pytest.mark.parametrize(
    ("result", "event_type", "reason", "summary"),
    [
        (_result(Outcome.EXITED, exit_code=0), "check.passed", "exit_zero", "Test passed: pytest"),
        (
            _result(Outcome.EXITED, exit_code=3),
            "check.failed",
            "nonzero_exit",
            "Test failed: pytest (exit 3)",
        ),
        (
            _result(Outcome.SIGNALED, signal=9),
            "check.failed",
            "signaled",
            "Test failed: pytest (signal SIGKILL)" if sys.platform != "win32" else None,
        ),
        (
            _result(Outcome.INTERRUPTED, exit_code=0, interrupts=("SIGINT",)),
            "check.failed",
            "interrupted",
            "Test failed: pytest (interrupted, exit 0)",
        ),
        (
            _result(Outcome.INTERRUPTED, signal=2, interrupts=("SIGINT",)),
            "check.failed",
            "interrupted",
            "Test failed: pytest (interrupted)",
        ),
    ],
)
def test_check_draft_outcomes(
    result: CommandResult, event_type: str, reason: str, summary: str | None
) -> None:
    draft = check_draft("ab" * 16, "cd" * 16, AUTOMATIC, result)
    assert draft.type.value == event_type
    assert draft.source is EventSource.COMMAND
    assert draft.path is None
    assert draft.payload["reason"] == reason
    assert draft.payload["exit_code"] == result.exit_code
    assert draft.payload["duration_ms"] == 42
    assert draft.payload["check_id"] == "ab" * 16
    assert draft.payload["command_id"] == "cd" * 16
    if summary is not None:
        assert draft.summary == summary
    else:
        assert draft.summary.startswith("Test failed: pytest (signal ")


def test_which_results_get_a_check_event() -> None:
    not_started = _result(Outcome.SPAWN_ERROR, pid=None, error_type="FileNotFoundError")
    interrupted_early = _result(Outcome.INTERRUPTED, pid=None, interrupts=("SIGINT",))
    ran = _result(Outcome.EXITED, exit_code=1)
    assert should_record_check(EXPLICIT, not_started)
    assert should_record_check(EXPLICIT, interrupted_early)
    assert should_record_check(EXPLICIT, ran)
    assert not should_record_check(AUTOMATIC, not_started)
    assert not should_record_check(AUTOMATIC, interrupted_early)
    assert should_record_check(AUTOMATIC, ran)
    draft = check_draft("ab" * 16, "cd" * 16, EXPLICIT, interrupted_early)
    assert draft.summary == "Build failed: npm build (interrupted)"
    assert draft.payload["exit_code"] is None


# -- repeated checks ---------------------------------------------------------------------


def test_repeated_checks_are_independent(live: Recorder, project: Path) -> None:
    for exit_code in (1, 1, 0):
        run(project, *py(EXIT, str(exit_code)), check="test")
    events = events_of(live)
    checks = [e for e in events if e.type in CHECK_TYPES]
    assert [e.type for e in checks] == [
        EventType.CHECK_FAILED,
        EventType.CHECK_FAILED,
        EventType.CHECK_PASSED,
    ]
    assert len({e.payload["check_id"] for e in checks}) == 3
    finished = {e.payload["command_id"]: e for e in events if e.type is EventType.COMMAND_FINISHED}
    for check in checks:
        assert_paired(finished[check.payload["command_id"]], check)
    assert all(set(e.payload) == PAYLOAD_KEYS for e in checks)  # no derived "recovery" field


# -- recording ends ------------------------------------------------------------------------

WAIT_FOR_GO = (
    "import pathlib,sys,time\n"
    "go = pathlib.Path(sys.argv[1])\n"
    "deadline = time.monotonic() + 60\n"
    "while not go.exists() and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "sys.exit(0)\n"
)


def _run_in_thread(
    project: Path, argv: list[str], check: str
) -> tuple[threading.Thread, list[object]]:
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.extend(run(project, *argv, check=check)))
    thread.start()
    return thread, result


def test_no_check_event_when_the_recording_ends_before_the_finish(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    sid = live.session.session_id
    thread, result = _run_in_thread(project, py(WAIT_FOR_GO, str(go)), "test")
    wait_until(lambda: len(command_events(live.store, sid)) == 1, what="command.started")
    live.stop()
    go.write_text("go")
    thread.join(timeout=60)
    assert result[0] == 0
    assert "finish event was not recorded" in str(result[1])
    assert "check result" not in str(result[1])  # the check was never attempted
    events = live.store.read_events(sid)
    assert events[-1].type is EventType.SESSION_STOPPED
    assert [e.type for e in events if e.type in CHECK_TYPES] == []


def test_no_check_event_goes_into_a_newer_recording(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    first = live.session.session_id
    thread, _ = _run_in_thread(project, py(WAIT_FOR_GO, str(go)), "build")
    wait_until(lambda: len(command_events(live.store, first)) == 1, what="command.started")
    live.stop()
    newer = Recorder(EventStore(project), record_git=False)
    second = newer.start().session_id
    try:
        go.write_text("go")
        thread.join(timeout=60)
    finally:
        newer.stop()
    for sid in (first, second):
        assert [e for e in live.store.read_events(sid) if e.type in CHECK_TYPES] == []


def test_check_is_skipped_if_the_recording_ends_after_the_finish(
    live: Recorder, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recording stops between command.finished and the check: nothing late is written."""
    import agentbodycam.core.command as command_module

    original = command_module.append_to_recording

    def append_then_stop(store: EventStore, attachment: object, draft: object) -> Event:
        event = original(store, attachment, draft)  # type: ignore[arg-type]
        if event.type is EventType.COMMAND_FINISHED:
            live.stop()
        return event

    monkeypatch.setattr(command_module, "append_to_recording", append_then_stop)
    code, err = run(project, *py(EXIT, "0"), check="lint")
    assert code == 0
    assert "the AgentBodycam recording ended before the check result could be recorded" in err
    events = events_of(live)
    assert events[-1].type is EventType.SESSION_STOPPED
    assert EventType.COMMAND_FINISHED in [e.type for e in events]
    assert [e for e in events if e.type in CHECK_TYPES] == []


# -- privacy -------------------------------------------------------------------------------


def test_secret_arguments_never_reach_check_events(live: Recorder, project: Path) -> None:
    secrets = ["flag-secret-111aaa", "inline-secret-222bbb", "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1"]
    args = ["--token", secrets[0], f"--api-key={secrets[1]}", secrets[2]]
    code, err = run(project, *py("pass"), *args, check="test")
    assert code == 0, err
    _, _, check = only_check(live)
    line = check.to_json_line()
    for value in secrets:
        assert value not in line
        assert value not in check.summary
    assert check.payload["name"] == "python"
    assert "--token" not in line and "-c" not in check.summary


def test_check_events_hold_no_output_environment_or_absolute_paths(
    live: Recorder,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ABC_CHECK_ENV_6d1", "env-value-secret-4c2")
    code = (
        "import os,sys; print('OUT-' + 'MARK-31a'); print('ERR-' + 'MARK-42b', file=sys.stderr);"
        "sys.exit(0 if os.environ['ABC_CHECK_' + 'ENV_6d1'] else 1)"
    )
    status, _ = run(project, sys.executable, "-c", code, check="test")
    assert status == 0
    captured = capfd.readouterr()
    assert "OUT-MARK-31a" in captured.out and "ERR-MARK-42b" in captured.err
    _, _, check = only_check(live)
    line = check.to_json_line()
    data = store_bytes(project)
    for marker in ("OUT-MARK-31a", "ERR-MARK-42b", "env-value-secret-4c2", "ABC_CHECK_ENV_6d1"):
        assert marker.encode() not in data
    home = str(Path.home())
    for path in (home, str(project), str(Path(sys.executable).parent)):
        assert path not in line
        assert json.dumps(path)[1:-1] not in line
    assert check.payload["name"] == "python"
