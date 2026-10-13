from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentbodycam.core import doctor
from agentbodycam.core.doctor import collect_environment, format_report
from agentbodycam.models.environment import ToolStatus
from agentbodycam.utils.git import detect_git


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
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert detect_git() == ToolStatus(name="git", available=False)


def test_detect_git_handles_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("cannot execute")

    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/git")
    monkeypatch.setattr(subprocess, "run", boom)
    status = detect_git()
    assert status.available is True
    assert status.path == "/fake/git"
    assert status.version is None


def test_detect_git_parses_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args == ["/fake/git", "--version"]
        return subprocess.CompletedProcess(args, 0, stdout="git version 2.99.0\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/git")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_git().version == "git version 2.99.0"
