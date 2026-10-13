"""Models describing the local environment."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """Availability of an external command-line tool."""

    name: str
    available: bool
    path: str | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    """Snapshot of basic environment facts reported by ``agentbodycam doctor``."""

    agentbodycam_version: str
    python_version: str
    python_supported: bool
    python_executable: str
    os_name: str
    os_release: str
    machine: str
    cwd: str
    git: ToolStatus
