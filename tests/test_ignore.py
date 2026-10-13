from __future__ import annotations

import pytest

from agentbodycam.core.ignore import DEFAULT_IGNORE_POLICY, IgnorePolicy


@pytest.mark.parametrize(
    "name",
    [
        ".agentbodycam",
        ".git",
        ".GIT",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".env",
        ".Env.Local",
        ".envrc",
        "server.pem",
        "tls.KEY",
        "id_ed25519",
        "id_rsa.pub",
        "file.swp",
        "backup~",
        ".DS_Store",
    ],
)
def test_default_policy_ignores(name: str) -> None:
    assert DEFAULT_IGNORE_POLICY.is_ignored(name)


@pytest.mark.parametrize(
    "name",
    [
        "app.py",
        "environment.py",
        "env",
        ".envrc.md.txt",
        "keys.py",
        "monkey.keyboard",
        "pem.txt",
        "git_helpers.py",
        "venv.md",
        ".github",
        ".gitignore",
    ],
)
def test_default_policy_keeps(name: str) -> None:
    assert not DEFAULT_IGNORE_POLICY.is_ignored(name)


def test_sensitive_is_subset_of_ignored() -> None:
    assert DEFAULT_IGNORE_POLICY.is_sensitive(".env.production")
    assert not DEFAULT_IGNORE_POLICY.is_sensitive("node_modules")


def test_custom_policy_is_case_insensitive() -> None:
    policy = IgnorePolicy(ignored_names=frozenset({"Build"}), sensitive_patterns=("*.SECRET",))
    assert policy.is_ignored("build")
    assert policy.is_ignored("a.secret")
    assert not policy.is_ignored(".git")
