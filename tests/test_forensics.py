"""Forensics engine: precise synthetic sessions written through the real event store."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from agentbodycam.cli import render_report
from agentbodycam.core import forensics
from agentbodycam.core.checks import CheckKind
from agentbodycam.core.command import Outcome
from agentbodycam.core.forensics import (
    MAX_EVIDENCE_REFS,
    MAX_FINDINGS,
    MAX_WINDOW_CHANGES,
    ForensicInputError,
    analyze_events,
    analyze_session,
    check_signature,
)
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType, JsonObject
from agentbodycam.models.forensics import FailureType, ForensicReport, RecordingState
from forensics_helpers import (
    CAUSAL_LANGUAGE,
    PYTEST,
    SessionBuilder,
    probe_active,
    probe_inactive,
    synthetic_events,
)
from recording_helpers import store_bytes


@pytest.fixture
def b(store: EventStore) -> SessionBuilder:
    return SessionBuilder(store)


def analyze(b: SessionBuilder, **kwargs: object) -> ForensicReport:
    return analyze_session(b.store, b.session_id, probe=probe_inactive, **kwargs)  # type: ignore[arg-type]


def results(report: ForensicReport) -> dict[str, str]:
    return {s.display_name: s.latest_result for s in report.final_check_states.items}


# -- 1-3: basics -----------------------------------------------------------------------------


def test_no_checks(b: SessionBuilder) -> None:
    b.file("modified", "app.py")
    b.run_command(["python", "script.py"])
    b.stop()
    report = analyze(b)
    assert report.first_observable_failure is None
    assert report.observable_failures.total == 0
    assert report.recoveries.total == 0 and report.unresolved_failures.total == 0
    assert report.final_check_states.total == 0
    assert report.command_summary.commands_started == 1
    text = render_report(report)
    assert "No observable check failure was recorded." in text
    assert "no check results were recorded" in text


def test_all_checks_pass(b: SessionBuilder) -> None:
    b.run_check(True)
    b.file("modified", "app.py")
    b.run_check(True)
    b.stop()
    report = analyze(b)
    assert report.first_observable_failure is None
    assert report.recoveries.total == 0 and report.unresolved_failures.total == 0
    [state] = report.final_check_states.items
    assert (state.latest_result, state.attempts, state.passed, state.failed) == ("passed", 2, 2, 0)
    assert state.identity_established


def test_first_check_fails(b: SessionBuilder) -> None:
    started, finished, failed = b.run_check(False)
    b.stop()
    report = analyze(b)
    first = report.first_observable_failure
    assert first is not None
    assert (first.event_id, first.sequence) == (failed.event_id, failed.sequence)
    assert first.failure_type is FailureType.CHECK_FAILED
    assert (first.check_kind, first.check_name) == ("test", "pytest")
    assert first.summary == "Test failed: pytest (exit 1)"
    assert first.exit_code == 1 and first.reason == "nonzero_exit"
    assert first.evidence_event_ids == (started.event_id, finished.event_id, failed.event_id)
    assert first.check_signature == check_signature("test", "pytest", started)
    assert first.finding_evidence == "observed"


# -- 4-9: failure / recovery periods ---------------------------------------------------------


def test_pass_then_fail(b: SessionBuilder) -> None:
    *_, passed = b.run_check(True)
    change = b.file("modified", "src/auth.py")
    *_, failed = b.run_check(False)
    b.stop()
    report = analyze(b)
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == failed.event_id
    [unresolved] = report.unresolved_failures.items
    assert unresolved.previous_pass_event_id == passed.event_id
    assert unresolved.previous_pass_note is None
    window = unresolved.changes_before_failure
    assert window is not None
    assert (window.after_sequence, window.before_sequence) == (passed.sequence, failed.sequence)
    assert [c.event_id for c in window.file_changes] == [change.event_id]
    assert "after the last recorded passing check" in window.description
    assert results(report) == {"pytest": "failed"}


def test_fail_then_pass(b: SessionBuilder) -> None:
    *_, failed = b.run_check(False)
    b.file("modified", "app.py")
    *_, passed = b.run_check(True)
    b.stop()
    report = analyze(b)
    [recovery] = report.recoveries.items
    assert recovery.first_failed_event_id == failed.event_id == recovery.last_failed_event_id
    assert (recovery.recovered_event_id, recovery.recovered_sequence) == (
        passed.event_id,
        passed.sequence,
    )
    assert (recovery.failed_attempts, recovery.retry_count) == (1, 0)
    assert recovery.changes_before_failure is None
    assert recovery.previous_pass_note == forensics.NO_EARLIER_PASSING_RESULT
    assert recovery.summary == (
        f"pytest failed at event #{failed.sequence} and later passed at event "
        f"#{passed.sequence} after 1 additional recorded attempt."
    )
    assert report.unresolved_failures.total == 0
    assert results(report) == {"pytest": "passed"}


def test_fail_fail_pass(b: SessionBuilder) -> None:
    *_, first = b.run_check(False)
    *_, second = b.run_check(False)
    *_, passed = b.run_check(True)
    b.stop()
    [recovery] = analyze(b).recoveries.items
    assert (recovery.failed_attempts, recovery.retry_count) == (2, 1)
    assert recovery.first_failed_event_id == first.event_id
    assert recovery.last_failed_event_id == second.event_id
    assert recovery.recovered_event_id == passed.event_id
    assert "after 2 additional recorded attempts" in recovery.summary


def test_pass_fail_fail_pass(b: SessionBuilder) -> None:
    *_, p1 = b.run_check(True)
    before = b.file("modified", "src/auth.py")
    *_, f1 = b.run_check(False)
    between = b.file("modified", "src/auth.py")
    *_, f2 = b.run_check(False)
    after = b.file("created", "tests/test_auth.py")
    *_, p2 = b.run_check(True)
    b.stop()
    report = analyze(b)
    [recovery] = report.recoveries.items
    assert recovery.previous_pass_event_id == p1.event_id
    assert (recovery.first_failed_event_id, recovery.last_failed_event_id) == (
        f1.event_id,
        f2.event_id,
    )
    assert recovery.recovered_event_id == p2.event_id
    assert recovery.retry_count == 1
    assert recovery.changes_before_failure is not None
    assert [c.event_id for c in recovery.changes_before_failure.file_changes] == [before.event_id]
    window = recovery.changes_between_failure_and_pass
    assert [c.event_id for c in window.file_changes] == [between.event_id, after.event_id]
    assert (window.file_change_count, window.modified, window.created) == (2, 1, 1)
    assert [c.event_id for c in recovery.changes_after_last_failure.file_changes] == [
        after.event_id
    ]
    for event in (p1, f1, f2, p2):
        assert event.event_id in recovery.evidence_event_ids
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == f1.event_id


def test_fail_pass_fail_pass_is_two_periods(b: SessionBuilder) -> None:
    *_, f1 = b.run_check(False)
    *_, p1 = b.run_check(True)
    *_, f2 = b.run_check(False)
    *_, p2 = b.run_check(True)
    b.stop()
    report = analyze(b)
    first, second = report.recoveries.items
    assert (first.first_failed_event_id, first.recovered_event_id) == (f1.event_id, p1.event_id)
    assert (second.first_failed_event_id, second.recovered_event_id) == (f2.event_id, p2.event_id)
    assert first.previous_pass_event_id is None
    assert second.previous_pass_event_id == p1.event_id
    assert report.observable_failures.total == 2


def test_pass_fail_fail_pass_fail_pass_is_separate_periods(b: SessionBuilder) -> None:
    for passed in (True, False, False, True, False, True):
        b.run_check(passed)
    b.stop()
    report = analyze(b)
    assert [r.failed_attempts for r in report.recoveries.items] == [2, 1]
    assert report.unresolved_failures.total == 0


def test_final_unresolved_failure(b: SessionBuilder) -> None:
    *_, p = b.run_check(True)
    *_, f1 = b.run_check(False)
    *_, f2 = b.run_check(False)
    *_, f3 = b.run_check(False)
    later = b.file("modified", "app.py")
    b.stop()
    report = analyze(b)
    [unresolved] = report.unresolved_failures.items
    assert (unresolved.failed_attempts, unresolved.retry_count) == (3, 2)
    assert unresolved.first_failed_event_id == f1.event_id
    assert unresolved.last_failed_event_id == f3.event_id
    assert unresolved.previous_pass_event_id == p.event_id
    assert "unresolved in this recording" in unresolved.summary
    assert "pytest remained failing across 3 recorded attempts." in unresolved.summary
    assert [c.event_id for c in unresolved.changes_after_last_failure.file_changes] == [
        later.event_id
    ]
    assert unresolved.changes_after_last_failure.before_sequence is None
    assert f2.event_id in unresolved.evidence_event_ids


# -- 10-13: check identity -------------------------------------------------------------------


def test_independent_check_signatures(b: SessionBuilder) -> None:
    b.run_check(False, PYTEST)
    b.run_check(False, ["python", "-m", "mypy", "src"], name="mypy", kind=CheckKind.TYPECHECK)
    b.run_check(True, PYTEST)
    b.stop()
    report = analyze(b)
    assert results(report) == {"pytest": "passed", "mypy": "failed"}
    [recovery] = report.recoveries.items
    [unresolved] = report.unresolved_failures.items
    assert (recovery.name, unresolved.name) == ("pytest", "mypy")
    assert report.command_summary.check_signatures == 2


def test_same_tool_different_argv_is_not_grouped(b: SessionBuilder) -> None:
    b.run_check(False, ["python", "-m", "pytest", "tests/auth"])
    b.run_check(True, ["python", "-m", "pytest", "tests/billing"])
    b.stop()
    report = analyze(b)
    assert report.recoveries.total == 0
    [unresolved] = report.unresolved_failures.items
    assert unresolved.name == "pytest"
    names = [s.display_name for s in report.final_check_states.items]
    assert len(names) == 2 and len(set(names)) == 2
    assert all(name.startswith("pytest [") for name in names)


def test_same_argv_and_cwd_is_grouped(b: SessionBuilder) -> None:
    argv = ["python", "-m", "pytest", "tests/auth", "-q"]
    s1, _, _ = b.run_check(False, argv, cwd="pkg")
    s2, _, _ = b.run_check(True, argv, cwd="pkg")
    b.stop()
    assert check_signature("test", "pytest", s1) == check_signature("test", "pytest", s2)
    assert analyze(b).recoveries.total == 1


def test_same_argv_different_cwd_is_not_grouped(b: SessionBuilder) -> None:
    b.run_check(False, PYTEST, cwd="service-a")
    b.run_check(True, PYTEST, cwd="service-b")
    b.stop()
    report = analyze(b)
    assert report.recoveries.total == 0 and report.unresolved_failures.total == 1
    assert {s.working_directory for s in report.final_check_states.items} == {
        "service-a",
        "service-b",
    }


def test_different_kind_or_name_is_not_grouped(b: SessionBuilder) -> None:
    b.run_check(False, PYTEST, kind=CheckKind.TEST)
    b.run_check(True, PYTEST, kind=CheckKind.LINT)
    b.stop()
    assert analyze(b).recoveries.total == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "pytest", "--token", "hunter2"],  # redacted
        ["python", "-m", "pytest", "/definitely/outside/tests"],  # <external-path>
        ["python", "-m", "pytest", *(["x"] * 80)],  # truncated
    ],
)
def test_lossy_recorded_argv_is_never_grouped(b: SessionBuilder, argv: list[str]) -> None:
    if argv[3].startswith("/") and sys.platform == "win32":
        argv = [*argv[:3], "D:\\definitely\\outside\\tests"]
    s1, _, _ = b.run_check(False, argv)
    b.run_check(True, argv)
    b.stop()
    assert check_signature("test", "pytest", s1) is None
    report = analyze(b)
    assert report.recoveries.total == 0
    assert report.unresolved_failures.total == 0
    assert report.command_summary.checks_ungrouped == 2
    assert all(not s.identity_established for s in report.final_check_states.items)
    assert any(w.code == "ungrouped_checks" for w in report.warnings.items)
    # The failure itself is still an observable failure.
    assert report.first_observable_failure is not None


def test_signature_uses_only_recorded_fields(b: SessionBuilder) -> None:
    started, _, _ = b.run_check(True, ["python", "-m", "pytest", "-q"])
    expected = check_signature("test", "pytest", started)
    assert expected is not None and len(expected) == 64
    payload = started.payload | {"command_id": "e" * 32, "wrapper": {"pid": 99999}}
    other = dataclasses.replace(started, payload=payload, event_id="f" * 32, sequence=999)
    assert check_signature("test", "pytest", other) == expected  # pid/ids are not identity


# -- 14-18: commands -------------------------------------------------------------------------


def test_command_started_without_finish(b: SessionBuilder) -> None:
    started = b.start(["python", "long_job.py"])
    b.stop()
    report = analyze(b)
    [incomplete] = report.incomplete_commands.items
    assert incomplete.command_id == started.payload["command_id"]
    assert (incomplete.started_event_id, incomplete.started_sequence) == (
        started.event_id,
        started.sequence,
    )
    assert incomplete.executable == "python"
    assert incomplete.summary == "Command started but no finish event was recorded."
    assert incomplete.evidence_event_ids == (started.event_id,)
    assert report.first_observable_failure is None  # not a failure, not "crashed"
    assert report.warnings.total == 0  # legitimate, not corruption


def test_check_links_to_its_command(b: SessionBuilder) -> None:
    started, finished, check = b.run_check(False)
    b.stop()
    report = analyze(b)
    [failure] = report.observable_failures.items
    assert failure.command_id == started.payload["command_id"] == check.payload["command_id"]
    assert failure.evidence_event_ids == (started.event_id, finished.event_id, check.event_id)
    [item] = [i for i in report.timeline.items if i.event_id == check.event_id]
    assert item.command_id == started.payload["command_id"]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.SPAWN_ERROR, FailureType.COMMAND_SPAWN_ERROR),
        (Outcome.SIGNALED, FailureType.COMMAND_SIGNALED),
        (Outcome.INTERRUPTED, FailureType.COMMAND_INTERRUPTED),
    ],
)
def test_operational_command_failures(
    b: SessionBuilder, outcome: Outcome, expected: FailureType
) -> None:
    started, finished = b.run_command(["missing-tool", "go"], exit_code=None, outcome=outcome)
    b.stop()
    report = analyze(b)
    first = report.first_observable_failure
    assert first is not None
    assert first.failure_type is expected
    assert first.event_id == finished.event_id
    assert first.evidence_event_ids == (started.event_id, finished.event_id)
    assert first.check_signature is None


def test_unclassified_nonzero_exit_is_not_a_failure(b: SessionBuilder) -> None:
    b.run_command(["git", "diff", "--quiet"], exit_code=1)
    b.run_command(["python", "script.py"], exit_code=2)
    b.stop()
    report = analyze(b)
    assert report.first_observable_failure is None
    assert report.observable_failures.total == 0


def test_failed_check_command_is_not_reported_twice(b: SessionBuilder) -> None:
    started = b.start(PYTEST)
    b.finish(started, exit_code=None, outcome=Outcome.INTERRUPTED)
    check = b.check(started, "pytest", exit_code=None, outcome=Outcome.INTERRUPTED)
    b.stop()
    report = analyze(b)
    [failure] = report.observable_failures.items
    assert failure.event_id == check.event_id
    assert failure.failure_type is FailureType.CHECK_FAILED


def test_errors_observed_are_separate(b: SessionBuilder) -> None:
    error = b.error()
    b.stop()
    report = analyze(b)
    assert report.first_observable_failure is None
    [observed] = report.errors_observed.items
    assert (observed.event_id, observed.operation, observed.error, observed.path) == (
        error.event_id,
        "hash",
        "PermissionError",
        "locked.bin",
    )
    assert "Errors observed" in render_report(report)


# -- 19-21: change windows -------------------------------------------------------------------


def test_window_excludes_non_file_events_and_bounds(b: SessionBuilder) -> None:
    *_, passed = b.run_check(True)
    b.git()
    b.run_command(["python", "gen.py"])
    b.error()
    changes = [b.file("modified", f"src/m{i:02}.py") for i in range(MAX_WINDOW_CHANGES + 5)]
    *_, failed = b.run_check(False)
    b.stop()
    [unresolved] = analyze(b).unresolved_failures.items
    window = unresolved.changes_before_failure
    assert window is not None
    assert window.file_change_count == len(changes)
    assert len(window.file_changes) == MAX_WINDOW_CHANGES
    assert window.file_changes_truncated is True
    assert [c.event_id for c in window.file_changes] == [
        c.event_id for c in changes[:MAX_WINDOW_CHANGES]
    ]
    assert passed.sequence < window.file_changes[0].sequence < failed.sequence


def test_no_previous_pass_means_no_regression_window(b: SessionBuilder) -> None:
    b.file("modified", "app.py")
    b.run_check(False)
    b.stop()
    report = analyze(b)
    [unresolved] = report.unresolved_failures.items
    assert unresolved.changes_before_failure is None
    assert unresolved.previous_pass_event_id is None
    note = "No earlier passing result for this exact check was recorded."
    assert unresolved.previous_pass_note == note == forensics.NO_EARLIER_PASSING_RESULT
    assert note in render_report(report)


def test_pass_of_a_different_check_is_not_a_previous_pass(b: SessionBuilder) -> None:
    b.run_check(True, ["python", "-m", "pytest", "tests/billing"])
    b.file("modified", "app.py")
    b.run_check(False, ["python", "-m", "pytest", "tests/auth"])
    b.stop()
    report = analyze(b)
    [unresolved] = report.unresolved_failures.items
    assert unresolved.changes_before_failure is None


# -- 22-25: final state, sessions, ordering --------------------------------------------------


def test_latest_git_snapshot_is_used_and_git_never_runs(
    b: SessionBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_git(*_a: object, **_k: object) -> None:
        raise AssertionError("forensics must not run Git")

    monkeypatch.setattr("agentbodycam.utils.git.run_git", no_git)
    monkeypatch.setattr("subprocess.Popen", no_git)
    b.git(commit="a" * 40, state="clean")
    last = b.git(branch="feature", commit="b" * 40, state="dirty", counts=(1, 2, 3, 0))
    b.stop()
    git = analyze(b).latest_git_state
    assert git is not None
    assert (git.event_id, git.sequence, git.snapshot_count) == (last.event_id, last.sequence, 2)
    assert (git.repository, git.branch, git.head, git.state) == (
        True,
        "feature",
        "bbbbbbb",
        "dirty",
    )
    assert (git.staged, git.unstaged, git.untracked, git.conflicted) == (1, 2, 3, 0)
    assert "not the current Git state" in git.description
    assert "private-root" not in json.dumps(git.to_dict())  # absolute root is not exposed


def test_no_git_snapshot(b: SessionBuilder) -> None:
    b.stop()
    report = analyze(b)
    assert report.latest_git_state is None
    assert "Last recorded Git state: none recorded" in render_report(report)


def test_stopped_session_is_complete(b: SessionBuilder) -> None:
    b.stop()
    report = analyze(b)
    assert report.recording_state is RecordingState.STOPPED
    assert report.analysis_complete is True
    assert report.session_status == "stopped" and report.ended_at is not None


def test_session_without_stop_and_no_live_recorder(b: SessionBuilder) -> None:
    started = b.start(["python", "train.py"])
    report = analyze_session(b.store, b.session_id, probe=probe_inactive)
    assert report.recording_state is RecordingState.ENDED_WITHOUT_STOP
    assert report.session_status == "recording"
    assert report.analysis_complete is True
    assert any(w.code == "recording_ended_without_stop" for w in report.warnings.items)
    [incomplete] = report.incomplete_commands.items
    assert incomplete.started_event_id == started.event_id
    assert "may still be running" not in incomplete.summary


def test_live_session_is_a_snapshot(b: SessionBuilder) -> None:
    b.start(["python", "train.py"])
    report = analyze_session(b.store, b.session_id, probe=probe_active(b.session_id))
    assert report.recording_state is RecordingState.RECORDING
    assert report.analysis_complete is False
    assert "may still be running" in report.incomplete_commands.items[0].summary


def test_unknown_liveness_is_incomplete(b: SessionBuilder) -> None:
    def broken(_root: Path) -> object:
        raise OSError("lock unreadable")

    report = analyze_session(b.store, b.session_id, probe=broken)  # type: ignore[arg-type]
    assert report.recording_state is RecordingState.UNKNOWN
    assert report.analysis_complete is False


def test_another_live_session_means_this_one_ended(b: SessionBuilder) -> None:
    other = "20990101T000000Z-0123456789abcdef"
    report = analyze_session(b.store, b.session_id, probe=probe_active(other))
    assert report.recording_state is RecordingState.ENDED_WITHOUT_STOP


def test_concurrent_commands_are_paired_by_command_id(b: SessionBuilder) -> None:
    a = b.start(["python", "-m", "pytest", "tests/a"])
    c = b.start(["python", "-m", "pytest", "tests/c"])
    b.finish(c, exit_code=0)
    b.finish(a, exit_code=1)
    check_c = b.check(c, "pytest", exit_code=0)
    check_a = b.check(a, "pytest", exit_code=1)
    b.stop()
    report = analyze(b)
    assert report.warnings.total == 0
    [failure] = report.observable_failures.items
    assert failure.event_id == check_a.event_id
    assert failure.command_id == a.payload["command_id"]
    by_id = {s.latest_event_id: s.latest_result for s in report.final_check_states.items}
    assert by_id == {check_c.event_id: "passed", check_a.event_id: "failed"}


def test_timeline_follows_sequence_not_timestamps(b: SessionBuilder) -> None:
    b.file("created", "a.py")
    b.run_check(True)
    b.stop()
    report = analyze(b)
    sequences = [item.sequence for item in report.timeline.items]
    assert sequences == list(range(1, report.event_count + 1))
    events = b.store.read_events(b.session_id)
    # Shuffle timestamps: the report's order and findings do not change.
    reversed_ts = [
        dataclasses.replace(e, timestamp=events[-1 - i].timestamp) for i, e in enumerate(events)
    ]
    session = b.store.load_session(b.session_id)
    a = analyze_events(session, events).to_dict()
    c = analyze_events(session, reversed_ts).to_dict()
    for data in (a, c):
        data.pop("timeline")
    assert a == c
    item = report.timeline.items[1]
    assert set(item.to_dict()) == {
        "sequence",
        "event_id",
        "timestamp",
        "type",
        "category",
        "source",
        "summary",
        "path",
        "command_id",
        "evidence",
    }
    assert item.evidence == "observed" and item.path == "a.py"


def test_events_must_be_complete_and_ordered(b: SessionBuilder) -> None:
    b.file("created", "a.py")
    events = b.store.read_events(b.session_id)
    session = b.store.load_session(b.session_id)
    with pytest.raises(ForensicInputError):
        analyze_events(session, list(reversed(events)))
    with pytest.raises(ForensicInputError):
        analyze_events(session, events[1:])


# -- relational corruption -------------------------------------------------------------------


def codes(report: ForensicReport) -> set[str]:
    return {w.code for w in report.warnings.items}


def test_duplicate_command_started_is_excluded(b: SessionBuilder) -> None:
    started = b.start(PYTEST, command_id="1" * 32)
    b.start(PYTEST, command_id="1" * 32)
    b.finish(started, exit_code=1)
    b.check(started, "pytest", exit_code=1)
    b.stop()
    report = analyze(b)
    assert "duplicate_command_started" in codes(report)
    assert report.observable_failures.total == 0
    assert report.final_check_states.total == 0
    assert report.incomplete_commands.total == 0
    assert report.timeline.total == report.event_count  # evidence still shown


def test_check_without_command_start(b: SessionBuilder) -> None:
    ghost = b.start(PYTEST, command_id="2" * 32)  # a real start for another id
    b.finish(ghost)
    b.check(ghost, "pytest", exit_code=1, command_id="3" * 32)
    b.stop()
    report = analyze(b)
    assert "check_without_command_start" in codes(report)
    assert report.observable_failures.total == 0


def test_check_before_command_finished(b: SessionBuilder) -> None:
    started = b.start(PYTEST)
    b.check(started, "pytest", exit_code=1)
    b.finish(started, exit_code=1)
    b.stop()
    report = analyze(b)
    assert "check_before_command_finished" in codes(report)
    assert report.observable_failures.total == 0 and report.final_check_states.total == 0


def test_finish_without_start(b: SessionBuilder) -> None:
    started = b.start(PYTEST, command_id="4" * 32)
    b.finish(started, command_id="5" * 32, exit_code=None, outcome=Outcome.SPAWN_ERROR)
    b.stop()
    report = analyze(b)
    assert "command_finished_without_start" in codes(report)
    assert report.observable_failures.total == 0
    assert report.incomplete_commands.total == 1  # the real start is still legitimate


def test_check_contradicting_its_finish(b: SessionBuilder) -> None:
    started = b.start(PYTEST)
    b.finish(started, exit_code=1)
    b.check(started, "pytest", exit_code=0)  # check.passed although the command exited 1
    b.stop()
    report = analyze(b)
    assert "check_contradicts_command_finished" in codes(report)
    assert report.final_check_states.total == 0
    assert report.observable_failures.total == 0


def test_duplicate_check_events(b: SessionBuilder) -> None:
    started, _, _ = b.run_check(False)
    b.check(started, "pytest", exit_code=1)
    b.stop()
    report = analyze(b)
    assert "duplicate_check" in codes(report)
    assert report.observable_failures.total == 0


def test_warnings_reference_events(b: SessionBuilder) -> None:
    started = b.start(PYTEST, command_id="6" * 32)
    second = b.start(PYTEST, command_id="6" * 32)
    b.stop()
    [warning] = analyze(b).warnings.items
    assert warning.event_ids == (started.event_id, second.event_id)
    assert warning.severity.value == "warning"


# -- 26-28: determinism, JSON, bounds --------------------------------------------------------


def _scenario(b: SessionBuilder) -> None:
    b.git()
    b.run_check(True)
    b.file("modified", "src/app.py")
    b.run_check(False)
    b.file("modified", "src/app.py")
    b.run_check(False)
    b.file("created", "src/new.py")
    b.run_check(True)
    b.run_check(False, ["python", "-m", "mypy", "src"], name="mypy", kind=CheckKind.TYPECHECK)
    b.start(["python", "serve.py"])
    b.run_command(["nope"], exit_code=None, outcome=Outcome.SPAWN_ERROR)
    b.error()
    b.stop()


def test_identical_bytes_give_identical_reports(b: SessionBuilder, tmp_path: Path) -> None:
    _scenario(b)
    first = analyze(b).to_json()
    assert analyze(b).to_json() == first
    copy_root = tmp_path / "elsewhere"
    shutil.copytree(b.store.project_root / ".agentbodycam", copy_root / ".agentbodycam")
    copied = analyze_session(EventStore(copy_root), b.session_id, probe=probe_inactive)
    assert copied.to_json() == first


def test_json_serialization(b: SessionBuilder) -> None:
    _scenario(b)
    report = analyze(b)
    text = report.to_json()
    data = json.loads(text)
    assert text.isascii() and "\x1b" not in text
    assert data["schema_version"] == 1
    assert data["finding_evidence"] == "observed"
    for key in (
        "session_id",
        "session_status",
        "started_at",
        "ended_at",
        "event_count",
        "timeline",
        "first_observable_failure",
        "recoveries",
        "unresolved_failures",
        "incomplete_commands",
        "final_check_states",
        "file_change_summary",
        "latest_git_state",
        "errors_observed",
        "warnings",
        "analysis_complete",
    ):
        assert key in data
    assert data["recoveries"]["total"] == 1 and data["recoveries"]["truncated"] is False
    assert data["first_observable_failure"]["evidence_event_ids"]
    assert data["recoveries"]["items"][0]["evidence_event_ids"]
    assert data["event_counts"]["check.failed"] == 3
    assert json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) == text


def test_every_finding_points_at_evidence(b: SessionBuilder) -> None:
    _scenario(b)
    report = analyze(b)
    ids = {e.event_id for e in b.store.read_events(b.session_id)}
    findings: list[tuple[str, ...]] = [
        f.evidence_event_ids for f in report.observable_failures.items
    ]
    findings += [r.evidence_event_ids for r in report.recoveries.items]
    findings += [u.evidence_event_ids for u in report.unresolved_failures.items]
    findings += [i.evidence_event_ids for i in report.incomplete_commands.items]
    assert findings and all(f and set(f) <= ids for f in findings)
    assert all(s.latest_event_id in ids for s in report.final_check_states.items)
    kinds = {r.finding_evidence for r in report.recoveries.items}
    kinds |= {u.finding_evidence for u in report.unresolved_failures.items}
    kinds |= {f.finding_evidence for f in report.observable_failures.items}
    assert kinds == {"observed"}


def test_report_bounds(b: SessionBuilder) -> None:
    for _ in range(40):
        b.run_check(False)
    for i in range(MAX_FINDINGS + 5):
        b.file("created", f"gen/f{i:04}.txt")
    b.run_check(True)
    b.stop()
    report = analyze(b, max_timeline_items=10)
    assert len(report.timeline.items) == 10 and report.timeline.truncated
    assert report.timeline.total == report.event_count
    [recovery] = report.recoveries.items
    assert recovery.failed_attempts == 40
    assert len(recovery.evidence_event_ids) == MAX_EVIDENCE_REFS
    assert recovery.evidence_event_ids_truncated is True
    assert recovery.recovered_event_id in recovery.evidence_event_ids
    assert recovery.first_failed_event_id in recovery.evidence_event_ids
    assert report.observable_failures.total == 40
    files = report.file_change_summary
    assert files.unique_paths == MAX_FINDINGS + 5
    assert files.paths.truncated and len(files.paths.items) == forensics.MAX_PATHS
    text = render_report(report, max_timeline=5)
    assert "Timeline (5 of" in text and "more (see --json)" in text


def test_file_change_summary(b: SessionBuilder) -> None:
    b.file("created", "new.py")
    b.file("modified", "new.py")
    b.file("modified", "old.py")
    b.file("deleted", "gone.py")
    b.stop()
    files = analyze(b).file_change_summary
    assert (files.created_events, files.modified_events, files.deleted_events) == (1, 2, 1)
    assert files.total_events == 4 and files.unique_paths == 3
    by_path = {p.path: p for p in files.paths.items}
    assert by_path["new.py"].change_events == 2
    assert (by_path["new.py"].first_change, by_path["new.py"].last_change) == (
        "created",
        "modified",
    )
    assert by_path["gone.py"].present_at_last_observation is False
    assert "Final evidence (observed during this recording)" in render_report(analyze(b))
    assert "observed during this recording" in files.description


# -- 29-32: privacy and wording --------------------------------------------------------------


def _tree(root: Path) -> tuple[bytes, list[str]]:
    return store_bytes(root), sorted(p.as_posix() for p in root.rglob("*"))


def test_analysis_writes_nothing(b: SessionBuilder) -> None:
    _scenario(b)
    before = _tree(b.store.project_root)
    analyze(b)
    analyze_session(b.store, b.session_id)
    assert _tree(b.store.project_root) == before


def test_liveness_probe_writes_nothing(b: SessionBuilder) -> None:
    b.run_check(False)  # status stays "recording", so the real probe runs
    before = _tree(b.store.project_root)
    report = analyze_session(b.store, b.session_id)
    assert report.recording_state is RecordingState.ENDED_WITHOUT_STOP
    assert _tree(b.store.project_root) == before


def test_no_secret_is_reintroduced(b: SessionBuilder) -> None:
    secret = "ghp_" + "Zx9" * 12
    b.run_check(False, ["python", "-m", "pytest", "--token", secret, f"--api-key={secret}"])
    b.stop()
    report = analyze(b)
    for text in (report.to_json(), render_report(report)):
        assert secret not in text
        assert "Zx9Zx9" not in text


def test_no_causal_language(b: SessionBuilder) -> None:
    _scenario(b)
    report = analyze(b)
    for text in (report.to_json(), render_report(report)):
        assert CAUSAL_LANGUAGE.search(text) is None, CAUSAL_LANGUAGE.search(text)
    text = render_report(report)
    assert "First observable failure" in text and "first bug" not in text.lower()


def test_engine_imports_no_network_or_process_modules() -> None:
    import agentbodycam.core.forensics as module
    import agentbodycam.models.forensics as models

    for mod in (module, models):
        assert mod.__file__ is not None
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for banned in ("socket", "urllib", "http", "requests", "subprocess", "ssl"):
            assert f"import {banned}" not in source
            assert f"from {banned}" not in source


# -- pure analysis and performance -----------------------------------------------------------


def test_pure_analysis_is_linear_enough() -> None:
    sid = "20261014T000000Z-0000000000000000"
    specs: list[tuple[EventType, str | None, JsonObject]] = [(EventType.SESSION_STARTED, None, {})]
    for i in range(4_000):
        cid = f"{i:032x}"
        passed = i % 3 != 0
        started: JsonObject = {
            "command_id": cid,
            "executable": "python",
            "argv": ["python", "-m", "pytest", f"tests/t{i % 50}"],
            "argument_count": 3,
            "argv_redacted": False,
            "argv_truncated": False,
            "argv_paths_rewritten": False,
            "working_directory": ".",
        }
        specs += [
            (EventType.COMMAND_STARTED, None, started),
            (EventType.FILE_MODIFIED, f"src/m{i % 97}.py", {}),
            (
                EventType.COMMAND_FINISHED,
                None,
                {"command_id": cid, "outcome": "exited", "exit_code": 0 if passed else 1},
            ),
            (
                EventType.CHECK_PASSED if passed else EventType.CHECK_FAILED,
                None,
                {
                    "command_id": cid,
                    "kind": "test",
                    "name": "pytest",
                    "result": "passed" if passed else "failed",
                    "exit_code": 0 if passed else 1,
                },
            ),
        ]
    events = synthetic_events(sid, specs)
    from agentbodycam.models.session import PlatformInfo, Session, SessionStatus

    session = Session(
        session_id=sid,
        project_root="/p",
        started_at=events[0].timestamp,
        agentbodycam_version="0",
        platform=PlatformInfo("x", "x", "x", "3", "CPython"),
        status=SessionStatus.STOPPED,
        ended_at=events[-1].timestamp,
    )
    started_at = time.perf_counter()
    report = analyze_events(session, events)
    elapsed = time.perf_counter() - started_at
    assert report.event_count == len(events) == 16_001
    assert report.command_summary.check_signatures == 50
    assert report.recoveries.total > 0
    assert elapsed < 10, elapsed
