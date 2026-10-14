"""The GitHub Actions configuration stays read-only, publishes nothing and covers every
supported OS and Python version. Text checks only: the project has no YAML dependency."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

if not WORKFLOWS.is_dir():  # the sdist does not ship .github/
    pytest.skip("no .github/workflows in this tree", allow_module_level=True)

ALLOWED_ACTIONS = {
    "actions/checkout@v7",
    "actions/setup-python@v7",
    "actions/setup-node@v7",
    "actions/upload-artifact@v7",
    "./.github/workflows/ci.yml",
}
PUBLISHING = re.compile(
    r"gh-action-pypi-publish|twine|npm publish|(?:hatch|uv|poetry|flit) publish|gh release"
    r"|action-gh-release|create-release|git push|git tag|id-token|secrets\.",
    re.IGNORECASE,
)


def _workflows() -> dict[str, str]:
    files = sorted(WORKFLOWS.glob("*.y*ml"))
    assert [path.name for path in files] == ["ci.yml", "release-build.yml"]
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def test_workflows_are_read_only_and_publish_nothing() -> None:
    for name, text in _workflows().items():
        assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE), name
        assert len(re.findall(r"^\s*permissions:", text, re.MULTILINE)) == 1, name
        assert not re.search(r":\s*write\b", text), name
        assert not PUBLISHING.search(text), (name, PUBLISHING.search(text))
        assert "pull_request_target" not in text, name


def test_workflows_use_only_official_actions() -> None:
    for name, text in _workflows().items():
        used = set(re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE))
        assert used and used <= ALLOWED_ACTIONS, (name, sorted(used - ALLOWED_ACTIONS))


def test_ci_covers_every_supported_platform_and_python() -> None:
    ci = _workflows()["ci.yml"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    classified = [
        c.rsplit(" :: ", 1)[1]
        for c in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    ]
    matrix = re.search(r"python-version: \[(.*)\]", ci)
    assert matrix is not None
    assert re.findall(r'"(3\.\d+)"', matrix.group(1)) == classified
    assert project["requires-python"] == f">={classified[0]}"
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in ci
    assert "fail-fast: false" in ci
    assert "run: python scripts/check.py" in ci
    for command in ("npm ci", "npm run typecheck", "npm run test", "npm run build"):
        assert f"run: {command}\n" in ci, command
    assert "git status --porcelain --untracked-files=all" in ci
    assert "AGENTBODYCAM_TEST_DIST: dist" in ci
    assert "run: python -m pytest tests/test_packaging.py" in ci
