"""Read-only Git detection."""

from __future__ import annotations

import shutil
import subprocess

from agentbodycam.models.environment import ToolStatus

_VERSION_TIMEOUT_SECONDS = 5.0


def detect_git() -> ToolStatus:
    """Report whether a ``git`` executable is on PATH and its version, if obtainable."""
    path = shutil.which("git")
    if path is None:
        return ToolStatus(name="git", available=False)

    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ToolStatus(name="git", available=True, path=path)

    output = completed.stdout.strip()
    version = output if completed.returncode == 0 and output else None
    return ToolStatus(name="git", available=True, path=path, version=version)
