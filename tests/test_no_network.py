"""AgentBodycam must work without any network access and must never reach the network.

The one intentional exception is LOCAL IPC: the Replay API (``core/replay_server.py``)
listens on a loopback address and answers local clients. It makes no outbound connection
and no DNS lookup, which the tests below enforce separately from the ban on networking
everywhere else.
"""

from __future__ import annotations

import ast
import io
import ipaddress
import socket
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

import agentbodycam
from agentbodycam.cli import main
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.replay import ReplayService
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventDraft, EventSource, EventType
from forensics_helpers import SessionBuilder
from git_helpers import commit_all, init_repo, requires_git
from recording_helpers import write
from replay_helpers import raw_request, serve

PACKAGE_DIR = Path(agentbodycam.__file__).parent
NETWORK_MODULES = {
    "socket",
    "socketserver",
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
# Loopback-only server modules, allowed in exactly one file. No client modules (urllib,
# http.client, ssl, ...) are allowed anywhere.
LOCAL_IPC_IMPORTS = {"replay_server.py": {"socket", "socketserver", "http.server"}}
DNS_AND_CLIENT_CALLS = (
    "create_connection",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getfqdn",
    "getnameinfo",
)


def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise AssertionError("network access attempted")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _refuse)
    for name in DNS_AND_CLIENT_CALLS:
        monkeypatch.setattr(socket, name, _refuse)


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
    assert main(["session", "active", "--project", str(project)], out=io.StringIO()) == 0


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


@pytest.mark.usefixtures("no_network")
def test_wrapped_command_works_with_network_disabled(project: Path, tmp_path: Path) -> None:
    """AgentBodycam's own side of `run` needs no network (the child is the user's own)."""
    recorder = Recorder(EventStore(project), record_git=False)
    sid = recorder.start().session_id
    marker = tmp_path / "ran"
    try:
        code = main(
            [
                "run",
                "--project",
                str(project),
                "--",
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('x')",
                str(marker),
            ],
            out=io.StringIO(),
            err=io.StringIO(),
        )
    finally:
        recorder.stop()
    assert code == 0
    assert marker.exists()
    types = [e.type for e in EventStore(project).read_events(sid)]
    assert EventType.COMMAND_STARTED in types and EventType.COMMAND_FINISHED in types


@pytest.mark.usefixtures("no_network")
def test_analysis_and_replay_service_work_with_network_disabled(store: EventStore) -> None:
    b = SessionBuilder(store)
    b.run_check(False)
    b.stop()
    argv = ["analyze", "--json", "--project", str(store.project_root), b.session_id]
    assert main(argv, out=io.StringIO()) == 0
    service = ReplayService(store.project_root)
    service.list_sessions()
    service.report(b.session_id)
    service.timeline(b.session_id)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_package_source_imports_no_network_modules() -> None:
    """Only the loopback Replay API server may touch sockets, and only server modules."""
    offenders: list[str] = []
    for path in PACKAGE_DIR.rglob("*.py"):
        allowed = LOCAL_IPC_IMPORTS.get(path.name, set())
        offenders += [
            f"{path.name}: {name}"
            for name in _imports(path)
            if name.split(".")[0] in NETWORK_MODULES and name not in allowed
        ]
    assert offenders == []
    server_imports = set(_imports(PACKAGE_DIR / "core" / "replay_server.py"))
    assert server_imports & NETWORK_MODULES <= {"socket", "socketserver"}
    assert {n for n in server_imports if n.split(".")[0] == "http"} == {"http.server"}


class _LoopbackOnlySocket(socket.socket):
    """Allows binding/accepting on loopback and connecting to loopback; nothing else."""

    connected: list[str] = []  # noqa: RUF012 - shared record for the test

    @staticmethod
    def _require_loopback(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if not ipaddress.ip_address(host).is_loopback:
            raise AssertionError(f"non-loopback network access attempted: {host}")

    def connect(self, address: Any) -> None:
        self._require_loopback(address)
        _LoopbackOnlySocket.connected.append(str(address[0]))
        super().connect(address)

    def connect_ex(self, address: Any) -> int:
        self._require_loopback(address)
        return super().connect_ex(address)

    def bind(self, address: Any) -> None:
        self._require_loopback(address)
        super().bind(address)

    def sendto(self, *args: Any) -> int:
        raise AssertionError("datagram sent")


def test_replay_api_is_loopback_ipc_only(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server binds loopback, does no DNS lookup and opens no outbound connection."""
    b = SessionBuilder(store)
    b.git()
    b.run_check(True)
    b.stop()
    _LoopbackOnlySocket.connected = []
    monkeypatch.setattr(socket, "socket", _LoopbackOnlySocket)
    for name in DNS_AND_CLIENT_CALLS:
        monkeypatch.setattr(socket, name, _refuse)
    server = serve(store.project_root)
    try:
        assert server.bound_address[0] == "127.0.0.1"
        for path in (
            "/api/v1/health",
            "/api/v1/sessions",
            f"/api/v1/sessions/{b.session_id}",
            f"/api/v1/sessions/{b.session_id}/report",
            f"/api/v1/sessions/{b.session_id}/timeline",
        ):
            raw = (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
                f"Authorization: Bearer {server.token}\r\nConnection: close\r\n\r\n"
            ).encode()
            assert raw_request(server.port, raw).status == 200
    finally:
        server.stop()
    # Every connection was the test client's own, to the loopback server.
    assert set(_LoopbackOnlySocket.connected) == {"127.0.0.1"}
