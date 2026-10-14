"""Helpers for Replay API tests: a real standard-library HTTP client and store snapshots."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentbodycam.core.replay import ReplayService
from agentbodycam.core.replay_server import ReplayServer

DEFAULT = object()


@dataclass(frozen=True)
class Reply:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        assert len(values) <= 1, f"duplicate {name} header"
        return values[0] if values else None

    def header_names(self) -> set[str]:
        return {k.lower() for k, _ in self.headers}

    @property
    def json(self) -> Any:
        """The decoded JSON object (``Any``: tests inspect arbitrary nested values)."""
        data = json.loads(self.body.decode("utf-8"))
        assert isinstance(data, dict)
        return data

    @property
    def error_code(self) -> object:
        error = self.json["error"]
        assert isinstance(error, dict)
        assert set(error) == {"code", "message"}
        return error["code"]

    def text(self) -> str:
        """Everything the client received (status line aside), for leak checks."""
        lines = [f"{k}: {v}" for k, v in self.headers]
        return "\n".join(lines) + "\n" + self.body.decode("utf-8", "replace")


class Target(Protocol):
    @property
    def host(self) -> str: ...
    @property
    def port(self) -> int: ...
    @property
    def token(self) -> str: ...


@dataclass(frozen=True)
class Endpoint:
    """A server running elsewhere (e.g. an `agentbodycam replay-api` process)."""

    host: str
    port: int
    token: str


class ApiClient:
    """Talks to a running Replay API server over real loopback TCP."""

    def __init__(self, server: Target) -> None:
        self.server = server

    def request(
        self,
        method: str,
        path: str,
        *,
        token: object = DEFAULT,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Reply:
        all_headers = dict(headers or {})
        if token is DEFAULT:
            all_headers.setdefault("Authorization", f"Bearer {self.server.token}")
        elif token is not None:
            all_headers.setdefault("Authorization", f"Bearer {token}")
        host = self.server.host
        conn = http.client.HTTPConnection(host, self.server.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=all_headers)
            response = conn.getresponse()
            data = response.read()
            return Reply(response.status, response.getheaders(), data)
        finally:
            conn.close()

    def get(
        self, path: str, *, token: object = DEFAULT, headers: dict[str, str] | None = None
    ) -> Reply:
        return self.request("GET", path, token=token, headers=headers)


def raw_request(port: int, raw: bytes, host: str = "127.0.0.1") -> Reply:
    """Send exact bytes (no client-side normalisation) and parse the HTTP response."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect((host, port))
        sock.sendall(raw)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return Reply(response.status, response.getheaders(), response.read())


def store_snapshot(project: Path) -> dict[str, bytes]:
    """Every file under .agentbodycam/ (relative path -> bytes); detects new files too."""
    root = project / ".agentbodycam"
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def serve(
    project: Path, *, host: str = "127.0.0.1", log: Callable[[str], None] | None = None
) -> ReplayServer:
    server = ReplayServer(ReplayService(project), host=host, log=log)
    server.start()
    return server
