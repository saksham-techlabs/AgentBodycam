"""Real regression tests for Codex audit findings ABD-001 .. ABD-004 (Task 14A).

These exercise the real discovery, recording, classification and observation paths —
not mocks of the failing behaviour.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from agentbodycam.cli import render_report
from agentbodycam.core.checks import (
    CheckKind,
    ClassificationSource,
    classify_command,
    explicit_classification,
)
from agentbodycam.core.doctor import collect_environment
from agentbodycam.core.forensics import analyze_session, check_signature
from agentbodycam.core.git_observer import GitObserver
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.redact import (
    HOME_PLACEHOLDER,
    PROJECT_PLACEHOLDER,
    ArgvItemOrigin,
    redact_command,
)
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType, JsonObject
from agentbodycam.utils.git import detect_git, find_git_executable, git_environment
from command_helpers import run_cli
from forensics_helpers import CAUSAL_LANGUAGE, SessionBuilder
from git_helpers import GIT, commit_all, git, head, init_repo, requires_git
from recording_helpers import write

requires_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm not available")


def _npm_argv(*args: str) -> list[str]:
    """Argv that can actually spawn npm on this platform (Windows needs ``cmd /c``)."""
    if sys.platform == "win32":
        return ["cmd", "/c", "npm", *args]
    else:
        return ["npm", *args]


def _recorded_argv(event: object) -> list[str]:
    payload = getattr(event, "payload", {})
    argv = payload.get("argv") if isinstance(payload, dict) else None
    assert isinstance(argv, list) and all(isinstance(item, str) for item in argv)
    return list(argv)


def _recorded_origins(event: object) -> list[str]:
    payload = getattr(event, "payload", {})
    origins = payload.get("argv_item_origins") if isinstance(payload, dict) else None
    assert isinstance(origins, list) and all(isinstance(item, str) for item in origins)
    return list(origins)


def _rewrite_command_started(
    store: EventStore, session_id: str, mutator: Callable[[JsonObject], None]
) -> None:
    """Persist in-place mutations to every ``command.started`` payload (for legacy tests)."""
    path = store.events_path(session_id)
    lines: list[str] = []
    for event in store.read_events(session_id):
        if event.type is EventType.COMMAND_STARTED:
            payload = dict(event.payload)
            mutator(payload)
            event = dataclasses.replace(event, payload=payload)
        lines.append(event.to_json_line())
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    store._tails.pop(session_id, None)


# -- ABD-001: safe Git discovery for doctor --------------------------------------------------


def test_abd001_planted_git_shims_never_run_via_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "planted-executed"
    (tmp_path / "git.cmd").write_text(
        f'@echo off\r\necho ran> "{marker}"\r\necho git version planted\r\n',
        encoding="utf-8",
    )
    (tmp_path / "git.bat").write_text(
        f'@echo off\r\necho ran> "{marker}"\r\necho git version planted\r\n',
        encoding="utf-8",
    )
    planted = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    planted.write_text(
        (
            f'@echo off\r\necho ran> "{marker}"\r\necho git version planted\r\n'
            if sys.platform == "win32"
            else f"#!/bin/sh\necho ran > '{marker}'\necho git version planted\n"
        ),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        planted.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    # Relative PATH only: cwd / `.` must never be used for trusted Git discovery.
    monkeypatch.setenv("PATH", ".")

    assert find_git_executable() is None
    assert detect_git().available is False
    assert collect_environment(cwd=tmp_path).git.available is False
    assert not marker.exists()


def test_abd001_legitimate_git_still_selected_when_shims_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = find_git_executable()
    if real is None:
        pytest.skip("no legitimate git on PATH")

    marker = tmp_path / "planted-executed"
    (tmp_path / "git.cmd").write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    (tmp_path / "git.bat").write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    planted = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    planted.write_text(
        (
            f'@echo off\r\necho ran> "{marker}"\r\n'
            if sys.platform == "win32"
            else f"#!/bin/sh\necho ran > '{marker}'\nexit 0\n"
        ),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        planted.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(Path(real).parent)]))

    status = detect_git()
    assert not marker.exists()
    assert status.available is True
    assert status.path == real


# -- ABD-002: false recovery from reserved redaction-marker collision ------------------------


def test_abd002_absolute_and_relative_project_scripts_are_not_false_recovery(
    project: Path,
) -> None:
    """CASE 1 — original fixed case: abs project/check.py vs relative check.py from sub/."""
    write(project / "check.py", "import sys; sys.exit(1)\n")
    write(project / "sub" / "check.py", "import sys; sys.exit(0)\n")
    store = EventStore(project)
    b = SessionBuilder(store)

    abs_fail = [sys.executable, str((project / "check.py").resolve())]
    rel_pass = [sys.executable, "check.py"]

    s1, _, failed = b.run_check(False, abs_fail, name="command", cwd="sub")
    s2, _, _passed = b.run_check(True, rel_pass, name="command", cwd="sub")
    b.stop()

    assert _recorded_argv(s1)[1] == f"{PROJECT_PLACEHOLDER}/check.py"
    assert _recorded_argv(s2)[1] == "check.py"
    assert _recorded_origins(s1)[1] == ArgvItemOrigin.PROJECT_PATH.value
    assert _recorded_origins(s2)[1] == ArgvItemOrigin.LITERAL.value
    assert s1.payload["working_directory"] == s2.payload["working_directory"] == "sub"
    assert check_signature("test", "command", s1) != check_signature("test", "command", s2)

    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.unresolved_failures.total == 1
    [unresolved] = report.unresolved_failures.items
    assert unresolved.first_failed_event_id == failed.event_id
    results = {s.latest_result for s in report.final_check_states.items}
    assert "failed" in results and "passed" in results
    text = render_report(report)
    assert "unresolved in this recording" in text.lower()
    assert not CAUSAL_LANGUAGE.search(text)
    for banned in ("ROOT CAUSE", "FIXED BY", "CAUSED BY", "CULPRIT"):
        assert banned not in text.upper()


def test_abd002_literal_reserved_marker_collision_is_not_false_recovery(
    project: Path,
) -> None:
    """CASE 2 — Codex reproduction: literal ``<project>/check.py`` vs absolute project path.

    Working directory ``project/sub/``. Two different files conceptually:
    ``project/check.py`` and ``project/sub/<project>/check.py``.

    SessionBuilder records evidence without executing; on Windows a real directory named
    ``<project>`` cannot be created, but the recorded argv collision is identical.
    """
    write(project / "check.py", "import sys; sys.exit(1)\n")
    write(project / "sub" / "check.py", "import sys; sys.exit(0)\n")
    store = EventStore(project)
    b = SessionBuilder(store)

    abs_fail = [sys.executable, str((project / "check.py").resolve())]
    literal_pass = [sys.executable, f"{PROJECT_PLACEHOLDER}/check.py"]

    s1, _, failed = b.run_check(False, abs_fail, name="command", cwd="sub")
    s2, _, _passed = b.run_check(True, literal_pass, name="command", cwd="sub")
    b.stop()

    assert _recorded_argv(s1)[1] == f"{PROJECT_PLACEHOLDER}/check.py"
    assert _recorded_argv(s2)[1] == f"{PROJECT_PLACEHOLDER}/check.py"
    assert _recorded_origins(s1)[1] == ArgvItemOrigin.PROJECT_PATH.value
    assert _recorded_origins(s2)[1] == ArgvItemOrigin.LITERAL.value
    assert check_signature("test", "command", s1) != check_signature("test", "command", s2)

    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.unresolved_failures.total == 1
    [unresolved] = report.unresolved_failures.items
    assert unresolved.first_failed_event_id == failed.event_id
    text = render_report(report)
    assert "observed recovery" not in text.lower()
    assert "unresolved in this recording" in text.lower()
    assert not CAUSAL_LANGUAGE.search(text)


def test_abd002_reverse_literal_reserved_marker_collision_is_not_false_recovery(
    project: Path,
) -> None:
    """CASE 3 — reverse: literal ``<project>/check.py`` fails, absolute project path passes."""
    write(project / "check.py", "import sys; sys.exit(0)\n")
    store = EventStore(project)
    b = SessionBuilder(store)

    literal_fail = [sys.executable, f"{PROJECT_PLACEHOLDER}/check.py"]
    abs_pass = [sys.executable, str((project / "check.py").resolve())]

    s1, _, failed = b.run_check(False, literal_fail, name="command", cwd="sub")
    s2, _, _ = b.run_check(True, abs_pass, name="command", cwd="sub")
    b.stop()

    assert _recorded_argv(s1) == _recorded_argv(s2)
    assert _recorded_origins(s1)[1] == ArgvItemOrigin.LITERAL.value
    assert _recorded_origins(s2)[1] == ArgvItemOrigin.PROJECT_PATH.value
    assert check_signature("test", "command", s1) != check_signature("test", "command", s2)

    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.unresolved_failures.total == 1
    assert report.unresolved_failures.items[0].first_failed_event_id == failed.event_id


def test_abd002_same_identifiable_relative_check_still_recovers(project: Path) -> None:
    """CASE 4 — genuine relative recovery."""
    write(project / "check.py", "import sys; sys.exit(1)\n")
    store = EventStore(project)
    b = SessionBuilder(store)
    argv = [sys.executable, "check.py"]
    b.run_check(False, argv, name="command")
    b.run_check(True, argv, name="command")
    b.stop()
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 1
    assert report.unresolved_failures.total == 0
    assert report.final_check_states.items[0].latest_result == "passed"
    text = render_report(report)
    assert "later passed" in text


def test_abd002_same_absolute_project_path_still_recovers(project: Path) -> None:
    """CASE 5 — genuine project-absolute recovery when identity remains safely provable."""
    script = project / "check.py"
    write(script, "import sys; sys.exit(1)\n")
    store = EventStore(project)
    b = SessionBuilder(store)
    argv = [sys.executable, str(script.resolve())]
    s1, _, _ = b.run_check(False, argv, name="command")
    s2, _, _ = b.run_check(True, argv, name="command")
    b.stop()
    assert _recorded_argv(s1)[1] == f"{PROJECT_PLACEHOLDER}/check.py"
    assert _recorded_origins(s1)[1] == ArgvItemOrigin.PROJECT_PATH.value
    assert check_signature("test", "command", s1) == check_signature("test", "command", s2)
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 1
    assert report.unresolved_failures.total == 0


def test_abd002_privacy_redaction_keeps_no_absolute_paths(project: Path, tmp_path: Path) -> None:
    """Stored command evidence must not contain project / home / external absolute paths."""
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "outside" / "secret.bin"
    write(external, "x")
    script = project / "app.py"
    write(script, "pass\n")

    abs_script = str(script.resolve())
    abs_home_file = str((home / "notes.txt").resolve())
    abs_external = str(external.resolve())
    argv = [sys.executable, abs_script, abs_home_file, abs_external, f"--cfg={abs_script}"]

    result = redact_command(argv, project_root=project.resolve(), home=home.resolve())
    joined = " ".join(result.argv)
    assert str(project.resolve()) not in joined
    assert str(home.resolve()) not in joined
    assert abs_external not in joined
    assert abs_script not in joined
    assert result.item_origins[1] is ArgvItemOrigin.PROJECT_PATH
    assert PROJECT_PLACEHOLDER in result.argv[1]

    store = EventStore(project)
    b = SessionBuilder(store)
    started = b.start(argv)
    b.finish(started, exit_code=0)
    b.stop()
    recorded = " ".join(_recorded_argv(started))
    assert str(project.resolve()) not in recorded
    assert abs_external not in recorded


def test_abd002_legacy_literal_marker_without_origins_is_ungrouped(project: Path) -> None:
    """Legacy recordings lacking argv_item_origins: literal reserved markers stay ungrouped."""
    store = EventStore(project)
    b = SessionBuilder(store)
    started, _, _ = b.run_check(
        False, [sys.executable, f"{PROJECT_PLACEHOLDER}/check.py"], name="command"
    )
    b.stop()

    def drop_origins(payload: JsonObject) -> None:
        payload.pop("argv_item_origins", None)
        payload["argv_paths_rewritten"] = False

    _rewrite_command_started(store, b.session_id, drop_origins)
    started = next(
        e for e in store.read_events(b.session_id) if e.type is EventType.COMMAND_STARTED
    )
    assert f"{PROJECT_PLACEHOLDER}/check.py" in _recorded_argv(started)
    assert check_signature("test", "command", started) is None


def test_abd002_embedded_project_marker_collision_is_not_false_recovery(project: Path) -> None:
    """CASE 6 — Codex: two different embedded absolute/literal rewrites look identical."""
    root = str(project.resolve())
    store = EventStore(project)
    b = SessionBuilder(store)

    # Different source strings; after redaction both become the same embedded marker text.
    fail_code = f"import runpy; runpy.run_path('{root}/check.py') # <project>/check.py"
    pass_code = f"import runpy; runpy.run_path('<project>/check.py') # {root}/check.py"
    s1, _, failed = b.run_check(False, [sys.executable, "-c", fail_code], name="command")
    s2, _, _ = b.run_check(True, [sys.executable, "-c", pass_code], name="command")
    b.stop()

    assert _recorded_argv(s1) == _recorded_argv(s2)
    assert _recorded_origins(s1)[2] == ArgvItemOrigin.EMBEDDED_PATH.value
    assert _recorded_origins(s2)[2] == ArgvItemOrigin.EMBEDDED_PATH.value
    assert check_signature("test", "command", s1) is None
    assert check_signature("test", "command", s2) is None
    assert root not in "".join(_recorded_argv(s1))

    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    # Ungrouped failures are not unresolved-by-signature; they remain observable failures.
    assert report.unresolved_failures.total == 0
    assert report.command_summary.checks_ungrouped == 2
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == failed.event_id
    text = render_report(report)
    assert "observed recovery" not in text.lower()
    assert str(project.resolve()) not in text


def test_abd002_embedded_home_marker_collision_is_not_false_recovery(
    project: Path, tmp_path: Path
) -> None:
    """CASE 7 — embedded ``<home>`` rewrite collisions must not invent a recovery."""
    home = tmp_path / "home"
    home.mkdir()
    home_root = str(home.resolve())
    fail_red = redact_command(
        [sys.executable, "-c", f"print('{home_root}/a') # <home>/a"],
        project_root=project.resolve(),
        home=home.resolve(),
    )
    pass_red = redact_command(
        [sys.executable, "-c", f"print('<home>/a') # {home_root}/a"],
        project_root=project.resolve(),
        home=home.resolve(),
    )
    assert fail_red.argv == pass_red.argv
    assert fail_red.item_origins[2] is ArgvItemOrigin.EMBEDDED_PATH
    assert HOME_PLACEHOLDER in fail_red.argv[2]
    assert home_root not in "".join(fail_red.argv)

    store = EventStore(project)
    b = SessionBuilder(store)
    _, _, failed = b.run_check(False, ["python", "-c", "pass"], name="command")
    b.run_check(True, ["python", "-c", "pass"], name="command")
    b.stop()

    planted = iter((fail_red, pass_red))

    def plant(payload: JsonObject) -> None:
        red = next(planted)
        payload["argv"] = list(red.argv)
        payload["argument_count"] = red.argument_count
        payload["argv_paths_rewritten"] = red.paths_rewritten
        payload["argv_item_origins"] = [o.value for o in red.item_origins]
        payload["executable"] = red.executable
        payload["argv_redacted"] = red.redacted
        payload["argv_truncated"] = red.truncated

    _rewrite_command_started(store, b.session_id, plant)
    events = [e for e in store.read_events(b.session_id) if e.type is EventType.COMMAND_STARTED]
    assert check_signature("test", "command", events[0]) is None
    assert check_signature("test", "command", events[1]) is None
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.command_summary.checks_ungrouped == 2
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == failed.event_id


def test_abd002_legacy_marker_with_paths_rewritten_still_ungrouped(project: Path) -> None:
    """CASE 8 — argv_paths_rewritten from argv[0] must not trust reserved markers elsewhere."""
    store = EventStore(project)
    b = SessionBuilder(store)
    abs_python = str(Path(sys.executable).resolve())
    _, _, failed = b.run_check(
        False, [abs_python, f"{PROJECT_PLACEHOLDER}/check.py"], name="command"
    )
    b.run_check(True, [abs_python, f"{PROJECT_PLACEHOLDER}/check.py"], name="command")
    b.stop()

    def legacy_trust_trap(payload: JsonObject) -> None:
        payload.pop("argv_item_origins", None)
        # Absolute interpreter rewrite sets the command-wide flag; that must not authorize
        # grouping when a reserved marker also appears in a later argv item.
        payload["argv_paths_rewritten"] = True

    _rewrite_command_started(store, b.session_id, legacy_trust_trap)
    events = [e for e in store.read_events(b.session_id) if e.type is EventType.COMMAND_STARTED]
    assert check_signature("test", "command", events[0]) is None
    assert check_signature("test", "command", events[1]) is None
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.command_summary.checks_ungrouped == 2
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == failed.event_id


@pytest.mark.parametrize(
    "origins",
    [
        None,  # explicit null
        "literal",  # wrong type
        ["literal"],  # wrong length
        ["literal", "not-a-real-origin"],  # unknown value
    ],
    ids=["null", "wrong-type", "wrong-length", "unknown"],
)
def test_abd002_malformed_origins_are_ungroupable(project: Path, origins: object) -> None:
    """CASE 9 — malformed argv_item_origins never fall back to permissive identity."""
    store = EventStore(project)
    b = SessionBuilder(store)
    argv = [sys.executable, "check.py"]
    _, _, failed = b.run_check(False, argv, name="command")
    b.run_check(True, argv, name="command")
    b.stop()

    def break_origins(payload: JsonObject) -> None:
        payload["argv_item_origins"] = origins  # type: ignore[assignment]

    _rewrite_command_started(store, b.session_id, break_origins)
    events = [e for e in store.read_events(b.session_id) if e.type is EventType.COMMAND_STARTED]
    assert check_signature("test", "command", events[0]) is None
    assert check_signature("test", "command", events[1]) is None
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.command_summary.checks_ungrouped == 2
    assert report.first_observable_failure is not None
    assert report.first_observable_failure.event_id == failed.event_id


def test_abd002_ordinary_legacy_session_without_markers_still_groups(project: Path) -> None:
    """CASE 10 — legacy recordings with unambiguous relative argv still recover."""
    store = EventStore(project)
    b = SessionBuilder(store)
    argv = [sys.executable, "-c", "pass"]
    b.run_check(False, argv, name="command")
    b.run_check(True, argv, name="command")
    b.stop()

    def drop_origins(payload: JsonObject) -> None:
        payload.pop("argv_item_origins", None)
        payload["argv_paths_rewritten"] = False

    _rewrite_command_started(store, b.session_id, drop_origins)
    events = [e for e in store.read_events(b.session_id) if e.type is EventType.COMMAND_STARTED]
    assert check_signature("test", "command", events[0]) is not None
    assert check_signature("test", "command", events[0]) == check_signature(
        "test", "command", events[1]
    )
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 1
    assert report.unresolved_failures.total == 0


# -- ABD-003: npm optional / manager-token forms must not become a passed build --------------


# Codex-reproduced npm 11.6.2 missing-script no-op / ambiguous manager-token forms.
_NPM_MANAGER_TOKEN_FORMS = (
    "--if-present",
    "--if-present=true",
    "--if-present=1",
    "--if-pres",
    "--if",
    "--if-",
    "--if-p",
    "-if-present",
    "-if",
    "---if-present",
    "--no-no-if-present",
)


def test_abd003_if_present_is_not_automatic_but_explicit_still_classifies() -> None:
    for flag in _NPM_MANAGER_TOKEN_FORMS:
        argv = ["npm", "run", "build", flag]
        assert classify_command(argv) is None, argv
        explicit = explicit_classification(CheckKind.BUILD, argv)
        assert explicit.source is ClassificationSource.EXPLICIT
        assert explicit.kind is CheckKind.BUILD
    for argv in (
        ["pnpm", "run", "--if-present", "build"],
        ["pnpm", "run", "--if-present=true", "build"],
        ["cmd", "/c", "npm", "run", "build", "--if-present"],
        ["cmd", "/c", "npm", "run", "build", "--if-present=true"],
        ["npm", "run", "build", "--some-future-npm-option"],
    ):
        assert classify_command(argv) is None, argv


def test_abd003_npm_double_dash_boundary_does_not_treat_script_arg_as_optional() -> None:
    for argv in (
        ["npm", "run", "build", "--", "--if-present"],
        ["npm", "run", "build", "--", "--watch"],
        ["npm", "test", "--", "--watch"],
    ):
        result = classify_command(argv)
        assert result is not None, argv
        assert result.source is ClassificationSource.AUTOMATIC


def test_abd003_pnpm_after_script_if_present_is_not_manager_optional() -> None:
    """pnpm only treats ``--if-present`` before the script name as optional."""
    assert classify_command(["pnpm", "run", "build", "--if-present"]) is not None
    assert classify_command(["pnpm", "build", "--if-present"]) is not None


def test_abd003_yarn_after_script_if_present_is_not_manager_optional() -> None:
    """Yarn classic has no after-script optional no-op equivalent."""
    assert classify_command(["yarn", "run", "build", "--if-present"]) is not None
    assert classify_command(["yarn", "build", "--if-present"]) is not None


@requires_npm
@pytest.mark.parametrize("flag", _NPM_MANAGER_TOKEN_FORMS)
def test_abd003_real_npm_manager_token_forms_without_build_script_are_not_checks(
    project: Path,
    flag: str,
) -> None:
    write(
        project / "package.json",
        json.dumps({"name": "abd003", "private": True, "scripts": {}}),
    )
    probe = subprocess.run(  # noqa: S603
        _npm_argv("run", "build", flag),
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    # Some forms no-op successfully; rejected forms must still never become check.passed.
    argv = _npm_argv("run", "build", flag)
    assert classify_command(argv) is None

    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        code, err = run_cli("run", "--project", str(project), "--", *argv)
        # Preserve npm's exit status: success no-op or rejection — never invent a check.
        assert code == probe.returncode, err
    finally:
        recorder.stop()

    events = store.read_events(recorder.session.session_id)
    types = [e.type for e in events]
    assert EventType.COMMAND_STARTED in types
    assert EventType.COMMAND_FINISHED in types
    assert EventType.CHECK_PASSED not in types
    assert EventType.CHECK_FAILED not in types
    if probe.returncode == 0:
        finished = next(e for e in events if e.type is EventType.COMMAND_FINISHED)
        assert finished.payload["exit_code"] == 0


@requires_npm
def test_abd003_real_npm_build_script_still_becomes_a_check(project: Path) -> None:
    write(
        project / "package.json",
        json.dumps(
            {
                "name": "abd003-build",
                "private": True,
                "scripts": {"build": 'node -e "process.exit(0)"'},
            }
        ),
    )
    argv = _npm_argv("run", "build")
    assert classify_command(argv) is not None

    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        code, err = run_cli("run", "--project", str(project), "--", *argv)
        assert code == 0, err
    finally:
        recorder.stop()

    checks = [
        e
        for e in store.read_events(recorder.session.session_id)
        if e.type is EventType.CHECK_PASSED
    ]
    assert len(checks) == 1
    assert checks[0].payload["name"] == "npm build"
    assert checks[0].payload["classification"] == "automatic"


@requires_npm
def test_abd003_real_npm_test_shorthand_still_becomes_a_check(project: Path) -> None:
    write(
        project / "package.json",
        json.dumps(
            {
                "name": "abd003-test",
                "private": True,
                "scripts": {"test": 'node -e "process.exit(0)"'},
            }
        ),
    )
    argv = _npm_argv("test")
    assert classify_command(argv) is not None

    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        code, err = run_cli("run", "--project", str(project), "--", *argv)
        assert code == 0, err
    finally:
        recorder.stop()

    checks = [
        e
        for e in store.read_events(recorder.session.session_id)
        if e.type is EventType.CHECK_PASSED
    ]
    assert len(checks) == 1
    assert checks[0].payload["name"] == "npm test"
    assert checks[0].payload["classification"] == "automatic"


@requires_npm
def test_abd003_real_npm_double_dash_boundary_with_build_script_still_checks(
    project: Path,
) -> None:
    """``npm run build -- --if-present`` forwards the token to the script, not to npm."""
    write(project / "build.js", "process.exit(0);\n")
    write(
        project / "package.json",
        json.dumps(
            {
                "name": "abd003-boundary",
                "private": True,
                # Script file (not `node -e`) so forwarded `--if-present` is argv, not a
                # node binary option.
                "scripts": {"build": "node build.js"},
            }
        ),
    )
    argv = _npm_argv("run", "build", "--", "--if-present")
    assert classify_command(argv) is not None

    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        code, err = run_cli("run", "--project", str(project), "--", *argv)
        assert code == 0, err
    finally:
        recorder.stop()

    checks = [
        e
        for e in store.read_events(recorder.session.session_id)
        if e.type is EventType.CHECK_PASSED
    ]
    assert len(checks) == 1
    assert checks[0].payload["classification"] == "automatic"


@requires_npm
def test_abd003_explicit_check_override_still_records_check_for_if_present(
    project: Path,
) -> None:
    write(
        project / "package.json",
        json.dumps({"name": "abd003-explicit", "private": True, "scripts": {}}),
    )
    argv = _npm_argv("run", "build", "--if-present=true")
    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        code, err = run_cli("run", "--project", str(project), "--check", "build", "--", *argv)
        assert code == 0, err
    finally:
        recorder.stop()

    checks = [
        e
        for e in store.read_events(recorder.session.session_id)
        if e.type is EventType.CHECK_PASSED
    ]
    assert len(checks) == 1
    assert checks[0].payload["classification"] == "explicit"
    assert checks[0].payload["kind"] == "build"


# -- ABD-004: Git observer must not lazy-fetch ----------------------------------------------


class _RequestCounter(http.server.BaseHTTPRequestHandler):
    counts: dict[str, int]

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).counts["GET"] = type(self).counts.get("GET", 0) + 1
        type(self).counts["total"] = type(self).counts.get("total", 0) + 1
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        type(self).counts["POST"] = type(self).counts.get("POST", 0) + 1
        type(self).counts["total"] = type(self).counts.get("total", 0) + 1
        self.send_response(404)
        self.end_headers()


def _git_supports_partial_clone() -> bool:
    """True when this Git can build a ``--filter=blob:none`` clone (probe, not version parse)."""
    if GIT is None:
        return False
    # ``git clone -h`` is incomplete on some builds; probe ``git rev-list`` filter support
    # and a dry capability check via ``git --version`` major.minor when needed.
    help_probe = subprocess.run(  # noqa: S603
        [GIT, "clone", "-h"],
        capture_output=True,
        text=True,
        check=False,
    )
    help_text = (help_probe.stdout or "") + (help_probe.stderr or "")
    if "--filter" in help_text:
        return True
    version = subprocess.run(  # noqa: S603
        [GIT, "--version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    # Partial clone landed in Git 2.22+; prefer attempting the real clone in the test.
    match = re.search(r"git version (\d+)\.(\d+)", version or "")
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (2, 22)


@pytest.mark.skipif(not _git_supports_partial_clone(), reason="git clone --filter unsupported")
@requires_git
def test_abd004_observe_never_hits_remote_on_partial_clone(tmp_path: Path) -> None:
    assert GIT is not None
    source = tmp_path / "source"
    init_repo(source)
    write(source / "big.bin", "x" * 8192 + "promised-object\n")
    write(source / "readme.txt", "tracked\n")
    commit_all(source, "with blob")

    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "--bare", str(source), str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")

    partial = tmp_path / "partial"
    # ``file://`` + ``--no-local`` + allowFilter: otherwise local clones often materialise
    # every object and leave nothing missing to lazy-fetch.
    bare_url = bare.resolve().as_uri()
    clone = subprocess.run(  # noqa: S603
        [
            GIT,
            "clone",
            "--no-local",
            "--no-checkout",
            "--filter=blob:none",
            bare_url,
            str(partial),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone.returncode != 0:
        pytest.skip(f"partial clone failed: {clone.stderr}")

    # Leave the worktree unpopulated so promised blobs stay missing.
    missing_before = git(partial, "rev-list", "--objects", "--missing=print", "HEAD", check=False)
    # Git prints missing promised objects as ``?<oid>``.
    if not any(line.startswith("?") for line in missing_before.splitlines()):
        pytest.skip(
            "partial clone did not leave missing promised objects "
            f"(git may have materialised them): {missing_before[:200]!r}"
        )

    counts: dict[str, int] = {}
    _RequestCounter.counts = counts
    server = socketserver.TCPServer(("127.0.0.1", 0), _RequestCounter)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        remote_url = f"http://127.0.0.1:{port}/audit.git"
        git(partial, "remote", "set-url", "origin", remote_url)

        before_objects = {
            p.relative_to(partial / ".git")
            for p in (partial / ".git" / "objects").rglob("*")
            if p.is_file()
        }

        counts.clear()
        observer = GitObserver(partial, executable=GIT)
        observation = observer.observe()
        remote_requests = counts.get("total", 0)
        assert remote_requests == 0, f"Git observation contacted remote: {counts}"

        after_objects = {
            p.relative_to(partial / ".git")
            for p in (partial / ".git" / "objects").rglob("*")
            if p.is_file()
        }
        assert after_objects == before_objects, "observation fetched new Git objects"

        missing_after = git(
            partial, "rev-list", "--objects", "--missing=print", "HEAD", check=False
        )
        assert missing_after == missing_before

        # Observation may succeed with local refs or report a problem — never fetch.
        assert (
            observation.state is not None
            or observation.problem is not None
            or observation.unavailable is not None
        )
        assert git_environment()["GIT_NO_LAZY_FETCH"] == "1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@requires_git
def test_abd004_forced_env_disables_lazy_fetch_and_network_protocols() -> None:
    env = git_environment({"GIT_NO_LAZY_FETCH": "0", "PATH": "/bin"})
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    from agentbodycam.utils import git as git_mod

    joined = " ".join(git_mod._SAFE_CONFIG)
    for key in (
        "protocol.https.allow=never",
        "protocol.http.allow=never",
        "protocol.ssh.allow=never",
        "protocol.git.allow=never",
        "protocol.file.allow=never",
    ):
        assert key in joined


@requires_git
def test_abd004_complete_repository_observation_still_works(project: Path) -> None:
    init_repo(project)
    write(project / "a.txt", "a\n")
    commit_all(project, "ok")
    observation = GitObserver(project, executable=GIT).observe()
    assert observation.problem is None
    assert observation.state is not None
    assert observation.state.commit == head(project)


# -- Cross-finding regression ----------------------------------------------------------------


@requires_git
def test_cross_finding_fake_git_sensitive_path_no_network_factual_forensics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake git shim + privacy-sensitive argv + network-isolated observe + factual report."""
    project = tmp_path / "proj"
    project.mkdir()
    init_repo(project)
    write(project / "app.py", "x = 1\n")
    commit_all(project, "init")

    marker = project / "planted-git-ran"
    (project / "git.cmd").write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    (project / "git.bat").write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="utf-8")
    monkeypatch.chdir(project)
    assert GIT is not None
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project), str(Path(GIT).parent)]))

    assert find_git_executable() == GIT
    assert not marker.exists()

    token = "ghp_" + ("Aa1" * 12)
    store = EventStore(project)
    b = SessionBuilder(store)
    secret_argv = [sys.executable, "-c", "pass", "--token", token, str(project / "app.py")]
    redacted = redact_command(secret_argv, project_root=project)
    assert token not in "".join(redacted.argv)
    assert str(project) not in "".join(redacted.argv)
    assert f"{PROJECT_PLACEHOLDER}/app.py" in redacted.argv

    # Same identifiable check argv; only the recorded exit status differs.
    same = [sys.executable, "-c", "pass"]
    b.run_check(False, same, name="command")
    b.run_check(True, same, name="command")

    counts: dict[str, int] = {}
    _RequestCounter.counts = counts
    server = socketserver.TCPServer(("127.0.0.1", 0), _RequestCounter)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        git(project, "remote", "add", "origin", f"http://127.0.0.1:{port}/x.git")
        observation = GitObserver.discover(project).observe()
        assert counts.get("total", 0) == 0
        assert observation.state is not None
        assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    b.stop()
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 1
    dumped = report.to_json()
    assert token not in dumped
    assert str(project) not in dumped
    assert not CAUSAL_LANGUAGE.search(dumped)
    for banned in ("ROOT CAUSE", "FIXED BY", "CAUSED BY", "CULPRIT"):
        assert banned not in dumped.upper()
