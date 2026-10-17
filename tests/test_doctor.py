from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentbodycam.core import doctor
from agentbodycam.core.doctor import collect_environment, format_report
from agentbodycam.models.environment import ToolStatus
from agentbodycam.utils import git as git_utils
from agentbodycam.utils.git import detect_git, find_git_executable


def test_collect_environment_reports_current_interpreter(tmp_path: Path) -> None:
    report = collect_environment(cwd=tmp_path)
    info = sys.version_info
    assert report.python_version == f"{info.major}.{info.minor}.{info.micro}"
    assert report.python_supported is True
    assert report.python_executable == sys.executable
    assert report.os_name == (platform.system() or "unknown")
    assert report.cwd == str(tmp_path)


def test_collect_environment_does_not_modify_directory(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("untouched", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    collect_environment(cwd=tmp_path)
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "untouched"


def test_format_report_when_git_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "detect_git", lambda: ToolStatus(name="git", available=False))
    text = format_report(collect_environment(cwd=tmp_path))
    assert "Git          : not found on PATH" in text


def test_format_report_when_git_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    status = ToolStatus(name="git", available=True, path="/usr/bin/git", version="git version 9")
    monkeypatch.setattr(doctor, "detect_git", lambda: status)
    text = format_report(collect_environment(cwd=tmp_path))
    assert "Git          : available (git version 9)" in text


def test_detect_git_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_utils, "find_git_executable", lambda path_env=None: None)
    assert detect_git() == ToolStatus(name="git", available=False)


def test_detect_git_handles_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("cannot execute")

    monkeypatch.setattr(git_utils, "find_git_executable", lambda path_env=None: "/fake/git")
    monkeypatch.setattr(subprocess, "run", boom)
    status = detect_git()
    assert status.available is True
    assert status.path == "/fake/git"
    assert status.version is None


def test_detect_git_parses_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args == ["/fake/git", "--version"]
        return subprocess.CompletedProcess(args, 0, stdout="git version 2.99.0\n", stderr="")

    monkeypatch.setattr(git_utils, "find_git_executable", lambda path_env=None: "/fake/git")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_git().version == "git version 2.99.0"


def test_doctor_never_executes_planted_git_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ABD-001: cwd / relative PATH git shims must never run via doctor discovery."""
    marker = tmp_path / "planted-git-ran"
    # Harmless shims: if executed they create a marker file (and exit 0).
    for name, body in (
        (
            "git.cmd",
            f'@echo off\r\necho planted> "{marker}"\r\necho git version planted.cmd\r\n',
        ),
        (
            "git.bat",
            f'@echo off\r\necho planted> "{marker}"\r\necho git version planted.bat\r\n',
        ),
    ):
        (tmp_path / name).write_text(body, encoding="utf-8")
    # Also plant a bare ``git`` in cwd. Discovery must not search cwd or relative PATH
    # entries; absolute PATH entries that contain a real ``git`` remain trusted.
    planted = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    planted.write_text(
        (
            f'@echo off\r\necho planted> "{marker}"\r\necho git version planted\r\n'
            if sys.platform == "win32"
            else f"#!/bin/sh\necho planted > '{marker}'\necho git version planted\n"
        ),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        planted.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    # Relative PATH only — the unsafe discovery shape that would favour cwd/shims.
    monkeypatch.setenv("PATH", ".")

    discovered = find_git_executable()
    status = detect_git()
    report = collect_environment(cwd=tmp_path)

    assert not marker.exists(), "planted git shim was executed"
    assert discovered is None
    assert status.available is False
    assert report.git.available is False


def test_doctor_selects_legitimate_git_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted cwd/relative shims must not win over a real absolute git on PATH."""
    real = find_git_executable()
    if real is None:
        pytest.skip("no legitimate git on PATH")

    marker = tmp_path / "planted-git-ran"
    (tmp_path / "git.cmd").write_text(
        f'@echo off\r\necho planted> "{marker}"\r\n', encoding="utf-8"
    )
    (tmp_path / "git.bat").write_text(
        f'@echo off\r\necho planted> "{marker}"\r\n', encoding="utf-8"
    )
    planted = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    planted.write_text(
        (
            f'@echo off\r\necho planted> "{marker}"\r\n'
            if sys.platform == "win32"
            else f"#!/bin/sh\necho planted > '{marker}'\nexit 0\n"
        ),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        planted.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    # Relative entry first (trap), then the real Git directory.
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(Path(real).parent)]))

    status = detect_git()
    assert not marker.exists(), "planted git shim was executed"
    assert status.available is True
    assert status.path == real
    assert status.version is not None and "git version" in status.version
