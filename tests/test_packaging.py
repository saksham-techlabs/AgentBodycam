"""Release packaging: the wheel and sdist contain what they must and nothing else, builds are
reproducible, and a wheel installed into a fresh virtual environment runs (CLI, Replay UI)
without the source checkout, Node.js or network access."""

from __future__ import annotations

import contextlib
import email
import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import agentbodycam

hatchling_build = pytest.importorskip("hatchling.build")

ROOT = Path(__file__).resolve().parent.parent
SOURCE_PACKAGE = ROOT / "src" / "agentbodycam"
VERSION = agentbodycam.__version__
DIST_INFO = f"agentbodycam-{VERSION}.dist-info"
SDIST_PREFIX = f"agentbodycam-{VERSION}/"
LISTENING = re.compile(r"^Listening: http://127\.0\.0\.1:(\d+)$")
SESSION_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{16}")

WHEEL_PACKAGE_SUFFIXES = {".py", ".typed", ".html", ".js", ".css", ".svg"}
WHEEL_DIST_INFO = {
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "RECORD",
    "licenses/LICENSE",
    "licenses/THIRD_PARTY_NOTICES.md",
}
SDIST_TOP_LEVEL = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs",
    "examples",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
    "web",
}
SDIST_REQUIRED = {
    "pyproject.toml",
    "PKG-INFO",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "src/agentbodycam/__init__.py",
    "src/agentbodycam/py.typed",
    "src/agentbodycam/ui/static/index.html",
    "tests/conftest.py",
    "scripts/check.py",
    "web/package.json",
    "web/package-lock.json",
    "web/vite.config.ts",
    "web/index.html",
    "web/src/main.tsx",
}
THIRD_PARTY_NOTICE_MARKERS = (
    b"react",
    b"react-dom",
    b"scheduler",
    b"Copyright (c) Meta Platforms, Inc. and affiliates.",
    b"MIT License",
)
FORBIDDEN_PARTS = {
    ".git",
    ".agentbodycam",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    "htmlcov",
    ".vite",
    ".idea",
    ".vscode",
}
FORBIDDEN_SUFFIXES = (
    ".map",
    ".pyc",
    ".log",
    ".webm",
    ".mp4",
    ".gif",
    ".png",
    ".jpg",
    ".jpeg",
    ".zip",
    ".whl",
    ".tar.gz",
    ".tsbuildinfo",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".har",
    ".egg-info",
)
FORBIDDEN_NAMES = {
    ".env",
    ".envrc",
    "events.jsonl",
    "session.json",
    "active.json",
    "active.lock",
    "agentbodycam-replay.svg",
    "agentbodycam-replay.webm",
    "agentbodycam-summary.json",
    ".DS_Store",
    "Thumbs.db",
}


@dataclass(frozen=True)
class Artifacts:
    wheel: Path
    sdist: Path


def _build(directory: Path) -> Artifacts:
    directory.mkdir(parents=True)
    with contextlib.chdir(ROOT):
        sdist = hatchling_build.build_sdist(str(directory))
        wheel = hatchling_build.build_wheel(str(directory))
    return Artifacts(wheel=directory / wheel, sdist=directory / sdist)


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> Artifacts:
    """A fresh build, or with AGENTBODYCAM_TEST_DIST=<dir> the exact files CI uploads."""
    prebuilt = os.environ.get("AGENTBODYCAM_TEST_DIST")
    if not prebuilt:
        return _build(tmp_path_factory.mktemp("dist") / "first")
    directory = Path(prebuilt).resolve()
    found = {path.name for path in directory.iterdir()}
    artifacts = Artifacts(
        wheel=directory / f"agentbodycam-{VERSION}-py3-none-any.whl",
        sdist=directory / f"agentbodycam-{VERSION}.tar.gz",
    )
    assert found == {artifacts.wheel.name, artifacts.sdist.name}, sorted(found)
    return artifacts


def _wheel_files(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()}


def _sdist_files(sdist: Path) -> dict[str, bytes]:
    files = {}
    with tarfile.open(sdist) as tf:
        for member in tf.getmembers():
            assert member.isfile() or member.isdir(), member.name  # no links or devices
            if member.isfile():
                extracted = tf.extractfile(member)
                assert extracted is not None
                files[member.name] = extracted.read()
    return files


def _forbidden(name: str) -> bool:
    parts = name.split("/")
    base = parts[-1]
    return bool(
        FORBIDDEN_PARTS.intersection(parts[:-1])
        or base in FORBIDDEN_NAMES
        or base.startswith(".env.")
        or base.lower().endswith(FORBIDDEN_SUFFIXES)
    )


def _local_paths() -> list[bytes]:
    paths = [str(ROOT), ROOT.as_posix()]
    home = Path.home()
    if len(home.parts) >= 3:  # e.g. C:\Users\<name> or /home/<name>
        paths += [str(home), home.as_posix()]
    return [path.encode() for path in paths]


# -- metadata and version --------------------------------------------------------------------


def test_metadata_is_consistent(artifacts: Artifacts) -> None:
    files = _wheel_files(artifacts.wheel)
    metadata = email.message_from_bytes(files[f"{DIST_INFO}/METADATA"])
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["Name"] == project["name"] == "agentbodycam"
    assert metadata["Version"] == VERSION
    assert metadata["Requires-Python"] == project["requires-python"] == ">=3.11"
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("License-File") == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    assert files[f"{DIST_INFO}/licenses/LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert (
        files[f"{DIST_INFO}/licenses/THIRD_PARTY_NOTICES.md"]
        == (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes()
    )
    assert metadata["Description-Content-Type"] == "text/markdown"
    description = files[f"{DIST_INFO}/METADATA"].split(b"\n\n", 1)[1].decode("utf-8")
    assert description == (ROOT / "README.md").read_text(encoding="utf-8")
    classifiers = metadata.get_all("Classifier") or []
    assert "Development Status :: 2 - Pre-Alpha" in classifiers
    assert not any("Production/Stable" in c or "Mature" in c for c in classifiers)
    # Zero runtime dependencies: every Requires-Dist belongs to the `dev` extra.
    assert project["dependencies"] == []
    assert all("extra == 'dev'" in req for req in metadata.get_all("Requires-Dist") or [])
    entry_points = files[f"{DIST_INFO}/entry_points.txt"].decode()
    assert "[console_scripts]\nagentbodycam = agentbodycam.cli:main" in entry_points
    assert artifacts.wheel.name == f"agentbodycam-{VERSION}-py3-none-any.whl"
    assert artifacts.sdist.name == f"agentbodycam-{VERSION}.tar.gz"
    pkg_info = _sdist_files(artifacts.sdist)[f"{SDIST_PREFIX}PKG-INFO"]
    assert email.message_from_bytes(pkg_info)["Version"] == VERSION


def test_web_ui_version_matches_the_python_package() -> None:
    """The two versions are defined separately; they must not drift apart."""
    web = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))
    assert web["version"] == lock["version"] == lock["packages"][""]["version"] == VERSION


def _assert_third_party_notices(body: bytes) -> None:
    """Bundled Replay UI runtime attribution must name React, ReactDOM and Scheduler."""
    lowered = body.lower()
    assert b"react" in lowered and b"react-dom" in lowered and b"scheduler" in lowered
    for marker in THIRD_PARTY_NOTICE_MARKERS:
        assert marker in body, marker
    # AgentBodycam stays MIT; do not claim the Meta packages are AgentBodycam's own license.
    assert b"AgentBodycam" in body or b"agentbodycam" in lowered
    assert b"Meta Platforms" in body


def test_third_party_notices_ship_in_wheel_and_sdist(artifacts: Artifacts) -> None:
    """ABD-005: bundled frontend runtime notices travel with both release artifacts."""
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes()
    _assert_third_party_notices(notices)
    wheel = _wheel_files(artifacts.wheel)
    assert wheel[f"{DIST_INFO}/licenses/THIRD_PARTY_NOTICES.md"] == notices
    sdist = _sdist_files(artifacts.sdist)
    assert sdist[f"{SDIST_PREFIX}THIRD_PARTY_NOTICES.md"] == notices
    metadata = email.message_from_bytes(wheel[f"{DIST_INFO}/METADATA"])
    assert metadata["License-Expression"] == "MIT"
    assert "THIRD_PARTY_NOTICES.md" in (metadata.get_all("License-File") or [])


def test_third_party_notices_survive_wheel_from_sdist(artifacts: Artifacts, tmp_path: Path) -> None:
    """A wheel rebuilt from the sdist must still carry the notices (no node_modules needed)."""
    with tarfile.open(artifacts.sdist) as tf:
        tf.extractall(tmp_path / "unpacked", filter="data")
    source = tmp_path / "unpacked" / SDIST_PREFIX.rstrip("/")
    assert (source / "THIRD_PARTY_NOTICES.md").is_file()
    assert not (source / "web" / "node_modules").exists()
    out = tmp_path / "out"
    out.mkdir()
    with contextlib.chdir(source):
        wheel = out / hatchling_build.build_wheel(str(out))
    rebuilt = _wheel_files(wheel)
    notices = rebuilt[f"{DIST_INFO}/licenses/THIRD_PARTY_NOTICES.md"]
    _assert_third_party_notices(notices)
    assert notices == (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes()


# -- wheel contents --------------------------------------------------------------------------


def test_wheel_contains_exactly_the_runtime_package(artifacts: Artifacts) -> None:
    names = set(_wheel_files(artifacts.wheel))
    package = {name for name in names if not name.startswith(f"{DIST_INFO}/")}
    assert {name.removeprefix(f"{DIST_INFO}/") for name in names - package} == WHEEL_DIST_INFO
    assert all(name.startswith("agentbodycam/") for name in package), sorted(package)
    source = {
        "agentbodycam/" + path.relative_to(SOURCE_PACKAGE).as_posix()
        for path in SOURCE_PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert package == source
    assert {
        "agentbodycam/__main__.py",
        "agentbodycam/cli.py",
        "agentbodycam/py.typed",
        "agentbodycam/ui/static/index.html",
        "agentbodycam/ui/static/favicon.svg",
    } <= package
    assert any(re.fullmatch(r"agentbodycam/ui/static/assets/index-[\w-]+\.js", n) for n in package)
    assert any(re.fullmatch(r"agentbodycam/ui/static/assets/[\w-]+\.css", n) for n in package)
    for name in package:
        suffix = name[name.rfind(".") :]
        assert suffix in WHEEL_PACKAGE_SUFFIXES, name
        assert not _forbidden(name), name


def test_wheel_ships_no_stale_ui_assets(artifacts: Artifacts) -> None:
    """Every packaged asset is referenced by the UI, so an old hashed bundle cannot ship."""
    files = _wheel_files(artifacts.wheel)
    static = "agentbodycam/ui/static/"
    assets = {name for name in files if name.startswith(f"{static}assets/")}
    assert assets
    for name in assets:
        filename = name.rsplit("/", 1)[-1].encode()
        referrers = [other for other in files if other.startswith(static) and other != name]
        assert any(filename in files[other] for other in referrers), f"unreferenced: {name}"


def test_artifacts_contain_no_local_paths_or_source_maps(artifacts: Artifacts) -> None:
    contents = {**_wheel_files(artifacts.wheel), **_sdist_files(artifacts.sdist)}
    for name, body in contents.items():
        for path in _local_paths():
            assert path not in body, name
        if name.endswith((".js", ".css", ".html")):
            assert b"sourceMappingURL" not in body, name


# -- sdist contents --------------------------------------------------------------------------


def test_sdist_contains_the_sources_and_nothing_local(artifacts: Artifacts) -> None:
    names = set(_sdist_files(artifacts.sdist))
    assert all(name.startswith(SDIST_PREFIX) for name in names), sorted(names)
    relative = {name.removeprefix(SDIST_PREFIX) for name in names}
    assert {name.split("/", 1)[0] for name in relative} == SDIST_TOP_LEVEL
    assert relative >= SDIST_REQUIRED, sorted(SDIST_REQUIRED - relative)
    assert not [name for name in relative if _forbidden(name)]
    web = {name.split("/", 2)[1] for name in relative if name.startswith("web/")}
    assert web <= {
        "src",
        "public",
        "index.html",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "tsconfig.node.json",
        "vite.config.ts",
    }


def test_sdist_contains_every_tracked_file(artifacts: Artifacts) -> None:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a Git checkout")
    result = subprocess.run(  # noqa: S603 - fixed arguments, read-only
        [git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("git ls-files unavailable")
    # GitHub configuration (workflows, templates) is not needed to build or test a release.
    tracked = {
        name
        for name in result.stdout.decode("utf-8").split("\0")
        if name and not name.startswith(".github/") and (ROOT / name).is_file()
    }
    relative = {name.removeprefix(SDIST_PREFIX) for name in _sdist_files(artifacts.sdist)}
    assert tracked - relative == set()


def test_sdist_builds_the_same_wheel(artifacts: Artifacts, tmp_path: Path) -> None:
    with tarfile.open(artifacts.sdist) as tf:
        tf.extractall(tmp_path / "unpacked", filter="data")
    source = tmp_path / "unpacked" / SDIST_PREFIX.rstrip("/")
    out = tmp_path / "out"
    out.mkdir()
    with contextlib.chdir(source):
        wheel = out / hatchling_build.build_wheel(str(out))
    assert wheel.read_bytes() == artifacts.wheel.read_bytes()


def test_builds_are_reproducible(artifacts: Artifacts, tmp_path: Path) -> None:
    again = _build(tmp_path / "second")
    assert again.wheel.read_bytes() == artifacts.wheel.read_bytes()
    assert again.sdist.read_bytes() == artifacts.sdist.read_bytes()


# -- installed wheel in a fresh virtual environment ------------------------------------------


@dataclass(frozen=True)
class Installed:
    python: Path
    cli: Path
    env: dict[str, str]
    work: Path

    def run(self, *args: str | Path, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed interpreter / installed CLI
            [str(arg) for arg in args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )


def _scrubbed_env(bin_dir: Path) -> dict[str, str]:
    """Only the venv's own executables on PATH: no Node, npm, Git or source checkout."""
    env = {"PATH": str(bin_dir), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


@pytest.fixture(scope="module")
def installed(artifacts: Artifacts, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    if importlib.util.find_spec("pip") is None:
        pytest.skip("pip is needed to install the wheel into a fresh environment")
    base = tmp_path_factory.mktemp("install") / "release test"
    venv.EnvBuilder(with_pip=False).create(base / "venv")
    bin_dir = base / "venv" / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    install = subprocess.run(  # noqa: S603 - fixed pip invocation, offline
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(python),
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            str(artifacts.wheel),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    work = base / "work dir"
    work.mkdir()
    cli = bin_dir / ("agentbodycam.exe" if os.name == "nt" else "agentbodycam")
    return Installed(python=python, cli=cli, env=_scrubbed_env(bin_dir), work=work)


PROBE = """
import json, sys, importlib.metadata as md
import agentbodycam, agentbodycam.cli
from agentbodycam.core.ui_assets import load_ui_bundle, packaged_ui_root
repo = sys.argv[1].lower()
loaded = [getattr(m, "__file__", None) or "" for m in list(sys.modules.values())]
bundle = load_ui_bundle()
print(json.dumps({
    "file": agentbodycam.__file__,
    "ui_root": str(packaged_ui_root()),
    "version": agentbodycam.__version__,
    "metadata_version": md.version("agentbodycam"),
    "direct_url": md.distribution("agentbodycam").read_text("direct_url.json") or "",
    "repo_on_path": any(repo in (p or "").lower() for p in sys.path),
    "repo_loaded": any(repo in f.lower() for f in loaded),
    "assets": sorted(bundle.assets),
}))
"""


def test_installed_wheel_runs_from_site_packages(installed: Installed) -> None:
    assert shutil.which("node", path=installed.env["PATH"]) is None
    assert shutil.which("npm", path=installed.env["PATH"]) is None
    probe = installed.run(installed.python, "-c", PROBE, str(ROOT))
    assert probe.returncode == 0, probe.stderr
    facts = json.loads(probe.stdout)
    site = Path(facts["file"]).resolve().parent.parent
    assert site.name == "site-packages"
    assert installed.python.parent.parent.resolve() in site.parents
    assert Path(facts["ui_root"]).resolve().is_relative_to(site)
    assert not facts["repo_on_path"] and not facts["repo_loaded"]
    assert '"editable": true' not in facts["direct_url"]
    assert facts["version"] == facts["metadata_version"] == VERSION
    assert any(name.endswith(".js") for name in facts["assets"])


def test_installed_cli_smoke(installed: Installed) -> None:
    launchers: list[list[str | Path]] = [[installed.cli], [installed.python, "-m", "agentbodycam"]]
    for launcher in launchers:
        version = installed.run(*launcher, "--version")
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == f"agentbodycam {VERSION}"
    help_text = installed.run(installed.cli, "--help")
    assert help_text.returncode == 0
    for name in ("record", "run", "analyze", "replay", "replay-api", "doctor", "session"):
        assert name in help_text.stdout
    doctor = installed.run(installed.cli, "doctor")
    assert doctor.returncode == 0, doctor.stderr
    assert f"AgentBodycam : {VERSION}" in doctor.stdout
    assert "not found on PATH" in doctor.stdout  # Git is deliberately absent here


def test_installed_cli_expected_failures_are_clean(installed: Installed) -> None:
    project = installed.work / "failure project"
    project.mkdir()
    marker = project / "ran.txt"
    script = f"open({str(marker)!r}, 'w').close()"
    cases: list[tuple[list[str | Path], int]] = [
        (["run", "--project", project, "--", installed.python, "-c", script], 125),
        (["analyze", "--project", project, "not-a-session"], 2),
        (["analyze", "--project", project, "20261014T000000Z-0000000000000000"], 1),
        (["replay", "--project", project, "--host", "0.0.0.0"], 2),  # noqa: S104
        (["replay-api", "--project", project, "--host", "192.168.1.10"], 2),
    ]
    for args, code in cases:
        result = installed.run(installed.cli, *args)
        assert result.returncode == code, (args, result.stderr)
        assert "error" in result.stderr and "Traceback" not in result.stderr, args
    assert not marker.exists()
    assert not (project / ".agentbodycam").exists()


def _get(port: int, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


@contextlib.contextmanager
def _replay(installed: Installed, project: Path) -> Iterator[int]:
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments
        [str(installed.python), "-m", "agentbodycam", "replay", "--project", str(project)],
        cwd=installed.work,
        env=installed.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert process.stdout is not None
        port = None
        for line in process.stdout:
            match = LISTENING.match(line.strip())
            if match:
                port = int(match.group(1))
                break
        assert port is not None, process.stderr.read() if process.stderr else ""
        yield port
    finally:
        process.terminate()
        process.wait(timeout=30)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_installed_replay_ui_serves_packaged_assets_without_node(installed: Installed) -> None:
    project = installed.work / "demo project"
    project.mkdir()
    created = installed.run(installed.cli, "session", "create", "--project", project)
    assert created.returncode == 0, created.stderr
    session_id = SESSION_ID.search(created.stdout)
    assert session_id is not None
    with _replay(installed, project) as port:
        status, headers, index = _get(port, "/", {})
        assert status == 200
        assert "'self'" in headers["content-security-policy"]
        assert "http" not in headers["content-security-policy"]
        references = re.findall(rb'(?:src|href)="(/[^"]+)"', index)
        assert any(ref.endswith(b".js") for ref in references)
        bundle = b""
        for reference in references:
            status, _, body = _get(port, reference.decode(), {})
            assert status == 200 and body, reference
            bundle += body
        for export in (b"agentbodycam-replay.svg", b"agentbodycam-summary.json"):
            assert export in bundle
        assert b"agentbodycam-replay.webm" in bundle and b"MediaRecorder" in bundle
        fetch = {"X-AgentBodycam-UI": "bootstrap", "Sec-Fetch-Site": "same-origin"}
        fetch |= {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        status, _, body = _get(port, "/ui/bootstrap", fetch)
        assert status == 200
        auth = {"Authorization": f"Bearer {json.loads(body)['token']}"}
        status, headers, body = _get(port, "/api/v1/sessions", auth)
        assert status == 200
        assert [s["session_id"] for s in json.loads(body)["sessions"]] == [session_id.group()]
        assert not [name for name in headers if name.startswith("access-control-")]
        assert _get(port, "/api/v1/sessions", {})[0] == 401
        assert _get(port, "/ui/bootstrap", {})[0] in (401, 403)
