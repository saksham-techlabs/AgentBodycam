"""Real regression tests for Codex audit findings ABD-001 .. ABD-004 (Task 14A).

These exercise the real discovery, recording, classification and observation paths —
not mocks of the failing behaviour.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
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
from agentbodycam.core.redact import PROJECT_PLACEHOLDER, redact_command
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType
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


# -- ABD-002: false recovery from absolute/relative path collision ---------------------------


def test_abd002_absolute_and_relative_project_scripts_are_not_false_recovery(
    project: Path,
) -> None:
    """Codex reproduction: both runs from ``sub/``, different check.py files.

    ``python <abs>/check.py`` fails (root script); ``python check.py`` passes (sub script).
    After redaction both must not collapse into one recoverable identity.
    """
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
    assert s1.payload["working_directory"] == s2.payload["working_directory"] == "sub"
    assert check_signature("test", "command", s1) != check_signature("test", "command", s2)

    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 0
    assert report.unresolved_failures.total == 1
    [unresolved] = report.unresolved_failures.items
    assert unresolved.first_failed_event_id == failed.event_id
    # The failed absolute check remains failed; the relative one is a separate passed state.
    results = {s.latest_result for s in report.final_check_states.items}
    assert "failed" in results and "passed" in results
    text = render_report(report)
    assert report.recoveries.total == 0
    assert "unresolved in this recording" in text.lower()
    assert not CAUSAL_LANGUAGE.search(text)
    for banned in ("ROOT CAUSE", "FIXED BY", "CAUSED BY", "CULPRIT"):
        assert banned not in text.upper()


def test_abd002_same_identifiable_relative_check_still_recovers(project: Path) -> None:
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
    script = project / "check.py"
    write(script, "import sys; sys.exit(1)\n")
    store = EventStore(project)
    b = SessionBuilder(store)
    argv = [sys.executable, str(script.resolve())]
    s1, _, _ = b.run_check(False, argv, name="command")
    s2, _, _ = b.run_check(True, argv, name="command")
    b.stop()
    assert _recorded_argv(s1)[1] == f"{PROJECT_PLACEHOLDER}/check.py"
    assert check_signature("test", "command", s1) == check_signature("test", "command", s2)
    report = analyze_session(store, b.session_id)
    assert report.recoveries.total == 1
    assert report.unresolved_failures.total == 0


# -- ABD-003: npm --if-present must not become a passed build --------------------------------


def test_abd003_if_present_is_not_automatic_but_explicit_still_classifies() -> None:
    for argv in (
        ["npm", "run", "build", "--if-present"],
        ["pnpm", "run", "build", "--if-present"],
        ["yarn", "run", "build", "--if-present"],
        ["pnpm", "build", "--if-present"],
        ["yarn", "build", "--if-present"],
        ["cmd", "/c", "npm", "run", "build", "--if-present"],
    ):
        assert classify_command(argv) is None, argv
        explicit = explicit_classification(CheckKind.BUILD, argv)
        assert explicit.source is ClassificationSource.EXPLICIT
        assert "build" in explicit.name


@requires_npm
def test_abd003_real_npm_if_present_without_build_script_is_not_a_check(
    project: Path,
) -> None:
    write(
        project / "package.json",
        json.dumps({"name": "abd003", "private": True, "scripts": {}}),
    )
    # Confirm the real npm command exits 0 when the script is absent.
    probe = subprocess.run(  # noqa: S603
        _npm_argv("run", "build", "--if-present"),
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr

    store = EventStore(project)
    recorder = Recorder(store, record_git=False)
    recorder.start()
    try:
        argv = _npm_argv("run", "build", "--if-present")
        assert classify_command(argv) is None
        code, err = run_cli("run", "--project", str(project), "--", *argv)
        assert code == 0, err
    finally:
        recorder.stop()

    events = store.read_events(recorder.session.session_id)
    types = [e.type for e in events]
    assert EventType.COMMAND_STARTED in types
    assert EventType.COMMAND_FINISHED in types
    assert EventType.CHECK_PASSED not in types
    assert EventType.CHECK_FAILED not in types
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
