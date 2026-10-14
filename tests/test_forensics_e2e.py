"""Forensics over real recordings: real recorder, real Git, real `agentbodycam run` + pytest."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentbodycam.cli import main, render_report
from agentbodycam.core.forensics import analyze_session
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType
from agentbodycam.models.forensics import FailureType, RecordingState
from command_helpers import RecorderProcess, run_cli, wait_until
from forensics_helpers import CAUSAL_LANGUAGE
from git_helpers import commit_all, init_repo, requires_git
from recording_helpers import store_bytes, write

PYTEST_ARGV = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py"]
CONTENT_MARKER = "CONTENT_MARKER_7f3a91"
OUTPUT_MARKER = "STDOUT_MARKER_c41d02"
ENV_MARKER = "ENV_MARKER_9b8e77"
SECRET = "ghp_" + "Qw8" * 12
TEST_FILE = (
    "from app import value\n\n"
    "def test_value():\n"
    f"    print('{OUTPUT_MARKER}')\n"
    "    assert value() == 1\n"
)


def analyze_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(["analyze", *argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def pytest_run(project: Path, *extra: str) -> int:
    code, err = run_cli("run", "--project", str(project), "--", *PYTEST_ARGV, *extra)
    assert code in (0, 1), err
    return code


@pytest.fixture
def demo(project: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[EventStore, str]]:
    """pytest PASS, modify, FAIL, modify, FAIL, modify, PASS, stop (real processes)."""
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("AGENTBODYCAM_TEST_SECRET_ENV", ENV_MARKER)
    init_repo(project)
    write(project / "app.py", f"# {CONTENT_MARKER}\ndef value():\n    return 1\n")
    write(project / "test_app.py", TEST_FILE)
    commit_all(project, "initial")
    store = EventStore(project)
    recorder = Recorder(store, git_interval=0)
    recorder.start()
    recorder.take_baseline()
    recorder.observe_git()

    def edit(body: str) -> None:
        write(project / "app.py", f"# {CONTENT_MARKER}\ndef value():\n    {body}\n")
        events = recorder.scan_once(force_git=True)
        assert any(e.type is EventType.FILE_MODIFIED and e.path == "app.py" for e in events)

    assert pytest_run(project) == 0
    edit("return 2")
    assert pytest_run(project) == 1
    edit("return 3  # second attempt")
    assert pytest_run(project) == 1
    edit("return 1  # third attempt, longer")
    assert pytest_run(project) == 0
    recorder.stop()
    yield store, recorder.session.session_id


@requires_git
def test_real_recording_forensic_interpretation(demo: tuple[EventStore, str]) -> None:
    store, sid = demo
    report = analyze_session(store, sid)
    events = store.read_events(sid)
    checks = [e for e in events if e.type in (EventType.CHECK_PASSED, EventType.CHECK_FAILED)]
    assert [e.type for e in checks] == [
        EventType.CHECK_PASSED,
        EventType.CHECK_FAILED,
        EventType.CHECK_FAILED,
        EventType.CHECK_PASSED,
    ]
    pass1, fail1, fail2, pass2 = checks

    assert report.recording_state is RecordingState.STOPPED and report.analysis_complete
    first = report.first_observable_failure
    assert first is not None
    assert first.event_id == fail1.event_id
    assert first.failure_type is FailureType.CHECK_FAILED
    assert first.summary == "Test failed: pytest (exit 1)"

    [recovery] = report.recoveries.items
    assert recovery.previous_pass_event_id == pass1.event_id
    assert (recovery.first_failed_event_id, recovery.last_failed_event_id) == (
        fail1.event_id,
        fail2.event_id,
    )
    assert recovery.recovered_event_id == pass2.event_id
    assert (recovery.failed_attempts, recovery.retry_count) == (2, 1)
    before = recovery.changes_before_failure
    assert before is not None
    assert [(c.change, c.path) for c in before.file_changes] == [("modified", "app.py")]
    between = recovery.changes_between_failure_and_pass
    assert [(c.change, c.path) for c in between.file_changes] == [("modified", "app.py")] * 2
    assert report.unresolved_failures.total == 0

    [state] = report.final_check_states.items
    assert (state.name, state.latest_result, state.attempts) == ("pytest", "passed", 4)
    assert state.identity_established and state.working_directory == "."

    files = report.file_change_summary
    assert (files.modified_events, files.created_events, files.unique_paths) == (3, 0, 1)
    git = report.latest_git_state
    assert git is not None and git.repository and git.branch == "main"
    assert git.state == "dirty" and git.unstaged == 1
    assert report.incomplete_commands.total == 0
    assert report.warnings.total == 0


@requires_git
def test_real_report_reflects_recorded_not_current_git(demo: tuple[EventStore, str]) -> None:
    store, sid = demo
    before = analyze_session(store, sid).to_json()
    commit_all(store.project_root, "after the recording")  # current repo is now clean
    (store.project_root / "app.py").unlink()  # and the current file system differs
    after = analyze_session(store, sid)
    assert after.to_json() == before
    assert after.latest_git_state is not None and after.latest_git_state.state == "dirty"


@requires_git
def test_real_report_privacy_and_wording(demo: tuple[EventStore, str]) -> None:
    store, sid = demo
    report = analyze_session(store, sid)
    for text in (report.to_json(), render_report(report)):
        assert CONTENT_MARKER not in text  # no file contents
        assert OUTPUT_MARKER not in text  # no stdout/stderr
        assert ENV_MARKER not in text  # no environment
        assert str(store.project_root) not in text
        assert json.dumps(str(store.project_root))[1:-1] not in text
        assert CAUSAL_LANGUAGE.search(text) is None, CAUSAL_LANGUAGE.search(text)


@requires_git
def test_real_analysis_is_read_only_and_deterministic(demo: tuple[EventStore, str]) -> None:
    store, sid = demo
    before = store_bytes(store.project_root)
    first = analyze_session(store, sid).to_json()
    second = analyze_session(EventStore(store.project_root), sid).to_json()
    assert first == second
    assert store_bytes(store.project_root) == before


@requires_git
def test_cli_analyze_human_and_json(demo: tuple[EventStore, str]) -> None:
    store, sid = demo
    project = str(store.project_root)
    code, text, err = analyze_cli("--project", project, sid)
    assert code == 0, err
    assert text.startswith("AgentBodycam forensic report\n")
    assert f"Session: {sid}" in text
    assert "First observable failure" in text and "Test failed: pytest (exit 1)" in text
    assert "and later passed at event" in text
    assert "Failed attempts: 2" in text
    assert "pytest" in text and "PASS" in text
    assert "\x1b" not in text

    report = analyze_session(store, sid)
    for argv in (("--json", "--project", project, sid), ("--project", project, sid, "--json")):
        code, out, err = analyze_cli(*argv)
        assert code == 0, err
        assert out == report.to_json() + "\n"
        assert json.loads(out)["recoveries"]["total"] == 1


def test_cli_analyze_from_project_directory(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    recorder.stop()
    monkeypatch.chdir(project)
    code, out, err = analyze_cli(recorder.session.session_id)
    assert code == 0, err
    assert "No observable check failure was recorded." in out


def test_cli_analyze_errors(project: Path) -> None:
    code, _, err = analyze_cli("--project", str(project), "not-a-session")
    assert code == 2 and "invalid session ID" in err
    code, _, err = analyze_cli("--project", str(project), "20261014T000000Z-0000000000000000")
    assert code == 1 and "session not found" in err
    assert not (project / ".agentbodycam").exists()  # analysis created nothing


def test_cli_help_lists_analyze(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["analyze", "--help"])
    text = capsys.readouterr().out
    assert "--json" in text and "never causes" in text


def test_live_recording_is_analysed_as_a_snapshot(project: Path) -> None:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    try:
        write(project / "a.txt", "x")
        recorder.scan_once()
        report = analyze_session(recorder.store, recorder.session.session_id)
        assert report.recording_state is RecordingState.RECORDING
        assert report.analysis_complete is False
        assert report.file_change_summary.created_events == 1
        assert any(w.code == "recording_in_progress" for w in report.warnings.items)
    finally:
        recorder.stop()
    report = analyze_session(recorder.store, recorder.session.session_id)
    assert report.recording_state is RecordingState.STOPPED and report.analysis_complete


def test_killed_recorder_session(project: Path) -> None:
    proc = RecorderProcess(project)
    try:
        code, err = run_cli(
            "run", "--project", str(project), "--", sys.executable, "-c", "pass", "--token", SECRET
        )
        assert code == 0, err
        store = EventStore(project)
        wait_until(lambda: len(store.read_events(proc.session_id)) >= 3, what="command events")
    finally:
        proc.kill()
    report = analyze_session(EventStore(project), proc.session_id)
    assert report.session_status == "recording"
    assert report.recording_state is RecordingState.ENDED_WITHOUT_STOP
    assert report.analysis_complete is True
    assert report.incomplete_commands.total == 0
    assert report.command_summary.commands_finished == 1
    text = report.to_json() + render_report(report)
    assert SECRET not in text
    assert "ended without a clean stop" in render_report(report)
