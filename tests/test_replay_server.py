"""Replay API over real loopback HTTP: security, routing, validation, concurrency, shutdown."""

from __future__ import annotations

import io
import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentbodycam.cli import main
from agentbodycam.core.forensics import analyze_session
from agentbodycam.core.replay import ReplayService
from agentbodycam.core.replay_server import (
    BindAddressError,
    ReplayServer,
    resolve_bind_host,
)
from agentbodycam.core.store import EventStore
from command_helpers import wait_until
from forensics_helpers import CAUSAL_LANGUAGE, SessionBuilder
from replay_helpers import ApiClient, Reply, raw_request, serve, store_snapshot

UNKNOWN_SID = "20261014T000000Z-0000000000000000"
MUTATIONS = ["POST", "PUT", "PATCH", "DELETE"]


@dataclass
class Api:
    client: ApiClient
    server: ReplayServer
    sid: str
    logs: list[str]
    store: EventStore

    @property
    def base(self) -> str:
        return f"/api/v1/sessions/{self.sid}"

    def routes(self) -> list[str]:
        return [
            "/api/v1/project",
            "/api/v1/sessions",
            self.base,
            f"{self.base}/report",
            f"{self.base}/timeline",
            f"{self.base}/timeline?after_sequence=2&limit=3",
        ]


@pytest.fixture
def api(store: EventStore) -> Iterator[Api]:
    """A stopped session (PASS, edit, FAIL, edit, PASS) served by a real server."""
    b = SessionBuilder(store)
    b.run_check(True)
    b.file("modified", "app.py")
    b.run_check(False)
    b.file("modified", "app.py")
    b.git(state="dirty", counts=(0, 1, 0, 0))
    b.run_check(True)
    b.stop()
    logs: list[str] = []
    server = serve(store.project_root, log=logs.append)
    try:
        yield Api(ApiClient(server), server, b.session_id, logs, store)
    finally:
        server.stop()


def assert_safe_headers(reply: Reply) -> None:
    assert reply.header("Content-Type") == "application/json; charset=utf-8"
    assert reply.header("Cache-Control") == "no-store"
    assert reply.header("X-Content-Type-Options") == "nosniff"
    assert not any(name.startswith("access-control-") for name in reply.header_names())


# -- binding ---------------------------------------------------------------------------------


def test_default_bind_is_ipv4_loopback_on_an_ephemeral_port(project: Path) -> None:
    server = ReplayServer(ReplayService(project))
    try:
        assert server.host == "127.0.0.1"
        host, port = server.bound_address
        assert host == "127.0.0.1" and port == server.port and port > 0
        assert server.url == f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104
        "::",
        "[::]",
        "192.168.1.10",
        "10.0.0.5",
        "172.16.0.1",
        "127.0.0.2",
        "example.com",
        "localhost.evil.example",
        "",
        "0",
    ],
)
def test_public_and_lan_binding_is_refused(project: Path, host: str) -> None:
    with pytest.raises(BindAddressError, match="loopback"):
        resolve_bind_host(host)
    with pytest.raises(BindAddressError):
        ReplayServer(ReplayService(project), host=host)


def test_loopback_names() -> None:
    assert resolve_bind_host("127.0.0.1") == "127.0.0.1"
    assert resolve_bind_host("localhost") == "127.0.0.1"  # no DNS lookup
    assert resolve_bind_host("LocalHost") == "127.0.0.1"
    assert resolve_bind_host("::1") == "::1"
    assert resolve_bind_host("[::1]") == "::1"


def test_ipv6_loopback_server(store: EventStore) -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support")
    SessionBuilder(store).stop()
    try:
        server = serve(store.project_root, host="::1")
    except OSError:
        pytest.skip("cannot bind ::1 here")
    try:
        assert server.bound_address[0] == "::1"
        assert server.url.startswith("http://[::1]:")
        reply = ApiClient(server).get("/api/v1/project")
        assert reply.status == 200 and reply.json["session_count"] == 1
    finally:
        server.stop()


def test_port_validation(project: Path) -> None:
    for port in (-1, 65536):
        with pytest.raises(ValueError, match="port"):
            ReplayServer(ReplayService(project), port=port)


# -- authentication --------------------------------------------------------------------------


def test_health_is_unauthenticated_and_reveals_nothing(api: Api) -> None:
    reply = api.client.get("/api/v1/health", token=None)
    assert reply.status == 200
    assert reply.json == {"status": "ok", "api_version": 1, "product": "AgentBodycam"}
    assert_safe_headers(reply)


def test_every_data_route_requires_the_token(api: Api) -> None:
    for path in api.routes():
        for token in (None, "wrong", api.server.token[:-1], api.server.token + "x", ""):
            reply = api.client.get(path, token=token)
            assert reply.status == 401, (path, token)
            assert reply.error_code == "unauthorized"
            assert reply.header("WWW-Authenticate") == 'Bearer realm="agentbodycam"'
            assert api.sid not in reply.text()
            assert_safe_headers(reply)
        assert api.client.get(path).status == 200


@pytest.mark.parametrize(
    "value",
    ["Basic {t}", "Token {t}", "Bearer", "Bearer ", "{t}", "Bearer {t} extra", "Bearer\t{t}"],
)
def test_malformed_authorization_is_rejected(api: Api, value: str) -> None:
    header = value.format(t=api.server.token)
    reply = api.client.get("/api/v1/sessions", headers={"Authorization": header})
    assert reply.status == 401


def test_bearer_scheme_is_case_insensitive(api: Api) -> None:
    header = f"bearer {api.server.token}"
    assert api.client.get("/api/v1/sessions", headers={"Authorization": header}).status == 200


def test_duplicate_authorization_headers_are_rejected(api: Api) -> None:
    token = api.server.token
    raw = (
        f"GET /api/v1/sessions HTTP/1.1\r\nHost: 127.0.0.1:{api.server.port}\r\n"
        f"Authorization: Bearer {token}\r\nAuthorization: Bearer {token}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    assert raw_request(api.server.port, raw).status == 401


def test_token_in_query_string_never_authenticates(api: Api) -> None:
    token = api.server.token
    for query in (f"token={token}", f"access_token={token}", f"authorization=Bearer%20{token}"):
        reply = api.client.get(f"/api/v1/sessions?{query}", token=None)
        assert reply.status == 401
        reply = api.client.get(f"{api.base}/timeline?{query}", token=None)
        assert reply.status == 401
        assert api.client.get(f"/api/v1/sessions?{query}").status == 400  # unknown parameter


def test_token_is_fresh_per_server_and_never_echoed(api: Api, project: Path) -> None:
    other = ReplayServer(ReplayService(project))
    try:
        assert other.token != api.server.token
        assert len(api.server.token) >= 43  # 32 random bytes, URL-safe base64
    finally:
        other.stop()
    replies = [api.client.get(path) for path in api.routes()]
    replies += [api.client.get(path, token=None) for path in api.routes()]
    replies += [api.client.get("/api/v1/nope"), api.client.request("POST", "/api/v1/sessions")]
    replies += [api.client.get(f"/api/v1/sessions?token={api.server.token}")]
    for reply in replies:
        assert api.server.token not in reply.text()


# -- browser protections ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "http://evil.example:80"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1"},  # wrong port: a different local origin
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_requests_are_refused(api: Api, headers: dict[str, str]) -> None:
    for path in [*api.routes(), "/api/v1/health"]:
        for token in (api.server.token, None):
            reply = api.client.get(path, token=token, headers=headers)
            assert reply.status == 403, (path, headers)
            assert reply.error_code == "origin_not_allowed"
            assert api.sid not in reply.text()
            assert_safe_headers(reply)


def test_same_origin_requests_are_allowed(api: Api) -> None:
    port = api.server.port
    for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
        reply = api.client.get("/api/v1/sessions", headers=headers)
        assert reply.status == 200
        assert_safe_headers(reply)  # still no CORS headers, not even for our own origin


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "{port}"])
def test_foreign_host_header_is_refused(api: Api, host: str) -> None:
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example."""
    headers = {"Host": host.format(port=api.server.port)}
    for path in ("/api/v1/health", "/api/v1/sessions"):
        reply = api.client.get(path, headers=headers)
        assert reply.status == 403 and reply.error_code == "host_not_allowed"


def test_localhost_host_header_is_allowed(api: Api) -> None:
    headers = {"Host": f"localhost:{api.server.port}"}
    assert api.client.get("/api/v1/sessions", headers=headers).status == 200


def test_options_preflight_opens_nothing(api: Api) -> None:
    preflight = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    for headers in (preflight, {}):
        reply = api.client.request("OPTIONS", "/api/v1/sessions", headers=headers)
        assert reply.status == 405
        assert reply.header("Allow") == "GET, HEAD"
        assert_safe_headers(reply)


def test_security_headers_on_every_kind_of_response(api: Api) -> None:
    replies = [
        api.client.get("/api/v1/health"),
        api.client.get(api.base),
        api.client.get("/api/v1/sessions", token=None),
        api.client.get("/api/v1/nope"),
        api.client.get("/api/v1/sessions/bad-id"),
        api.client.get(f"{api.base}/timeline?limit=0"),
        api.client.get("/api/v1/sessions", headers={"Origin": "https://evil.example"}),
        api.client.request("DELETE", api.base),
        raw_request(api.server.port, b"NONSENSE\r\n"),
    ]
    for reply in replies:
        assert_safe_headers(reply)
        assert reply.header("X-Frame-Options") == "DENY"
        assert reply.header("Cross-Origin-Resource-Policy") == "same-origin"
        assert reply.header("Server") == "AgentBodycam"


# -- methods ---------------------------------------------------------------------------------


@pytest.mark.parametrize("method", [*MUTATIONS, "OPTIONS", "TRACE", "PROPFIND", "FOO"])
def test_only_get_and_head_exist(api: Api, method: str) -> None:
    before = store_snapshot(api.store.project_root)
    for path in [*api.routes(), "/api/v1/health", "/api/v1/nope"]:
        reply = api.client.request(method, path, body=b'{"status": "stopped"}')
        assert reply.status == 405, (method, path)
        assert reply.error_code == "method_not_allowed"
        assert reply.header("Allow") == "GET, HEAD"
        assert_safe_headers(reply)
    assert store_snapshot(api.store.project_root) == before


def test_head_matches_get_without_body(api: Api) -> None:
    for path in api.routes():
        get = api.client.get(path)
        head = api.client.request("HEAD", path)
        assert head.status == get.status == 200
        assert head.body == b""
        assert head.header("Content-Length") == str(len(get.body))
        assert head.header("Content-Type") == get.header("Content-Type")
    assert api.client.request("HEAD", "/api/v1/sessions", token=None).status == 401


# -- routes ----------------------------------------------------------------------------------


def test_project_summary(api: Api) -> None:
    reply = api.client.get("/api/v1/project")
    assert reply.status == 200
    assert reply.json == {"api_version": 1, "session_count": 1}


def test_sessions_and_detail_match_the_service(api: Api) -> None:
    service = ReplayService(api.store.project_root)
    assert api.client.get("/api/v1/sessions").json == service.list_sessions().to_dict()
    detail = api.client.get(api.base).json
    assert detail == service.session_detail(api.sid).to_dict()
    assert (detail["recovery_count"], detail["observable_failure_count"]) == (1, 1)


def test_report_equals_analyze_json_byte_for_byte(api: Api) -> None:
    out = io.StringIO()
    project = str(api.store.project_root)
    assert main(["analyze", "--json", "--project", project, api.sid], out=out) == 0
    first = api.client.get(f"{api.base}/report")
    second = api.client.get(f"{api.base}/report")
    assert first.status == 200
    assert first.body == second.body
    assert first.body.decode("utf-8") == out.getvalue()
    report = analyze_session(api.store, api.sid)
    assert first.json == report.to_dict()


def test_report_findings_are_observed_order_only(api: Api) -> None:
    data = api.client.get(f"{api.base}/report").json
    first = data["first_observable_failure"]
    assert isinstance(first, dict)
    assert first["summary"] == "Test failed: pytest (exit 1)"
    assert first["finding_evidence"] == "observed"
    recoveries = data["recoveries"]
    assert isinstance(recoveries, dict) and recoveries["total"] == 1
    for path in api.routes():
        text = api.client.get(path).body.decode("utf-8")
        assert CAUSAL_LANGUAGE.search(text) is None, (path, CAUSAL_LANGUAGE.search(text))


def test_timeline_over_http(api: Api) -> None:
    page = api.client.get(f"{api.base}/timeline").json
    assert (page["total"], page["has_more"], page["next_after_sequence"]) == (14, False, 14)
    items = page["items"]
    assert isinstance(items, list) and len(items) == 14
    collected: list[object] = []
    cursor = 0
    while True:
        reply = api.client.get(f"{api.base}/timeline?after_sequence={cursor}&limit=5")
        assert reply.status == 200
        data = reply.json
        assert isinstance(data["items"], list)
        collected += data["items"]
        assert isinstance(data["next_after_sequence"], int)
        cursor = data["next_after_sequence"]
        if not data["has_more"]:
            break
    assert collected == items
    last = api.client.get(f"{api.base}/timeline?limit=1&after_sequence=13").json
    assert last["items"] == items[13:] and last["has_more"] is False


@pytest.mark.parametrize(
    "query", ["", "?", "?limit=1", "?limit=500", "?after_sequence=0&limit=100"]
)
def test_valid_timeline_queries(api: Api, query: str) -> None:
    assert api.client.get(f"{api.base}/timeline{query}").status == 200


@pytest.mark.parametrize(
    "query",
    [
        "limit=abc",
        "limit=1.5",
        "limit=1e3",
        "limit=",
        "limit",
        "limit=+5",
        "limit=%31",
        "limit=0",
        "limit=-5",
        "limit=501",
        "limit=99999999999999999999999",
        "after_sequence=-1",
        "after_sequence=x",
        "limit=1&limit=2",
        "after_sequence=1&after_sequence=1",
        "LIMIT=1",
        "foo=1",
        "limit=1&",
        "&limit=1",
        "limit=1;after_sequence=2",
    ],
)
def test_invalid_timeline_queries(api: Api, query: str) -> None:
    reply = api.client.get(f"{api.base}/timeline?{query}")
    assert reply.status == 400, query
    assert reply.error_code == "invalid_parameter"


def test_other_routes_reject_query_parameters(api: Api) -> None:
    for path in ("/api/v1/health", "/api/v1/project", "/api/v1/sessions", api.base):
        reply = api.client.get(f"{path}?limit=1")
        assert reply.status == 400 and reply.error_code == "invalid_parameter"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/api",
        "/api/v1",
        "/api/v1/",
        "/api/v1/nope",
        "/api/v2/sessions",
        "/api/v1/sessions/{sid}/",
        "/api/v1/sessions/{sid}/events",
        "/api/v1/sessions/{sid}/payload",
        "/api/v1/sessions/{sid}/findings",
        "/api/v1/sessions/{sid}/report/extra",
        "/api/v1//sessions",
    ],
)
def test_unknown_routes(api: Api, path: str) -> None:
    reply = api.client.get(path.format(sid=api.sid))
    assert reply.status == 404 and reply.error_code == "not_found"


def test_unknown_session(api: Api) -> None:
    for suffix in ("", "/report", "/timeline"):
        reply = api.client.get(f"/api/v1/sessions/{UNKNOWN_SID}{suffix}")
        assert reply.status == 404 and reply.error_code == "session_not_found"
        assert reply.json["error"] == {"code": "session_not_found", "message": "Session not found."}


@pytest.mark.parametrize(
    "segment",
    [
        "",  # /api/v1/sessions/ and /api/v1/sessions//report
        "not-a-session",
        "..",
        "%2e%2e",
        "%2E%2E",
        "..%2f..%2fetc%2fpasswd",
        "..%5c..%5cwindows",
        "%2e%2e%2f%2e%2e%2f",
        "{sid}%00",
        "{sid}%2f..",
        "{sid}..",
        "{upper}",
        "C:%5cWindows",
        "~",
    ],
)
def test_malformed_session_ids(api: Api, segment: str) -> None:
    value = segment.format(sid=api.sid, upper=api.sid.upper())
    for suffix in ("", "/report", "/timeline"):
        reply = api.client.get(f"/api/v1/sessions/{value}{suffix}")
        assert reply.status == 400, value
        assert reply.error_code == "invalid_session_id"


@pytest.mark.parametrize(
    "target",
    [
        "/api/v1/sessions/../../etc/passwd",
        "/api/v1/sessions/../../../../windows/win.ini",
        "/api/v1/../../../.agentbodycam/sessions",
        "/api/v1/sessions/..\\..\\session.json",
        "/api/v1/sessions/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
        "/api/v1/sessions/{sid}/../../../{sid}/session.json",
        "/../.agentbodycam/sessions/{sid}/events.jsonl",
        "http://127.0.0.1/api/v1/sessions",
        "*",
    ],
)
def test_path_traversal_is_never_mapped_to_files(api: Api, target: str) -> None:
    request_target = target.format(sid=api.sid)
    raw = (
        f"GET {request_target} HTTP/1.1\r\nHost: 127.0.0.1:{api.server.port}\r\n"
        f"Authorization: Bearer {api.server.token}\r\nConnection: close\r\n\r\n"
    ).encode()
    reply = raw_request(api.server.port, raw)
    assert reply.status in (400, 404), request_target
    assert set(reply.json) == {"error"}
    text = reply.text()
    assert "event_id" not in text and "project_root" not in text and "root:" not in text


# -- errors ----------------------------------------------------------------------------------


def test_unreadable_session_is_a_safe_500(api: Api) -> None:
    with api.store.events_path(api.sid).open("ab") as fh:
        fh.write(b"garbage\n")
    reply = api.client.get(f"{api.base}/report")
    assert reply.status == 500
    assert reply.json == {
        "error": {
            "code": "session_unreadable",
            "message": "The session's stored evidence could not be read.",
        }
    }
    listing = api.client.get("/api/v1/sessions").json
    assert listing["sessions"] == [{**_nulls(api.sid), "readable": False}]


def _nulls(sid: str) -> dict[str, object]:
    keys = ("status", "recording_state", "analysis_complete", "started_at", "ended_at")
    return {"session_id": sid, **dict.fromkeys(keys), "event_count": None}


def test_unexpected_errors_leak_nothing(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path = str(api.store.project_root / "private")

    def explode(_self: ReplayService, _sid: str) -> NoReturn:
        raise RuntimeError(f"boom at {secret_path}")

    monkeypatch.setattr(ReplayService, "report_dict", explode)
    reply = api.client.get(f"{api.base}/report")
    assert reply.status == 500
    assert reply.json == {
        "error": {"code": "internal_error", "message": "Unexpected server error."}
    }
    assert "boom" not in reply.text() and "Traceback" not in reply.text()
    assert api.client.get("/api/v1/sessions").status == 200  # still serving


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        # Only the request line is sent: the server answers before reading headers, and
        # unread bytes would make Windows reset the connection.
        (b"NONSENSE\r\n", 400),
        (b"GET /api/v1/health HTTP/9.9\r\n", 505),
        (b"GET /api/v1/health HTTP/1.1 extra\r\n", 400),
        (b"GET /api/v1/health HTTP/1.x\r\n", 400),
    ],
)
def test_malformed_http_gets_json_errors(api: Api, raw: bytes, status: int) -> None:
    reply = raw_request(api.server.port, raw)
    assert reply.status == status
    assert set(reply.json) == {"error"}
    assert b"<html" not in reply.body.lower() and b"<!doctype" not in reply.body.lower()
    assert b"HTTP/9.9" not in reply.body and b"NONSENSE" not in reply.body


# -- logging ---------------------------------------------------------------------------------


def test_request_log_has_no_token_ids_or_queries(api: Api) -> None:
    api.client.get(f"{api.base}/timeline?after_sequence=3&limit=7")
    api.client.get(f"{api.base}/report", token="not-the-token")  # noqa: S106
    api.client.get(f"/api/v1/sessions?token={api.server.token}")
    api.client.request("POST", api.base)
    api.client.get("/api/v1/sessions", headers={"Origin": "https://evil.example"})
    # The server thread logs after the response is sent, so the last line may lag.
    wait_until(lambda: len(api.logs) >= 5, timeout=10, what="request log lines")
    assert api.logs == [
        "GET /api/v1/sessions/{session_id}/timeline 200",
        "GET (unmatched) 401",
        "GET /api/v1/sessions 400",
        "POST (unmatched) 405",
        "GET (unmatched) 403",
    ]
    text = "\n".join(api.logs)
    for secret in (api.server.token, api.sid, "after_sequence", "Authorization", "evil"):
        assert secret not in text


def test_server_without_log_callback_prints_nothing(
    store: EventStore, capsys: pytest.CaptureFixture[str]
) -> None:
    SessionBuilder(store).stop()
    server = serve(store.project_root)
    try:
        client = ApiClient(server)
        client.get("/api/v1/sessions")
        client.get("/api/v1/sessions", token="bad")  # noqa: S106
        raw_request(server.port, b"NONSENSE\r\n")
    finally:
        server.stop()
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# -- read-only -------------------------------------------------------------------------------


def test_starting_the_api_creates_nothing(project: Path) -> None:
    server = serve(project)
    try:
        client = ApiClient(server)
        assert client.get("/api/v1/project").json == {"api_version": 1, "session_count": 0}
        assert client.get("/api/v1/sessions").json["sessions"] == []
        assert client.get(f"/api/v1/sessions/{UNKNOWN_SID}/report").status == 404
    finally:
        server.stop()
    assert list(project.iterdir()) == []


def test_requests_do_not_modify_the_store(api: Api) -> None:
    before = store_snapshot(api.store.project_root)
    for _ in range(2):
        for path in [*api.routes(), "/api/v1/health", f"/api/v1/sessions/{UNKNOWN_SID}"]:
            api.client.get(path)
            api.client.request("HEAD", path)
        for method in MUTATIONS:
            api.client.request(method, api.base, body=b"{}")
    assert store_snapshot(api.store.project_root) == before


def test_requests_run_no_process(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    crashed = SessionBuilder(api.store)  # still 'recording': the liveness probe runs
    crashed.git()
    started: list[object] = []

    def refuse(*args: Any, **_kwargs: Any) -> NoReturn:
        started.append(args)
        raise AssertionError("the Replay API must not start processes")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    for sid in (api.sid, crashed.session_id):
        for suffix in ("", "/report", "/timeline"):
            assert api.client.get(f"/api/v1/sessions/{sid}{suffix}").status == 200
    assert api.client.get("/api/v1/sessions").status == 200
    assert started == []


def test_token_is_never_written_to_disk(api: Api, tmp_path: Path) -> None:
    for route in api.routes():
        api.client.get(route)
    api.client.get(f"/api/v1/sessions?token={api.server.token}")
    token = api.server.token.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token not in path.read_bytes(), path


# -- concurrency and shutdown ----------------------------------------------------------------


def test_concurrent_requests(api: Api) -> None:
    before = store_snapshot(api.store.project_root)
    paths = api.routes() * 8
    expected = {path: api.client.get(path).body for path in set(paths)}

    def fetch(path: str) -> tuple[str, Reply]:
        return path, api.client.get(path)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fetch, paths))
    assert len(results) == len(paths)
    for path, reply in results:
        assert reply.status == 200, path
        assert reply.body == expected[path]
    assert store_snapshot(api.store.project_root) == before


def test_clean_shutdown(project: Path) -> None:
    threads_before = set(threading.enumerate())
    server = serve(project)
    port = server.port
    client = ApiClient(server)
    assert client.get("/api/v1/health").status == 200
    server.stop()
    server.stop()  # idempotent
    assert set(threading.enumerate()) <= threads_before
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def test_context_manager_stops_the_server(project: Path) -> None:
    with ReplayServer(ReplayService(project)) as server:
        port = server.port
        assert ApiClient(server).get("/api/v1/health").json["status"] == "ok"
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def test_stop_without_start_releases_the_port(project: Path) -> None:
    server = ReplayServer(ReplayService(project))
    port = server.port
    server.stop()
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def test_report_json_is_deterministic_text(api: Api) -> None:
    body = api.client.get(f"{api.base}/report").body.decode("utf-8")
    assert body == json.dumps(json.loads(body), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
