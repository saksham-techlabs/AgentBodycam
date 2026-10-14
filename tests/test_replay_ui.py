"""Replay UI on the Replay server's own origin: documents, packaged assets, path safety,
security headers, the token bootstrap, the `replay` command and packaging."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import agentbodycam
from agentbodycam.cli import _run_replay, build_parser, main
from agentbodycam.core.replay_server import UI_CONTENT_SECURITY_POLICY, ReplayServer
from agentbodycam.core.store import EventStore
from agentbodycam.core.ui_assets import (
    CONTENT_TYPES,
    UiBundle,
    UiUnavailableError,
    load_ui_bundle,
)
from command_helpers import wait_until
from forensics_helpers import SessionBuilder
from replay_helpers import ApiClient, Endpoint, Reply, raw_request, serve, store_snapshot

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(agentbodycam.__file__).resolve().parent
STATIC = PACKAGE_DIR / "ui" / "static"
API_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
BROWSER_FETCH = {
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "X-AgentBodycam-UI": "bootstrap",
}
FILE_MARKER = "PROJECT_FILE_MARKER_7c1e55"
REFERENCE = re.compile(rb"""(?:src|href)\s*=\s*["']([^"']+)["']""")
LISTENING = re.compile(r"^Listening: http://127\.0\.0\.1:(\d+)$", re.MULTILINE)


@dataclass
class Ui:
    client: ApiClient
    server: ReplayServer
    sid: str
    logs: list[str]
    store: EventStore

    def bootstrap(self, **headers: str) -> Reply:
        return self.client.get("/ui/bootstrap", token=None, headers={**BROWSER_FETCH, **headers})

    def api_routes(self) -> list[str]:
        base = f"/api/v1/sessions/{self.sid}"
        return [
            "/api/v1/project",
            "/api/v1/sessions",
            base,
            f"{base}/report",
            f"{base}/timeline?after_sequence=1&limit=3",
        ]


@pytest.fixture(scope="module")
def bundle() -> UiBundle:
    return load_ui_bundle()


@pytest.fixture
def ui(store: EventStore, bundle: UiBundle) -> Iterator[Ui]:
    b = SessionBuilder(store)
    b.run_check(False)
    b.file("modified", "app.py")
    b.run_check(True)
    b.stop()
    (store.project_root / "secret.txt").write_text(FILE_MARKER, encoding="utf-8")
    logs: list[str] = []
    server = serve(store.project_root, log=logs.append, ui=bundle)
    try:
        yield Ui(ApiClient(server), server, b.session_id, logs, store)
    finally:
        server.stop()


def index_references(index: bytes) -> list[str]:
    return [match.decode("ascii") for match in REFERENCE.findall(index)]


def csp_directives(value: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in value.split(";"):
        name, *sources = part.split()
        directives[name] = sources
    return directives


def assert_api_headers(reply: Reply) -> None:
    assert reply.header("Content-Type") == "application/json; charset=utf-8"
    assert reply.header("Content-Security-Policy") == API_CSP
    assert reply.header("Cache-Control") == "no-store"
    assert reply.header("X-Content-Type-Options") == "nosniff"
    assert reply.header("X-Frame-Options") == "DENY"
    assert not any(name.startswith("access-control-") for name in reply.header_names())


# -- the UI document -------------------------------------------------------------------------


def test_root_serves_the_packaged_ui_document(ui: Ui) -> None:
    reply = ui.client.get("/", token=None)
    assert reply.status == 200
    assert reply.body == (STATIC / "index.html").read_bytes()
    assert reply.header("Content-Type") == "text/html; charset=utf-8"
    assert reply.header("Content-Security-Policy") == UI_CONTENT_SECURITY_POLICY
    assert reply.header("Cache-Control") == "no-store"
    assert reply.header("X-Content-Type-Options") == "nosniff"
    assert reply.header("X-Frame-Options") == "DENY"
    assert reply.header("Cross-Origin-Opener-Policy") == "same-origin"
    assert reply.header("Cross-Origin-Resource-Policy") == "same-origin"
    assert reply.header("Referrer-Policy") == "no-referrer"
    assert reply.header("Server") == "AgentBodycam"
    assert not any(name.startswith("access-control-") for name in reply.header_names())
    assert ui.logs[-1] == "GET / 200"


def test_ui_csp_is_strict() -> None:
    directives = csp_directives(UI_CONTENT_SECURITY_POLICY)
    assert directives["default-src"] == ["'none'"]
    for name in ("script-src", "style-src", "connect-src", "img-src", "font-src"):
        assert directives[name] == ["'self'"], name
    for name in ("object-src", "base-uri", "form-action", "frame-ancestors"):
        assert directives[name] == ["'none'"], name
    text = UI_CONTENT_SECURITY_POLICY.lower()
    for forbidden in ("unsafe-inline", "unsafe-eval", "*", "http:", "https:", "data:", "blob:"):
        assert forbidden not in text, forbidden


def test_session_routes_serve_the_document(ui: Ui) -> None:
    index = ui.client.get("/", token=None).body
    for path in (f"/session/{ui.sid}", "/session/20261014T000000Z-0000000000000000"):
        reply = ui.client.get(path, token=None)
        assert reply.status == 200 and reply.body == index, path
        assert reply.header("Content-Security-Policy") == UI_CONTENT_SECURITY_POLICY
    assert ui.logs[-1] == "GET /session/{session_id} 200"
    assert ui.sid not in "\n".join(ui.logs)


@pytest.mark.parametrize(
    "path",
    [
        "/session",
        "/session/",
        "/session/not-a-session",
        "/session/{sid}/",
        "/session/{sid}/report",
        "/session/{upper}",
        "/session/..",
        "/sessions/{sid}",
        "/index.html",
        "/static/index.html",
        "/ui",
        "/ui/",
        "/ui/static/index.html",
        "/assets",
        "/assets/",
    ],
)
def test_only_known_ui_paths_exist(ui: Ui, path: str) -> None:
    reply = ui.client.get(path.format(sid=ui.sid, upper=ui.sid.upper()), token=None)
    assert reply.status == 404 and reply.error_code == "not_found"
    assert_api_headers(reply)


def test_ui_routes_take_no_query_string(ui: Ui, bundle: UiBundle) -> None:
    asset = next(iter(bundle.assets))
    for path in ("/?x=1", f"/session/{ui.sid}?tab=timeline", "/?", f"{asset}?v=1"):
        reply = ui.client.get(path, token=None)
        assert reply.status == 400 and reply.error_code == "invalid_parameter", path


def test_head_on_ui_routes(ui: Ui, bundle: UiBundle) -> None:
    for path in ("/", f"/session/{ui.sid}", *bundle.assets):
        get = ui.client.get(path, token=None)
        head = ui.client.request("HEAD", path, token=None)
        assert head.status == get.status == 200
        assert head.body == b""
        assert head.header("Content-Length") == str(len(get.body))


def test_ui_is_read_only(ui: Ui) -> None:
    before = store_snapshot(ui.store.project_root)
    for method in ("POST", "PUT", "DELETE", "OPTIONS"):
        for path in ("/", f"/session/{ui.sid}", "/ui/bootstrap"):
            reply = ui.client.request(method, path, token=None, headers=BROWSER_FETCH)
            assert reply.status == 405 and reply.header("Allow") == "GET, HEAD"
            assert_api_headers(reply)
    assert store_snapshot(ui.store.project_root) == before


# -- packaged assets -------------------------------------------------------------------------


def test_index_references_only_packaged_same_origin_assets(bundle: UiBundle) -> None:
    references = index_references(bundle.index.body)
    assert references, "index.html references no assets"
    for ref in references:
        assert ref.startswith("/") and not ref.startswith("//"), ref
        assert bundle.asset(ref) is not None, f"index.html references a missing asset: {ref}"
    assert any(ref.endswith(".js") for ref in references)
    assert any(ref.endswith(".css") for ref in references)


def test_index_has_no_inline_code_or_remote_resources(bundle: UiBundle) -> None:
    html = bundle.index.body.decode("utf-8").lower()
    for script in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in script, f"inline script: {script}"
    assert "<style" not in html and " style=" not in html
    assert re.search(r"\son[a-z]+\s*=", html) is None, "inline event handler"
    assert "http://" not in html and "https://" not in html
    for path, asset in bundle.assets.items():
        if path.endswith(".css"):
            css = asset.body.decode("utf-8").lower()
            assert "@import" not in css and "url(http" not in css and "url(//" not in css


def test_no_remote_fonts_analytics_or_cdns(bundle: UiBundle) -> None:
    for path, asset in [("/", bundle.index), *bundle.assets.items()]:
        text = asset.body.decode("utf-8", "replace").lower()
        for host in ("fonts.googleapis", "fonts.gstatic", "googletagmanager", "cdn.", "unpkg"):
            assert host not in text, (path, host)


def test_assets_are_served_with_safe_types_and_api_headers(ui: Ui, bundle: UiBundle) -> None:
    for path, asset in bundle.assets.items():
        reply = ui.client.get(path, token=None)
        assert reply.status == 200, path
        assert reply.body == asset.body
        suffix = path[path.rfind(".") :]
        assert reply.header("Content-Type") == CONTENT_TYPES[suffix]
        assert reply.header("Content-Security-Policy") == API_CSP
        assert reply.header("X-Content-Type-Options") == "nosniff"
        assert reply.header("Cache-Control") == "no-store"
        assert reply.header("Cross-Origin-Resource-Policy") == "same-origin"
    assert "GET (ui asset) 200" in ui.logs


def test_every_servable_file_is_a_packaged_build_file(bundle: UiBundle) -> None:
    on_disk = {
        "/" + path.relative_to(STATIC).as_posix()
        for path in STATIC.rglob("*")
        if path.is_file() and path.name != "index.html"
    }
    assert set(bundle.assets) == on_disk
    for path in bundle.assets:
        assert path.count("/") in (1, 2) and ".." not in path and "\\" not in path


# -- path safety -----------------------------------------------------------------------------


TRAVERSALS = [
    "/../pyproject.toml",
    "/..%2fpyproject.toml",
    "/%2e%2e/%2e%2e/secret.txt",
    "/assets/../index.html",
    "/assets/..%2f..%2fcli.py",
    "/assets/%2e%2e%5c%2e%2e%5ccli.py",
    "/assets\\..\\..\\cli.py",
    "/./index.html",
    "//index.html",
    "/favicon.svg/",
    "/favicon.svg/..",
    "/secret.txt",
    "/my%20project/secret.txt",
    "/.agentbodycam/sessions/{sid}/events.jsonl",
    "/.agentbodycam/sessions/{sid}/session.json",
    "/.agentbodycam/.gitignore",
    "/session/{sid}/../../.agentbodycam/sessions/{sid}/events.jsonl",
    "/cli.py",
    "/__init__.py",
    "/ui/__init__.py",
    "/core/ui_assets.py",
    "/py.typed",
    "/C:/Windows/win.ini",
    "/C:%5cWindows%5cwin.ini",
    "/etc/passwd",
    "/web/package.json",
    "/src/agentbodycam/ui/static/index.html",
    "/assets/index.html",
    "/assets/.gitkeep",
    "/ASSETS/{asset}",
    "/assets//{asset}",
    "/assets/{asset}/",
    "/assets/{asset}%00.html",
]


@pytest.mark.parametrize("target", TRAVERSALS)
def test_traversal_and_arbitrary_files_are_never_served(
    ui: Ui, bundle: UiBundle, target: str
) -> None:
    asset = next(path for path in bundle.assets if path.startswith("/assets/")).split("/")[-1]
    request_target = target.format(sid=ui.sid, asset=asset)
    raw = (
        f"GET {request_target} HTTP/1.1\r\nHost: 127.0.0.1:{ui.server.port}\r\n"
        f"Authorization: Bearer {ui.server.token}\r\nConnection: close\r\n\r\n"
    ).encode()
    reply = raw_request(ui.server.port, raw)
    assert reply.status in (400, 404), request_target
    assert set(reply.json) == {"error"}
    assert_api_headers(reply)
    text = reply.text()
    for marker in (FILE_MARKER, "event_id", "session_id", "import ", "[tool.", "<html"):
        assert marker not in text, (request_target, marker)


def test_absolute_project_paths_are_not_routes(ui: Ui) -> None:
    root = ui.store.project_root.resolve().as_posix()
    for path in (f"/{root}/secret.txt", f"/{root.lstrip('/')}/secret.txt"):
        reply = ui.client.get(path.replace(" ", "%20"), token=None)
        assert reply.status == 404
        assert FILE_MARKER not in reply.text()


# -- API routes are unchanged ----------------------------------------------------------------


def _comparable(reply: Reply) -> tuple[int, bytes, list[tuple[str, str]]]:
    headers = sorted((k.lower(), v) for k, v in reply.headers if k.lower() != "date")
    return reply.status, reply.body, headers


def test_api_routes_behave_exactly_as_without_the_ui(ui: Ui) -> None:
    plain = serve(ui.store.project_root)
    try:
        plain_client = ApiClient(plain)
        paths = [*ui.api_routes(), "/api/v1/health", "/api/v1/nope", "/api/v1/sessions/bad"]
        for path in paths:
            for auth in ("own", None, "wrong"):
                with_ui = ui.client.get(path, token=ui.server.token if auth == "own" else auth)
                without = plain_client.get(path, token=plain.token if auth == "own" else auth)
                assert _comparable(with_ui) == _comparable(without), (path, auth)
                assert_api_headers(with_ui)
    finally:
        plain.stop()


def test_report_still_equals_analyze_json(ui: Ui) -> None:
    out = io.StringIO()
    project = str(ui.store.project_root)
    assert main(["analyze", "--json", "--project", project, ui.sid], out=out) == 0
    reply = ui.client.get(f"/api/v1/sessions/{ui.sid}/report")
    assert reply.status == 200 and reply.body.decode("utf-8") == out.getvalue()


def test_api_still_requires_the_bearer_token(ui: Ui) -> None:
    for path in ui.api_routes():
        reply = ui.client.get(path, token=None, headers=BROWSER_FETCH)
        assert reply.status == 401, path


def test_api_only_server_serves_no_ui(store: EventStore, bundle: UiBundle) -> None:
    SessionBuilder(store).stop()
    server = serve(store.project_root)
    try:
        client = ApiClient(server)
        for path in ["/", "/ui/bootstrap", "/session/20261014T000000Z-0000000000000000"]:
            reply = client.get(path, headers=BROWSER_FETCH)
            assert reply.status == 404 and reply.error_code == "not_found", path
            assert server.token not in reply.text()
        for path in bundle.assets:
            assert client.get(path).status == 404
    finally:
        server.stop()


def test_ui_and_api_share_one_loopback_origin(ui: Ui) -> None:
    port = ui.server.port
    assert ui.server.url == f"http://127.0.0.1:{port}"
    page = ui.client.get("/", token=None)
    origin = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
    api = ui.client.get("/api/v1/sessions", headers=origin)
    assert page.status == api.status == 200
    other_port = {"Origin": f"http://127.0.0.1:{port + 1}"}
    assert ui.client.get("/api/v1/sessions", headers=other_port).status == 403
    assert ui.client.get("/", token=None, headers={"Host": f"evil.example:{port}"}).status == 403


# -- token bootstrap -------------------------------------------------------------------------


def test_bootstrap_hands_the_token_to_a_same_origin_ui_fetch(ui: Ui) -> None:
    reply = ui.bootstrap()
    assert reply.status == 200
    assert reply.json == {"api_version": 1, "token": ui.server.token}
    assert_api_headers(reply)
    assert reply.header("Cross-Origin-Resource-Policy") == "same-origin"
    assert ui.client.get("/api/v1/sessions", token=reply.json["token"]).status == 200
    same_origin = ui.bootstrap(**{"Sec-Fetch-Mode": "same-origin"})
    assert same_origin.status == 200
    with_origin = ui.bootstrap(Origin=f"http://localhost:{ui.server.port}")
    assert with_origin.status == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"X-AgentBodycam-UI": ""},
        {"X-AgentBodycam-UI": "1"},
        {"X-AgentBodycam-UI": "BOOTSTRAP"},
        {"Sec-Fetch-Site": "none"},  # typed into the address bar
        {"Sec-Fetch-Site": ""},
        {"Sec-Fetch-Mode": "navigate"},
        {"Sec-Fetch-Mode": "no-cors"},  # <script src> / <img src> from any page
        {"Sec-Fetch-Dest": "document"},
        {"Sec-Fetch-Dest": "iframe"},
        {"Sec-Fetch-Dest": "script"},
    ],
)
def test_bootstrap_refuses_anything_but_the_ui_fetch(ui: Ui, headers: dict[str, str]) -> None:
    reply = ui.bootstrap(**headers)
    assert reply.status == 403 and reply.error_code == "bootstrap_not_allowed"
    assert ui.server.token not in reply.text()
    assert_api_headers(reply)


@pytest.mark.parametrize(
    "missing", ["X-AgentBodycam-UI", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"]
)
def test_bootstrap_requires_every_marker(ui: Ui, missing: str) -> None:
    headers = {k: v for k, v in BROWSER_FETCH.items() if k != missing}
    reply = ui.client.get("/ui/bootstrap", token=None, headers=headers)
    assert reply.status == 403
    assert ui.server.token not in reply.text()


def test_plain_http_clients_get_no_token(ui: Ui) -> None:
    """curl-style requests (no browser fetch metadata) are refused."""
    for headers in ({}, {"Authorization": f"Bearer {ui.server.token}"}):
        reply = ui.client.get("/ui/bootstrap", token=None, headers=headers)
        assert reply.status == 403
        assert ui.server.token not in reply.text()


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:1"},
        {"Host": "evil.example:{port}"},
        {"Host": "127.0.0.1:1"},
    ],
)
def test_other_sites_and_dns_rebinding_get_no_token(ui: Ui, headers: dict[str, str]) -> None:
    resolved = {k: v.format(port=ui.server.port) for k, v in headers.items()}
    reply = ui.bootstrap(**resolved)
    assert reply.status == 403
    assert reply.error_code in ("origin_not_allowed", "host_not_allowed")
    assert ui.server.token not in reply.text()


def test_bootstrap_preflight_is_never_approved(ui: Ui) -> None:
    preflight = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-agentbodycam-ui",
    }
    reply = ui.client.request("OPTIONS", "/ui/bootstrap", token=None, headers=preflight)
    assert reply.status == 405
    assert not any(name.startswith("access-control-") for name in reply.header_names())


def test_bootstrap_rejects_query_and_duplicate_headers(ui: Ui) -> None:
    assert ui.client.get("/ui/bootstrap?x=1", token=None, headers=BROWSER_FETCH).status == 403
    lines = "".join(f"{k}: {v}\r\n" for k, v in BROWSER_FETCH.items())
    raw = (
        f"GET /ui/bootstrap HTTP/1.1\r\nHost: 127.0.0.1:{ui.server.port}\r\n{lines}"
        "X-AgentBodycam-UI: bootstrap\r\nConnection: close\r\n\r\n"
    ).encode()
    reply = raw_request(ui.server.port, raw)
    assert reply.status == 403 and ui.server.token not in reply.text()


def test_token_reaches_only_the_bootstrap_response(ui: Ui, bundle: UiBundle) -> None:
    token = ui.server.token
    replies = [ui.client.get(path) for path in ui.api_routes()]
    replies += [ui.client.get(path, token=None) for path in ("/", f"/session/{ui.sid}")]
    replies += [ui.client.get(path, token=None) for path in bundle.assets]
    replies += [ui.client.get("/api/v1/health"), ui.client.get("/nope")]
    for reply in replies:
        assert token not in reply.text()
    assert token in ui.bootstrap().text()
    assert token not in "\n".join(ui.logs)
    assert "GET /ui/bootstrap 200" in ui.logs


def test_token_is_not_in_the_static_build(ui: Ui) -> None:
    token = ui.server.token.encode()
    for path in STATIC.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert token not in data, path
            assert re.search(rb"Bearer [A-Za-z0-9_-]{32,}", data) is None, path


def test_token_is_never_written_to_disk(ui: Ui, tmp_path: Path) -> None:
    ui.bootstrap()
    for path in ui.api_routes():
        ui.client.get(path)
    token = ui.server.token.encode()
    for root in (tmp_path, PACKAGE_DIR):
        for file in root.rglob("*"):
            if file.is_file():
                assert token not in file.read_bytes(), file


def test_each_server_instance_has_its_own_token(store: EventStore, bundle: UiBundle) -> None:
    first = serve(store.project_root, ui=bundle)
    second = serve(store.project_root, ui=bundle)
    try:
        a = ApiClient(first).get("/ui/bootstrap", token=None, headers=BROWSER_FETCH).json
        b = ApiClient(second).get("/ui/bootstrap", token=None, headers=BROWSER_FETCH).json
        assert a["token"] == first.token and b["token"] == second.token
        assert a["token"] != b["token"]
        assert ApiClient(second).get("/api/v1/sessions", token=a["token"]).status == 401
    finally:
        first.stop()
        second.stop()


# -- the `replay` command --------------------------------------------------------------------


def test_replay_command_serves_ui_and_api_without_printing_the_token(project: Path) -> None:
    args = build_parser().parse_args(["replay", "--project", str(project)])
    out, err = io.StringIO(), io.StringIO()
    stop = threading.Event()
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(_run_replay(args, out, err, stop)))
    runner.start()
    try:
        wait_until(lambda: "Press Ctrl+C to stop." in out.getvalue(), what="startup")
        match = LISTENING.search(out.getvalue())
        assert match is not None, out.getvalue()
        port = int(match.group(1))
        assert f"Open in your browser: http://127.0.0.1:{port}/\n" in out.getvalue()
        assert "under development" in out.getvalue()
        client = ApiClient(Endpoint("127.0.0.1", port, ""))
        page = client.get("/", token=None)
        assert page.status == 200 and b'<div id="root">' in page.body
        token = client.get("/ui/bootstrap", token=None, headers=BROWSER_FETCH).json["token"]
        assert isinstance(token, str) and len(token) >= 43
        assert client.get("/api/v1/project", token=token).json["session_count"] == 0
        assert client.get("/api/v1/project", token=None).status == 401
    finally:
        stop.set()
        runner.join(timeout=30)
    assert result == [0] and err.getvalue() == ""
    text = out.getvalue()
    assert token not in text
    assert "API token" not in text
    assert text.endswith("Replay stopped.\n")
    assert list(project.iterdir()) == []  # serving the UI created nothing


def test_replay_command_without_ui_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> UiBundle:
        raise UiUnavailableError("missing")

    monkeypatch.setattr("agentbodycam.cli.load_ui_bundle", missing)
    out, err = io.StringIO(), io.StringIO()
    assert main(["replay", "--project", str(project)], out=out, err=err) == 1
    assert "Replay UI files are missing" in err.getvalue()
    assert "replay-api" in err.getvalue()
    assert out.getvalue() == ""
    assert list(project.iterdir()) == []


def test_replay_command_refuses_public_hosts(project: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["replay", "--project", str(project), "--host", "0.0.0.0"])  # noqa: S104
    assert exc.value.code == 2


def test_replay_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["replay", "--help"])
    text = capsys.readouterr().out
    assert "under development" in text and "loopback" in text and "never through the URL" in text


# -- the asset loader ------------------------------------------------------------------------


def make_build(root: Path) -> Path:
    static = root / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<script src="/assets/app-1a2b.js"></script>', "utf-8")
    (static / "assets" / "app-1a2b.js").write_text("console.log(1)", "utf-8")
    (static / "assets" / "style-3c4d.css").write_text("body{}", "utf-8")
    (static / "favicon.svg").write_text("<svg/>", "utf-8")
    return static


def test_loader_reads_only_allowlisted_files(tmp_path: Path) -> None:
    static = make_build(tmp_path)
    for name in (
        ".hidden.js",
        "notes.txt",
        "page.html",
        "tool.py",
        "app.js.map",
        "data.json",
        "name with space.js",
    ):
        (static / name).write_text("x", "utf-8")
    (static / "assets" / "nested").mkdir()
    (static / "assets" / "nested" / "deep.js").write_text("x", "utf-8")
    (static / "other").mkdir()
    (static / "other" / "x.js").write_text("x", "utf-8")
    bundle = load_ui_bundle(static)
    assert set(bundle.assets) == {
        "/assets/app-1a2b.js",
        "/assets/style-3c4d.css",
        "/favicon.svg",
    }
    assert bundle.index.body.startswith(b"<script")
    assert bundle.asset("/assets/app-1a2b.js") is not None
    assert bundle.asset("/assets/../favicon.svg") is None
    assert bundle.asset("/index.html") is None


def test_loader_needs_an_index(tmp_path: Path) -> None:
    static = make_build(tmp_path)
    (static / "index.html").unlink()
    with pytest.raises(UiUnavailableError, match=r"index\.html"):
        load_ui_bundle(static)
    with pytest.raises(UiUnavailableError, match="missing"):
        load_ui_bundle(tmp_path / "nowhere")


def test_loader_bounds_sizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    static = make_build(tmp_path)
    monkeypatch.setattr("agentbodycam.core.ui_assets.MAX_ASSET_BYTES", 4)
    with pytest.raises(UiUnavailableError, match="large"):
        load_ui_bundle(static)


def test_loader_never_follows_links(tmp_path: Path) -> None:
    static = make_build(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_text(FILE_MARKER, "utf-8")
    try:
        (static / "assets" / "leak-0000.js").symlink_to(outside)
    except OSError:
        pytest.skip("cannot create symlinks here")
    bundle = load_ui_bundle(static)
    assert "/assets/leak-0000.js" not in bundle.assets
    assert all(FILE_MARKER.encode() not in a.body for a in bundle.assets.values())


def test_loader_reads_a_zipped_package(tmp_path: Path) -> None:
    static = make_build(tmp_path)
    archive = tmp_path / "ui.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in static.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(tmp_path).as_posix())
    with zipfile.ZipFile(archive) as zf:
        bundle = load_ui_bundle(zipfile.Path(zf, "static/"))
        assert set(bundle.assets) == {
            "/assets/app-1a2b.js",
            "/assets/style-3c4d.css",
            "/favicon.svg",
        }


# -- packaging: the installed package serves the UI without Node -----------------------------


SMOKE = r"""
import http.client, pathlib, shutil, sys, tempfile
site = sys.argv[1]
sys.path.insert(0, site)
import agentbodycam
assert pathlib.Path(agentbodycam.__file__).resolve().is_relative_to(pathlib.Path(site).resolve())
assert shutil.which("node") is None and shutil.which("npm") is None
from agentbodycam.core.replay import ReplayService
from agentbodycam.core.replay_server import ReplayServer
from agentbodycam.core.ui_assets import load_ui_bundle
bundle = load_ui_bundle()
server = ReplayServer(ReplayService(pathlib.Path(tempfile.mkdtemp())), ui=bundle)
server.start()
try:
    for path in ["/", *bundle.assets]:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        conn.request("GET", path)
        response = conn.getresponse()
        assert response.status == 200, (path, response.status)
        response.read()
        conn.close()
finally:
    server.stop()
print("UI-OK", len(bundle.assets))
"""


def run_smoke(site: Path) -> str:
    env = {"PATH": "", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test script
        [sys.executable, "-I", "-c", SMOKE, str(site)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_installed_style_package_serves_ui_without_node(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    shutil.copytree(
        PACKAGE_DIR, site / "agentbodycam", ignore=shutil.ignore_patterns("__pycache__")
    )
    assert "UI-OK" in run_smoke(site)


def test_wheel_ships_the_ui_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hatchling_build = pytest.importorskip("hatchling.build")
    monkeypatch.chdir(ROOT)
    wheel = tmp_path / hatchling_build.build_wheel(str(tmp_path))
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        expected = {
            "agentbodycam/ui/" + path.relative_to(STATIC.parent).as_posix()
            for path in STATIC.rglob("*")
            if path.is_file()
        }
        assert expected <= names
        assert "agentbodycam/ui/static/index.html" in names
        assert not any(name.startswith(("web/", "agentbodycam/web/")) for name in names)
        assert not any("node_modules" in name for name in names)
        site = tmp_path / "site"
        zf.extractall(site)
    assert "UI-OK" in run_smoke(site)
