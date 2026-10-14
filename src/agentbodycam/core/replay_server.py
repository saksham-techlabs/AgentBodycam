"""Local, read-only, authenticated HTTP transport for :class:`ReplayService`.

This is AgentBodycam's only use of sockets, and it is local IPC: the server binds to a
loopback address (``127.0.0.1``, ``::1``; ``localhost`` means ``127.0.0.1``) and refuses any
other, so it is never reachable from the network. It makes no outbound connection and no
DNS lookup.

Security model:

- **Bearer token.** A fresh ``secrets.token_urlsafe(32)`` per server instance, held in
  memory only (never written to disk or logged by this module) and compared with
  ``secrets.compare_digest``. Every ``/api/*`` route except ``/api/v1/health`` requires
  ``Authorization: Bearer <token>``; tokens in query strings are rejected like any other
  unknown parameter.
- **Browsers.** No CORS: no ``Access-Control-*`` header is ever sent, so a page on another
  origin cannot read a response. Requests carrying a foreign ``Origin`` or
  ``Sec-Fetch-Site: cross-site|same-site`` are refused, and the ``Host`` header must name the
  loopback address and port the server listens on (defeats DNS rebinding). A future UI is
  meant to be served from this same origin, so CORS is never needed.
- **Read-only.** Only GET and HEAD exist; every other method gets 405. Handlers call
  :class:`ReplayService`, which writes nothing and runs nothing.
- **No leaks.** Every response, including protocol errors, is JSON with ``no-store`` and
  ``nosniff``. Errors carry a code and a fixed message: no paths, tracebacks or exception
  text. The default access log is disabled; the optional ``log`` callback receives only the
  method, the route *template* (never the raw path or query) and the status.
- **Strict parsing.** Paths are never percent-decoded and never mapped to files: a session
  segment must match the session-ID pattern exactly, so ``..``, ``%2e%2e`` or encoded
  separators cannot reach the file system. Timeline parameters are strictly validated.

Optional Replay UI (``ui=``, used by ``agentbodycam replay``; ``replay-api`` serves none):

- **Same origin.** The packaged UI build (:mod:`agentbodycam.core.ui_assets`) is served from
  this server's own origin: the UI document for ``/`` and ``/session/<session-id>``, and
  the known build files by exact path from an in-memory allowlist. Nothing else outside
  ``/api/`` exists. The document gets its own strict CSP (same-origin scripts, styles and
  fetches only; no inline script, no eval, no framing); every other response keeps the API
  headers above unchanged.
- **Token handoff.** ``GET /ui/bootstrap`` returns this instance's token as JSON, only to a
  same-origin ``fetch()`` from the UI page: it requires ``X-AgentBodycam-UI: bootstrap``
  (a cross-origin page cannot send it without a CORS preflight, which is never approved)
  and browser fetch metadata (``Sec-Fetch-Site: same-origin``, ``Sec-Fetch-Mode:
  cors|same-origin``, ``Sec-Fetch-Dest: empty``), so other web sites, frames and plain
  navigations are refused. The page keeps the token in memory; it never appears in a URL,
  file, cookie, browser storage or log. Local programs can imitate these headers, so with
  the UI enabled the token no longer keeps out other local programs that can reach the
  loopback port (see SECURITY.md).
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import socketserver
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from agentbodycam.core.replay import ReplayError, ReplayErrorCode, ReplayService
from agentbodycam.core.ui_assets import UiAsset, UiBundle
from agentbodycam.models.events import JsonObject
from agentbodycam.models.replay import PRODUCT_NAME, REPLAY_API_VERSION, health
from agentbodycam.models.session import SESSION_ID_PATTERN

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0  # the operating system picks a free ephemeral port
ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost")
ALLOWED_METHODS = "GET, HEAD"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_QUERY_INTEGER_DIGITS = 18
MAX_DISCARDED_BODY_BYTES = 64 * 1024
API_PREFIX = "/api/"
UI_BOOTSTRAP_PATH = "/ui/bootstrap"
UI_BOOTSTRAP_HEADER = "X-AgentBodycam-UI"
UI_BOOTSTRAP_HEADER_VALUE = "bootstrap"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_UI_SESSION_ROUTE = re.compile(r"^/session/([^/]+)$")

_LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}
_INTEGER = re.compile(rf"^-?[0-9]{{1,{MAX_QUERY_INTEGER_DIGITS}}}$")
_PARAM_NAME = re.compile(r"^[a-z_]{1,32}$")
_LOGGED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Referrer-Policy", "no-referrer"),
)

# Only the UI document runs anything: same-origin script, style, image, font and fetch;
# no inline script or style markup, no eval, no plugins, no forms, no framing, no <base>.
UI_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_UI_DOCUMENT_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", UI_CONTENT_SECURITY_POLICY),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=()"),
)

_STATUS_BY_CODE = {
    ReplayErrorCode.INVALID_SESSION_ID: 400,
    ReplayErrorCode.INVALID_PARAMETER: 400,
    ReplayErrorCode.SESSION_NOT_FOUND: 404,
    ReplayErrorCode.SESSION_UNREADABLE: 500,
    ReplayErrorCode.STORE_UNSAFE: 500,
}

# Responses to malformed HTTP that BaseHTTPRequestHandler reports through send_error().
_PROTOCOL_ERRORS = {
    400: ("bad_request", "Malformed HTTP request."),
    404: ("not_found", "No such route."),
    405: ("method_not_allowed", "This API is read-only: only GET and HEAD are allowed."),
    408: ("request_timeout", "Request timed out."),
    414: ("request_uri_too_long", "Request URI too long."),
    431: ("request_header_fields_too_large", "Request headers too large."),
    500: ("internal_error", "Unexpected server error."),
    505: ("http_version_not_supported", "HTTP version not supported."),
}


class BindAddressError(ValueError):
    """The requested listening address is not a loopback address."""


def resolve_bind_host(host: str) -> str:
    """The loopback address to bind for ``host``; raises :class:`BindAddressError` otherwise.

    Only ``127.0.0.1``, ``::1`` and ``localhost`` (bound as ``127.0.0.1`` without a DNS
    lookup) are accepted. ``0.0.0.0``, ``::``, LAN addresses and other names are refused so
    the API can never be exposed to the network by accident.
    """
    key = host.strip().lower()
    if key.startswith("[") and key.endswith("]"):
        key = key[1:-1]
    address = _LOOPBACK.get(key)
    if address is None:
        raise BindAddressError(
            f"refusing to listen on {host!r}: the Replay API only binds to loopback "
            f"addresses ({', '.join(ALLOWED_HOSTS)})"
        )
    return address


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    body: JsonObject
    headers: tuple[tuple[str, str], ...] = ()
    route: str = "(unmatched)"
    # A UI document or asset instead of JSON: its bytes, type and security headers.
    raw: UiAsset | None = None
    security: tuple[tuple[str, str], ...] = _SECURITY_HEADERS


def _ui_response(asset: UiAsset, route: str, *, document: bool) -> _Response:
    security = _UI_DOCUMENT_HEADERS if document else _SECURITY_HEADERS
    return _Response(200, {}, route=route, raw=asset, security=security)


def _error(
    status: int, code: str, message: str, *, route: str = "(unmatched)", **headers: str
) -> _Response:
    extra = tuple((name.replace("_", "-"), value) for name, value in headers.items())
    return _Response(status, {"error": {"code": code, "message": message}}, extra, route)


def _protocol_error(status: int, **headers: str) -> _Response:
    code, message = _PROTOCOL_ERRORS.get(status, ("error", "Request failed."))
    return _error(status, code, message, **headers)


def _replay_error(exc: ReplayError, route: str) -> _Response:
    return _error(_STATUS_BY_CODE[exc.code], exc.code.value, exc.message, route=route)


class _QueryError(ValueError):
    pass


def _parse_query(query: str | None, allowed: tuple[str, ...]) -> dict[str, int]:
    """Strictly parse ``name=integer`` pairs; nothing is percent-decoded."""
    if query is None or query == "":
        return {}
    if not allowed:
        raise _QueryError("This route takes no query parameters.")
    values: dict[str, int] = {}
    for part in query.split("&"):
        name, sep, raw = part.partition("=")
        if not sep or not _PARAM_NAME.fullmatch(name) or name not in allowed:
            raise _QueryError(f"Unexpected query parameter; allowed: {', '.join(allowed)}.")
        if name in values:
            raise _QueryError(f"Duplicate query parameter: {name}.")
        if not _INTEGER.fullmatch(raw):
            raise _QueryError(f"{name} must be an integer.")
        values[name] = int(raw)
    return values


def _json_bytes(body: JsonObject) -> bytes:
    """Deterministic JSON, identical in layout to ``agentbodycam analyze --json``."""
    return (json.dumps(body, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


@dataclass(slots=True)
class _Config:
    service: ReplayService
    token: bytes
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    log: Callable[[str], None] | None
    ui: UiBundle | None = None
    log_lock: threading.Lock = field(default_factory=threading.Lock)


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close() joins every request thread
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16
    config: _Config

    def server_bind(self) -> None:
        if sys.platform == "win32":
            # Stop any other socket from binding the same port while we listen.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind() would call socket.getfqdn() (a DNS lookup); skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: object, client_address: object) -> None:
        """Never print a traceback (it could contain request data); note the failure only."""
        log = self.config.log
        if log is not None:
            with self.config.log_lock:
                log("request failed: connection error")


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server_version = PRODUCT_NAME
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS
    # Never answer in header-less HTTP/0.9 form, even to a malformed request line.
    default_request_version = "HTTP/1.0"

    @property
    def _config(self) -> _Config:
        return cast(_Server, self.server).config

    def version_string(self) -> str:
        return PRODUCT_NAME

    # -- logging: the default access log would print raw paths and query strings ----------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _log(self, response: _Response) -> None:
        log = self._config.log
        if log is None:
            return
        method = self.command if self.command in _LOGGED_METHODS else "OTHER"
        with self._config.log_lock:
            log(f"{method} {response.route} {response.status}")

    # -- output --------------------------------------------------------------------------

    def _send(self, response: _Response) -> None:
        if response.raw is None:
            body, content_type = _json_bytes(response.body), JSON_CONTENT_TYPE
        else:
            body, content_type = response.raw.body, response.raw.content_type
        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in response.security + response.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(response)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """JSON instead of the default HTML page; the client's input is never echoed."""
        status = 405 if code in (405, 501) else int(code)
        self.close_connection = True
        headers = {"Allow": ALLOWED_METHODS} if status == 405 else {}
        if status == 405 and getattr(self, "headers", None) is not None:
            self._discard_small_body()
        self._send(_protocol_error(status, **headers))

    # -- methods -------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._discard_small_body()
        self._send(_protocol_error(405, Allow=ALLOWED_METHODS))

    def _discard_small_body(self) -> None:
        """Drain (never parse) a small request body so closing does not reset the socket."""
        length = (self.headers.get("Content-Length") or "").strip()
        if length.isdigit() and int(length) <= MAX_DISCARDED_BODY_BYTES:
            self.rfile.read(int(length))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = (
        _method_not_allowed
    )

    def _serve(self) -> None:
        try:
            response = self._dispatch()
        except Exception:
            response = _protocol_error(500)
        self._send(response)

    # -- request checks ------------------------------------------------------------------

    def _browser_check(self) -> _Response | None:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self._config.allowed_hosts:
            return _error(
                403, "host_not_allowed", "The Host header must name this loopback server."
            )
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1 or (origins and origins[0] not in self._config.allowed_origins):
            return _error(403, "origin_not_allowed", "Cross-origin requests are not allowed.")
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return _error(403, "origin_not_allowed", "Cross-origin requests are not allowed.")
        return None

    def _authorized(self) -> bool:
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1:
            return False
        scheme, _, credential = values[0].strip().partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return False
        supplied = credential.strip().encode("latin-1", "replace")
        return secrets.compare_digest(supplied, self._config.token)

    # -- routing -------------------------------------------------------------------------

    def _dispatch(self) -> _Response:
        rejected = self._browser_check()
        if rejected is not None:
            return rejected
        path, has_query, query = self.path.partition("?")
        query_text = query if has_query else None
        if not path.startswith("/"):
            return _protocol_error(400)
        segments = path.split("/")[1:]

        if segments == ["api", "v1", "health"]:
            route = "/api/v1/health"
            try:
                _parse_query(query_text, ())
            except _QueryError as exc:
                return _error(400, "invalid_parameter", str(exc), route=route)
            return _Response(200, health(), route=route)

        if not path.startswith(API_PREFIX):
            ui = self._config.ui
            return _protocol_error(404) if ui is None else self._ui_route(ui, path, query_text)
        if not self._authorized():
            return _error(
                401,
                "unauthorized",
                "Missing or invalid bearer token.",
                WWW_Authenticate='Bearer realm="agentbodycam"',
            )
        return self._route(segments, query_text)

    def _route(self, segments: list[str], query: str | None) -> _Response:
        service = self._config.service
        if segments[:2] != ["api", "v1"]:
            return _protocol_error(404)
        rest = segments[2:]
        allowed: tuple[str, ...]
        if rest == ["project"]:
            route, allowed = "/api/v1/project", ()
        elif rest == ["sessions"]:
            route, allowed = "/api/v1/sessions", ()
        elif len(rest) == 2 and rest[0] == "sessions":
            route, allowed = "/api/v1/sessions/{session_id}", ()
        elif len(rest) == 3 and rest[0] == "sessions" and rest[2] == "report":
            route, allowed = "/api/v1/sessions/{session_id}/report", ()
        elif len(rest) == 3 and rest[0] == "sessions" and rest[2] == "timeline":
            route, allowed = (
                "/api/v1/sessions/{session_id}/timeline",
                ("after_sequence", "limit"),
            )
        else:
            return _protocol_error(404)
        try:
            params = _parse_query(query, allowed)
        except _QueryError as exc:
            return _error(400, "invalid_parameter", str(exc), route=route)
        try:
            if rest == ["project"]:
                return _Response(200, service.project().to_dict(), route=route)
            if rest == ["sessions"]:
                return _Response(200, service.list_sessions().to_dict(), route=route)
            session_id = rest[1]  # raw, never decoded; the service validates it strictly
            if len(rest) == 2:
                return _Response(200, service.session_detail(session_id).to_dict(), route=route)
            if rest[2] == "report":
                return _Response(200, service.report_dict(session_id), route=route)
            page = service.timeline(session_id, **params)
            return _Response(200, page.to_dict(), route=route)
        except ReplayError as exc:
            return _replay_error(exc, route)

    # -- Replay UI -----------------------------------------------------------------------

    def _ui_route(self, ui: UiBundle, path: str, query: str | None) -> _Response:
        """UI document routes, the token bootstrap and known assets; nothing else."""
        if path == UI_BOOTSTRAP_PATH:
            return self._ui_bootstrap(query)
        if path == "/":
            route = "/"
        elif (match := _UI_SESSION_ROUTE.fullmatch(path)) and SESSION_ID_PATTERN.fullmatch(
            match.group(1)
        ):
            route = "/session/{session_id}"
        else:
            asset = ui.asset(path)
            if asset is None:
                return _protocol_error(404)
            if query is not None:
                return _error(400, "invalid_parameter", "UI files take no query parameters.")
            return _ui_response(asset, "(ui asset)", document=False)
        if query is not None:
            return _error(400, "invalid_parameter", "UI pages take no query parameters.")
        return _ui_response(ui.index, route, document=True)

    def _ui_bootstrap(self, query: str | None) -> _Response:
        """This instance's token, for a same-origin ``fetch()`` by the UI page only."""
        route = UI_BOOTSTRAP_PATH
        markers = self.headers.get_all(UI_BOOTSTRAP_HEADER) or []
        site = self.headers.get_all("Sec-Fetch-Site") or []
        mode = self.headers.get_all("Sec-Fetch-Mode") or []
        dest = self.headers.get_all("Sec-Fetch-Dest") or []
        if (
            query is not None
            or markers != [UI_BOOTSTRAP_HEADER_VALUE]
            or site != ["same-origin"]
            or mode not in (["cors"], ["same-origin"])
            or dest != ["empty"]
        ):
            return _error(
                403,
                "bootstrap_not_allowed",
                "UI access is only granted to the Replay UI page served by this server.",
                route=route,
            )
        token = self._config.token.decode("ascii")
        return _Response(200, {"api_version": REPLAY_API_VERSION, "token": token}, route=route)


class ReplayServer:
    """The Replay API on a loopback address. Foreground use: ``start()`` then ``stop()``.

    ``token`` is generated here, per instance, and exists only in this object's memory.
    With ``ui`` (the packaged UI build), the Replay UI is served from the same origin and
    the UI page can obtain the token through ``/ui/bootstrap``; without it (the default),
    no route outside ``/api/`` exists.
    """

    def __init__(
        self,
        service: ReplayService,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        log: Callable[[str], None] | None = None,
        ui: UiBundle | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"port must be between 0 and 65535, got {port}")
        self.host = resolve_bind_host(host)
        self.token = secrets.token_urlsafe(32)
        server_class = _Server6 if ":" in self.host else _Server
        self._httpd = server_class((self.host, port), _Handler)
        self.port = int(self._httpd.server_address[1])
        names = ("[::1]" if self.host == "::1" else "127.0.0.1", "localhost")
        self._httpd.config = _Config(
            service=service,
            token=self.token.encode("ascii"),
            allowed_hosts=frozenset(f"{name}:{self.port}" for name in names),
            allowed_origins=frozenset(f"http://{name}:{self.port}" for name in names),
            log=log,
            ui=ui,
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def bound_address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> threading.Thread:
        """Serve in a background (non-daemon) thread; :meth:`stop` ends it."""
        if self._thread is not None:
            raise RuntimeError("the Replay API server is already running")
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="agentbodycam-replay-api",
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        """Stop serving, wait for in-flight requests and close the socket. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join()
        self._httpd.server_close()

    def __enter__(self) -> ReplayServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
