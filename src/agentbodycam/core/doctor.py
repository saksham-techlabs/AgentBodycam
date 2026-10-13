"""Read-only environment inspection for ``agentbodycam doctor``."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from agentbodycam import __version__
from agentbodycam.models.environment import EnvironmentReport
from agentbodycam.utils.git import detect_git

MIN_PYTHON = (3, 11)


def collect_environment(cwd: Path | None = None) -> EnvironmentReport:
    """Gather basic facts about the current environment without modifying anything."""
    info = sys.version_info
    return EnvironmentReport(
        agentbodycam_version=__version__,
        python_version=f"{info.major}.{info.minor}.{info.micro}",
        python_supported=(info.major, info.minor) >= MIN_PYTHON,
        python_executable=sys.executable,
        os_name=platform.system() or "unknown",
        os_release=platform.release(),
        machine=platform.machine(),
        cwd=str(cwd if cwd is not None else Path.cwd()),
        git=detect_git(),
    )


def format_report(report: EnvironmentReport) -> str:
    python_note = "" if report.python_supported else "  (unsupported: requires 3.11+)"
    if report.git.available:
        git_line = f"available ({report.git.version or 'version unknown'})"
    else:
        git_line = "not found on PATH"

    lines = [
        "AgentBodycam doctor",
        "",
        f"  AgentBodycam : {report.agentbodycam_version}",
        f"  Python       : {report.python_version}{python_note}",
        f"  Interpreter  : {report.python_executable}",
        f"  OS           : {report.os_name} {report.os_release} ({report.machine})".rstrip(),
        f"  Current dir  : {report.cwd}",
        f"  Git          : {git_line}",
    ]
    return "\n".join(lines)
