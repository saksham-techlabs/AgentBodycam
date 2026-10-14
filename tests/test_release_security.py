"""Release hardening regressions: hostile command lines end to end, Replay server edge cases,
repository hygiene and documentation that matches what is implemented."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentbodycam.cli import main
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.redact import EXTERNAL_PATH, REDACTED
from agentbodycam.core.replay_server import ReplayServer
from agentbodycam.core.store import EventStore
from agentbodycam.core.ui_assets import UiBundle, load_ui_bundle
from agentbodycam.models.events import EventType
from command_helpers import command_events, py, run_cli
from forensics_helpers import SessionBuilder
from replay_helpers import ApiClient, raw_request, serve

ROOT = Path(__file__).resolve().parent.parent
DUMP_ARGV = (
    "import json,sys; open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(sys.argv[2:]))"
)
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVlLTg3"
RANDOM_TOKEN = "Rnd8" + "aZ9x" * 8


# -- hostile command lines -------------------------------------------------------------------


@pytest.fixture
def live(project: Path) -> Iterator[Recorder]:
    recorder = Recorder(EventStore(project), record_git=False)
    recorder.start()
    recorder.take_baseline()
    yield recorder
    if recorder.is_active:
        recorder.stop()


def test_hostile_command_line_is_redacted_end_to_end(
    live: Recorder, project: Path, tmp_path: Path
) -> None:
    home = Path.home()
    secrets = {
        "flag": "T0K3N-flag-value-81",
        "inline": "AK-inline-value-82",
        "password": "PW-value-83",
        "assign": "assign-value-84",
        "url": "URLPASS85",
        "query": "QS-value-86",
        "jwt": JWT,
        "random": RANDOM_TOKEN,
        "windows-user": "alice88",
        "posix-user": "alice89",
        "external": "outside-90",
        "home": "notes-91",
    }
    args = [
        "--token",
        secrets["flag"],
        f"--api-key={secrets['inline']}",
        "--password",
        secrets["password"],
        f"DB_PASSWORD={secrets['assign']}",
        f"https://bob:{secrets['url']}@example.com/repo.git",
        f"https://example.com/cb?access_token={secrets['query']}&x=1",
        secrets["jwt"],
        secrets["random"],
        rf"C:\Users\{secrets['windows-user']}\secret.txt",
        f"/home/{secrets['posix-user']}/.ssh/id_rsa",
        str(tmp_path / secrets["external"] / "x.txt"),
        str(home / f"{secrets['home']}.txt"),
    ]
    out = tmp_path / "argv.json"
    code, err = run_cli("run", "--project", str(project), "--", *py(DUMP_ARGV, str(out), *args))
    assert code == 0, err
    assert json.loads(out.read_text(encoding="utf-8")) == args  # the command got the original
    live.stop()

    [started, _finished] = command_events(live.store, live.session.session_id)
    recorded = started.payload["argv"]
    assert isinstance(recorded, list)
    assert recorded[-len(args) :] == [
        "--token",
        REDACTED,
        f"--api-key={REDACTED}",
        "--password",
        REDACTED,
        f"DB_PASSWORD={REDACTED}",
        f"https://{REDACTED}@example.com/repo.git",
        f"https://example.com/cb?access_token={REDACTED}&x=1",
        REDACTED,
        REDACTED,
        EXTERNAL_PATH,
        EXTERNAL_PATH,
        EXTERNAL_PATH,
        EXTERNAL_PATH,
    ]

    stored = b"".join(p.read_bytes() for p in (project / ".agentbodycam").rglob("*") if p.is_file())
    server = serve(project)
    try:
        client = ApiClient(server)
        base = f"/api/v1/sessions/{live.session.session_id}"
        bodies = [
            client.get(path).body.decode("utf-8")
            for path in ("/api/v1/sessions", base, f"{base}/report", f"{base}/timeline?limit=500")
        ]
    finally:
        server.stop()
    for value in secrets.values():
        assert value.encode() not in stored, value
        for body in bodies:
            assert value not in body, value
    for body in bodies:
        assert str(home) not in body and json.dumps(str(home))[1:-1] not in body
        assert str(project.resolve()) not in body


def test_no_command_event_is_appended_after_stop(live: Recorder, project: Path) -> None:
    live.stop()
    code, err = run_cli("run", "--project", str(project), "--", *py("pass"))
    assert code == 125 and "no active AgentBodycam recording" in err
    events = live.store.read_events(live.session.session_id)
    assert events[-1].type is EventType.SESSION_STOPPED
    assert command_events(live.store, live.session.session_id) == []


# -- Replay server edge cases ----------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle() -> UiBundle:
    return load_ui_bundle()


@pytest.fixture
def ui_server(store: EventStore, bundle: UiBundle) -> Iterator[ReplayServer]:
    SessionBuilder(store).stop()
    (store.project_root / ".env").write_text("SECRET_MARKER_31f=1\n", encoding="utf-8")
    server = serve(store.project_root, ui=bundle)
    try:
        yield server
    finally:
        server.stop()


def _raw(server: ReplayServer, target: str, *headers: str) -> bytes:
    lines = [f"GET {target} HTTP/1.1", f"Host: 127.0.0.1:{server.port}", *headers]
    return ("\r\n".join([*lines, "Connection: close", "", ""])).encode()


def test_duplicate_fetch_metadata_is_refused(ui_server: ReplayServer) -> None:
    auth = f"Authorization: Bearer {ui_server.token}"
    for order in (("same-origin", "cross-site"), ("same-origin", "same-origin")):
        sites = [f"Sec-Fetch-Site: {site}" for site in order]
        reply = raw_request(ui_server.port, _raw(ui_server, "/api/v1/sessions", auth, *sites))
        assert reply.status == 403 and reply.error_code == "origin_not_allowed"
    reply = raw_request(ui_server.port, _raw(ui_server, "/api/v1/sessions", auth))
    assert reply.status == 200


def test_a_restarted_server_rejects_the_previous_token(store: EventStore) -> None:
    SessionBuilder(store).stop()
    first = serve(store.project_root)
    old_token = first.token
    assert ApiClient(first).get("/api/v1/sessions").status == 200
    first.stop()
    second = serve(store.project_root)
    try:
        assert second.token != old_token
        reply = ApiClient(second).get("/api/v1/sessions", token=old_token)
        assert reply.status == 401 and reply.error_code == "unauthorized"
        assert ApiClient(second).get("/api/v1/sessions").status == 200
    finally:
        second.stop()


def test_every_response_role_has_its_security_headers(ui_server: ReplayServer) -> None:
    client = ApiClient(ui_server)
    bootstrap = client.get(
        "/ui/bootstrap",
        token=None,
        headers={
            "X-AgentBodycam-UI": "bootstrap",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
    )
    assert bootstrap.status == 200
    sid = client.get("/api/v1/sessions").json["sessions"][0]["session_id"]
    roles = {
        "api": client.get(f"/api/v1/sessions/{sid}/report"),
        "api error": client.get("/api/v1/sessions", token=None),
        "bootstrap": bootstrap,
        "asset": client.get(next(iter(load_ui_bundle().assets)), token=None),
        "document": client.get("/", token=None),
    }
    for role, reply in roles.items():
        assert reply.header("Cache-Control") == "no-store", role
        assert reply.header("X-Content-Type-Options") == "nosniff", role
        assert reply.header("Referrer-Policy") == "no-referrer", role
        assert reply.header("X-Frame-Options") == "DENY", role
        assert reply.header("Cross-Origin-Resource-Policy") == "same-origin", role
        csp = reply.header("Content-Security-Policy") or ""
        assert "frame-ancestors 'none'" in csp, role
        assert not any(name.startswith("access-control-") for name in reply.header_names())
        assert "set-cookie" not in reply.header_names(), role
    for role in ("api", "api error", "bootstrap", "asset"):
        assert roles[role].header("Content-Security-Policy") == (
            "default-src 'none'; frame-ancestors 'none'; sandbox"
        ), role
    document = roles["document"]
    assert document.header("Cross-Origin-Opener-Policy") == "same-origin"
    assert "camera=()" in (document.header("Permissions-Policy") or "")
    assert "microphone=()" in (document.header("Permissions-Policy") or "")


MORE_TRAVERSALS = [
    "/%252e%252e/%252e%252e/secret.txt",
    "/assets/%252e%252e%252fcli.py",
    "/%c0%ae%c0%ae/%c0%ae%c0%ae/secret.txt",
    "/assets/..;/..;/cli.py",
    "/.env",
    "/.git/config",
    "/.agentbodycam",
    "/.agentbodycam/",
    "/%2eagentbodycam/sessions",
    "/assets/{asset}.map",
    "/{asset}",
    "/assets/{asset};.html",
    "/assets/{asset}%20",
    "/assets/{asset}.",
    "/assets/{upper}",
    "/favicon.svg.map",
    "/index.html.map",
    "/assets/../../../../windows/win.ini",
    "/\\\\server\\share\\x",
    "http://127.0.0.1/assets/{asset}",
    "*",
]


@pytest.mark.parametrize("target", MORE_TRAVERSALS)
def test_more_static_path_tricks_are_never_served(
    ui_server: ReplayServer, bundle: UiBundle, target: str
) -> None:
    asset = next(p for p in bundle.assets if p.startswith("/assets/")).split("/")[-1]
    request_target = target.format(asset=asset, upper=asset.upper())
    auth = f"Authorization: Bearer {ui_server.token}"
    reply = raw_request(ui_server.port, _raw(ui_server, request_target, auth))
    assert reply.status in (400, 404), request_target
    assert set(reply.json) == {"error"}
    assert reply.header("Content-Type") == "application/json; charset=utf-8"
    for marker in ("SECRET_MARKER_31f", "session_id", "import ", "<html", "[core]"):
        assert marker not in reply.text(), (request_target, marker)


def test_only_allowlisted_build_files_are_servable(bundle: UiBundle) -> None:
    for path, asset in bundle.assets.items():
        name = path.rsplit("/", 1)[-1]
        assert not name.startswith("."), path
        assert not path.endswith((".map", ".html", ".py", ".json", ".txt")), path
        assert b"sourceMappingURL" not in asset.body, path
    assert b"sourceMappingURL" not in bundle.index.body


# -- repository hygiene ----------------------------------------------------------------------


def _tracked_files() -> list[str]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a Git checkout")
    result = subprocess.run(  # noqa: S603 - fixed arguments, read-only
        [git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("git ls-files unavailable")
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


ARTIFACT_PARTS = {
    ".agentbodycam",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    ".idea",
    ".vscode",
    "dist",
}
ARTIFACT_SUFFIXES = (
    ".log",
    ".map",
    ".pyc",
    ".webm",
    ".mp4",
    ".gif",
    ".png",
    ".jpg",
    ".jpeg",
    ".zip",
    ".whl",
    ".tsbuildinfo",
    ".swp",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".db",
    ".har",
)
ARTIFACT_NAMES = {
    "events.jsonl",
    "session.json",
    "active.json",
    "active.lock",
    ".env",
    ".envrc",
    ".DS_Store",
    "Thumbs.db",
    ".coverage",
    "agentbodycam-replay.svg",
    "agentbodycam-replay.webm",
    "agentbodycam-summary.json",
}
TOKEN_SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}"
    r"|\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}|\bAIza[0-9A-Za-z_\-]{35}|\bnpm_[A-Za-z0-9]{36}"
    r"|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"
)
# Redaction tests must spell out the secrets they redact.
TOKEN_FIXTURE_FILES = {"tests/test_redact.py", "tests/test_release_security.py"}


def test_repository_tracks_no_runtime_or_session_artifacts() -> None:
    offenders = []
    for name in _tracked_files():
        parts = name.split("/")
        base = parts[-1]
        if (
            ARTIFACT_PARTS.intersection(parts[:-1])
            or base in ARTIFACT_NAMES
            or base.startswith(".env.")
            or base.lower().endswith(ARTIFACT_SUFFIXES)
        ):
            offenders.append(name)
    assert offenders == []


def test_repository_contains_no_credentials_or_personal_paths() -> None:
    home = Path.home()
    personal = (
        [str(home).lower(), home.as_posix().lower()] if len(home.parts) >= 3 else []
    )  # e.g. C:\Users\<name> or /home/<name>
    offenders = []
    for name in _tracked_files():
        if not (ROOT / name).is_file():
            continue  # deleted in the working tree, e.g. a replaced build asset
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        if name not in TOKEN_FIXTURE_FILES and TOKEN_SHAPES.search(text):
            offenders.append(f"{name}: token-shaped value")
        lowered = text.lower()
        if any(path in lowered for path in personal):
            offenders.append(f"{name}: personal path")
    assert offenders == []


def test_gitignore_keeps_local_data_out_of_git() -> None:
    entries = set((ROOT / ".gitignore").read_text(encoding="utf-8").split())
    required = {".agentbodycam/", "node_modules/", ".venv/", "__pycache__/", ".env", ".env.*"}
    required |= {"*.log", "*.map", "*.webm", "*.pem", "*.key"}
    assert required <= entries, sorted(required - entries)


# -- documentation and help match what exists ------------------------------------------------

DOCS = ["README.md", "SECURITY.md", "CHANGELOG.md", "docs/architecture.md", "docs/product-scope.md"]
NEGATION = re.compile(r"\b(no|not|never|nothing|without|isn't|doesn't)\b", re.IGNORECASE)
CAUSAL = re.compile(
    r"root cause|culprit|\bcaused\b|this (edit|change) (fixed|broke)|agent fixed|responsible for",
    re.IGNORECASE,
)
FAKE_VIDEO = re.compile(
    r"screen (recording|capture|video)|footage|desktop video|recording of (cursor|codex|claude)",
    re.IGNORECASE,
)


def _help_texts(capsys: pytest.CaptureFixture[str]) -> str:
    texts = []
    for command in ([], ["record"], ["run"], ["analyze"], ["replay"], ["replay-api"], ["session"]):
        with pytest.raises(SystemExit):
            main([*command, "--help"])
        texts.append(capsys.readouterr().out)
    return "\n".join(texts)


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?:;])\s+", flat)


def test_docs_and_help_never_claim_causes_or_screen_video(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sources = {name: (ROOT / name).read_text(encoding="utf-8") for name in DOCS}
    sources["cli --help"] = _help_texts(capsys)
    offenders = []
    for name, text in sources.items():
        for sentence in _sentences(text):
            if FAKE_VIDEO.search(sentence) and not NEGATION.search(sentence):
                offenders.append(f"{name}: {sentence}")
            # The design docs' fact-vs-inference examples quote what must never be claimed.
            if (
                CAUSAL.search(sentence)
                and not NEGATION.search(sentence)
                and "inference" not in sentence.lower()
            ):
                offenders.append(f"{name}: {sentence}")
    assert offenders == []


def test_docs_state_the_current_feature_set() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    not_implemented = readme.split("> Still not implemented:", 1)[1].split("\n\n", 1)[0]
    for missing in ("GIF export", "MP4 export", "hosted", "file contents and diffs", "adapters"):
        assert missing in not_implemented, missing
    assert "AI analyst" in not_implemented
    implemented = readme.split("> Implemented:", 1)[1].split("> Still not implemented:", 1)[0]
    for present in ("SVG", "JSON", "WebM", "Replay UI", "Replay API", "forensic", "Git"):
        assert present in implemented, present
    for name in ("README.md", "SECURITY.md", "docs/architecture.md", "docs/product-scope.md"):
        raw = (ROOT / name).read_text(encoding="utf-8").lower()
        text = re.sub(r"\s+", " ", re.sub(r"(?m)^>\s?", "", raw))
        assert "not ready for production" in text or "not production-ready" in text, name
    scope = (ROOT / "docs/product-scope.md").read_text(encoding="utf-8")
    current = scope.split("## Current state", 1)[1].split("\n## ", 1)[0]
    assert "everything else" not in current
    for present in ("SVG", "JSON", "WebM", "Replay API", "Replay UI", "analyze", "record"):
        assert present in current, present
    assert "no agent-specific adapter exists" in scope
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    assert "Most of what is described here is not implemented" not in architecture


def test_cli_help_matches_the_current_feature_set(capsys: pytest.CaptureFixture[str]) -> None:
    text = _help_texts(capsys)
    assert "no recording yet" not in text
    assert "no command or agent capture" not in text
    assert "GIF/MP4 export and hosted sharing are not built" in text
    assert "WebM" in text and "SVG" in text and "JSON" in text
