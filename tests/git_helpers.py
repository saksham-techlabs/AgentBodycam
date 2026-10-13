"""Real-Git helpers for tests. Only test setup mutates repositories, never AgentBodycam."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentbodycam.utils.git import find_git_executable

GIT = find_git_executable()
requires_git = pytest.mark.skipif(GIT is None, reason="git executable not available")

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}


def git(repo: Path, *args: str, check: bool = True) -> str:
    if GIT is None:
        raise RuntimeError("git not available")
    result = subprocess.run(  # noqa: S603 - fixed executable, test-controlled arguments
        [GIT, "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **_IDENTITY},
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def git_dir_state(repo: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under ``.git`` with size, mtime and contents, to prove nothing changed."""
    root = repo / ".git"
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
