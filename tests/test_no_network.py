"""AgentBodycam's store and CLI must work without any network access."""

from __future__ import annotations

import ast
import io
import socket
from pathlib import Path
from typing import Any, NoReturn

import pytest

import agentbodycam
from agentbodycam.cli import main
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventDraft, EventSource, EventType
from git_helpers import commit_all, init_repo, requires_git
from recording_helpers import write

PACKAGE_DIR = Path(agentbodycam.__file__).parent
NETWORK_MODULES = {
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "websocket",
    "websockets",
}


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "gethostbyname", refuse)


@pytest.mark.usefixtures("no_network")
def test_store_works_with_network_disabled(project: Path) -> None:
    store = EventStore(project)
    sid = store.create_session().session_id
    store.append_event(
        sid,
        EventDraft(type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary="x"),
    )
    assert store.count_events(sid) == 1
    assert [s.session_id for s in store.list_sessions()] == [sid]


@pytest.mark.usefixtures("no_network")
def test_cli_session_commands_work_with_network_disabled(project: Path) -> None:
    out = io.StringIO()
    assert main(["session", "create", "--project", str(project)], out=out) == 0
    sid = out.getvalue().split(": ", 1)[1].splitlines()[0]
    assert main(["session", "list", "--project", str(project)], out=io.StringIO()) == 0
    assert main(["session", "show", sid, "--project", str(project)], out=io.StringIO()) == 0


@pytest.mark.usefixtures("no_network")
def test_recorder_works_with_network_disabled(project: Path) -> None:
    out = io.StringIO()
    assert main(["record", "--project", str(project), "--duration", "0.2"], out=out) == 0
    assert "Recording stopped." in out.getvalue()
    store = EventStore(project)
    [sid] = store.list_session_ids()
    assert [e.type for e in store.read_events(sid)] == [
        EventType.SESSION_STARTED,
        EventType.SESSION_STOPPED,
    ]


@requires_git
@pytest.mark.usefixtures("no_network")
def test_git_recording_works_with_network_disabled(project: Path) -> None:
    init_repo(project)
    write(project / "a.txt", "a")
    commit_all(project, "First")
    out = io.StringIO()
    assert main(["record", "--project", str(project), "--duration", "0"], out=out) == 0
    store = EventStore(project)
    [sid] = store.list_session_ids()
    assert EventType.GIT_SNAPSHOT in [e.type for e in store.read_events(sid)]


def test_package_source_imports_no_network_modules() -> None:
    offenders: list[str] = []
    for path in PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{path.name}: {name}" for name in names if name.split(".")[0] in NETWORK_MODULES
            ]
    assert offenders == []
