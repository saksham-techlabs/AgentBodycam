"""Command-line redaction for recorded evidence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentbodycam.core.redact import (
    EXTERNAL_PATH,
    MAX_ARG_CHARS,
    MAX_ARGV_CHARS,
    MAX_ARGV_ITEMS,
    REDACTED,
    is_sensitive_name,
    project_relative,
    redact_command,
)

WINDOWS = sys.platform == "win32"
PROJECT = Path(r"C:\Users\me\work\app") if WINDOWS else Path("/home/me/work/app")
HOME = Path(r"C:\Users\me") if WINDOWS else Path("/home/me")


def native(*parts: str) -> str:
    return str(PROJECT.joinpath(*parts))


def redact(*argv: str) -> tuple[str, ...]:
    return redact_command(argv, project_root=PROJECT, home=HOME).argv


def test_plain_command_is_unchanged() -> None:
    result = redact_command(["python", "-m", "pytest", "-q"], project_root=PROJECT, home=HOME)
    assert result.argv == ("python", "-m", "pytest", "-q")
    assert result.executable == "python"
    assert result.argument_count == 3
    assert not (result.redacted or result.truncated or result.paths_rewritten)
    assert result.display() == "python -m pytest -q"


@pytest.mark.parametrize(
    "flag",
    [
        "--password",
        "--passwd",
        "--token",
        "--secret",
        "--api-key",
        "--api_key",
        "--access-key",
        "--access_key",
        "--private-key",
        "--credential",
        "--auth",
        "--authorization",
        "--bearer",
        "--client-secret",
        "--github-token",
        "-pass",
        "--key",
    ],
)
def test_sensitive_flag_value_is_redacted(flag: str) -> None:
    result = redact_command(["tool", flag, "S3CR3T", "next"], project_root=PROJECT, home=HOME)
    assert result.argv == ("tool", flag, REDACTED, "next")
    assert result.redacted


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("--token=S3CR3T", f"--token={REDACTED}"),
        ("--api-key=S3CR3T", f"--api-key={REDACTED}"),
        ("password=S3CR3T", f"password={REDACTED}"),
        ("DB_PASSWORD=S3CR3T", f"DB_PASSWORD={REDACTED}"),
        ("AWS_SECRET_ACCESS_KEY=S3CR3T", f"AWS_SECRET_ACCESS_KEY={REDACTED}"),
        ("Authorization: Bearer S3CR3T", f"Authorization: {REDACTED}"),
        ("X-Api-Key: S3CR3T", f"X-Api-Key: {REDACTED}"),
        ("pass:S3CR3T", f"pass:{REDACTED}"),
        ("Bearer S3CR3T", f"Bearer {REDACTED}"),
        ("--header=Authorization: Bearer S3CR3T", f"--header=Authorization: Bearer {REDACTED}"),
    ],
)
def test_inline_secrets_are_redacted(item: str, expected: str) -> None:
    assert redact("tool", item) == ("tool", expected)


def test_value_after_flag_starting_with_double_dash_is_kept() -> None:
    assert redact("tool", "--no-auth", "--verbose") == ("tool", "--no-auth", "--verbose")


def test_bare_bearer_redacts_next_item() -> None:
    assert redact("curl", "-H", "Bearer", "S3CR3T") == ("curl", "-H", "Bearer", REDACTED)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:hunter2@example.com/x", f"https://{REDACTED}@example.com/x"),
        ("https://ghtoken@github.com/o/r.git", f"https://{REDACTED}@github.com/o/r.git"),
        ("postgres://u:p@db:5432/x", f"postgres://{REDACTED}@db:5432/x"),
        ("https://e.com/a?token=abc&page=2", f"https://e.com/a?token={REDACTED}&page=2"),
        ("https://e.com/a?x=1&api_key=abc", f"https://e.com/a?x=1&api_key={REDACTED}"),
        ("https://example.com/path", "https://example.com/path"),
    ],
)
def test_url_credentials_are_redacted(url: str, expected: str) -> None:
    assert redact("git", "clone", url) == ("git", "clone", expected)


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + "a1B2" * 9,
        "github_pat_" + "A1b2" * 8,
        "sk-" + "proj-" + "Ab1" * 10,
        "sk-ant-api03-" + "x" * 30,
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "xoxb-" + "1234567890-abcdef",
        "AIza" + "B" * 35,
        "sk_live_" + "a" * 24,
        "npm_" + "A" * 36,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "Zx9" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tV9wX1yZ",
    ],
)
def test_recognisable_tokens_are_redacted_anywhere(token: str) -> None:
    result = redact_command(
        ["tool", token, f"--opt={token}", f"prefix:{token}"], project_root=PROJECT, home=HOME
    )
    assert all(token not in item for item in result.argv)
    assert result.redacted


def test_private_key_material_is_redacted() -> None:
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    assert redact("tool", key) == ("tool", REDACTED)


def test_git_shas_and_ordinary_words_are_kept() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert redact("git", "show", sha, "--stat") == ("git", "show", sha, "--stat")
    assert redact("pytest", "tests/test_x.py::test_long_name") == (
        "pytest",
        "tests/test_x.py::test_long_name",
    )


def test_sensitive_name_matching() -> None:
    assert is_sensitive_name("--API_KEY")
    assert is_sensitive_name("clientSecret")
    assert is_sensitive_name("pwd")
    assert not is_sensitive_name("--verbose")
    assert not is_sensitive_name("--passes")
    assert not is_sensitive_name("")


# -- paths -------------------------------------------------------------------------------


def test_project_paths_become_relative() -> None:
    result = redact_command(
        ["python", native("src", "app.py"), native()], project_root=PROJECT, home=HOME
    )
    assert result.argv == ("python", "src/app.py", ".")
    assert result.paths_rewritten


def test_external_paths_are_hidden() -> None:
    other = r"D:\data\x.txt" if WINDOWS else "/etc/passwd"
    tool = r"C:\Tools\bin\tool.exe" if WINDOWS else "/usr/local/bin/tool"
    result = redact_command([tool, other, "~/notes.txt"], project_root=PROJECT, home=HOME)
    assert result.argv == (EXTERNAL_PATH, EXTERNAL_PATH, EXTERNAL_PATH)
    assert result.executable == ("tool.exe" if WINDOWS else "tool")


def test_assignment_paths_and_embedded_roots_are_rewritten() -> None:
    sep = "\\" if WINDOWS else "/"
    result = redact_command(
        [
            "pytest",
            f"--cov={native('src')}",
            f"--basetemp={HOME}{sep}tmp",
            f"a;{native('lib')}",
            f"x:{HOME}{sep}y",
        ],
        project_root=PROJECT,
        home=HOME,
    )
    assert result.argv[1] == "--cov=src"
    assert result.argv[2] == f"--basetemp={EXTERNAL_PATH}"
    assert result.argv[3] == f"a;<project>{sep}lib"
    assert str(HOME) not in "".join(result.argv)


def test_foreign_style_absolute_paths_are_hidden_on_every_platform() -> None:
    for path in (r"C:\Users\alice\secret.txt", "D:/data/x.txt", r"\\server\share\x"):
        assert redact("tool", path, f"--out={path}") == (
            "tool",
            EXTERNAL_PATH,
            f"--out={EXTERNAL_PATH}",
        ), path


def test_rooted_paths_are_hidden_but_single_switches_are_kept() -> None:
    hidden = ["/home/alice/.ssh/id_rsa", "/etc/passwd"]
    if WINDOWS:
        hidden.append(r"\Users\alice\notes.txt")  # on POSIX this is just a file name
    for path in hidden:
        assert redact("tool", path) == ("tool", EXTERNAL_PATH), path
    switches = ("/c", "/?", "/p:Configuration=Release", "/Q")
    expected = (EXTERNAL_PATH,) * len(switches) if not WINDOWS else switches
    assert redact("cmd", *switches) == ("cmd", *expected)


def test_relative_paths_are_kept_as_typed() -> None:
    assert redact("python", "scripts/build.py", "../sibling") == (
        "python",
        "scripts/build.py",
        "../sibling",
    )


def test_project_relative_helper() -> None:
    assert project_relative(native("a", "b.txt"), PROJECT) == "a/b.txt"
    assert project_relative(native(), PROJECT) == "."
    assert project_relative(native("..", "escape"), PROJECT) is None
    assert project_relative("relative/path", PROJECT) is None
    assert project_relative("~/x", PROJECT, None) is None


@pytest.mark.skipif(not WINDOWS, reason="Windows paths are case-insensitive")
def test_windows_project_paths_match_case_insensitively() -> None:
    assert redact("tool", r"c:\users\ME\WORK\app\src") == ("tool", "src")


# -- bounds ------------------------------------------------------------------------------


def test_long_items_are_truncated() -> None:
    result = redact_command(["echo", "x" * 1000], project_root=PROJECT, home=HOME)
    assert len(result.argv[1]) == MAX_ARG_CHARS
    assert result.truncated
    assert result.argument_count == 1


def test_item_count_and_total_size_are_capped() -> None:
    many = redact_command(["echo", *map(str, range(500))], project_root=PROJECT, home=HOME)
    assert len(many.argv) == MAX_ARGV_ITEMS
    assert many.truncated and many.argument_count == 500
    assert many.display().endswith("...")
    big = redact_command(["echo"] + ["y" * 200] * 60, project_root=PROJECT, home=HOME)
    assert sum(map(len, big.argv)) <= MAX_ARGV_CHARS
    assert big.truncated


def test_secrets_are_redacted_before_truncation() -> None:
    token = "ghp_" + "Q1w2" * 9
    result = redact_command(
        ["tool", "x" * (MAX_ARG_CHARS - 10) + token], project_root=PROJECT, home=HOME
    )
    assert "ghp_" not in result.argv[1]


def test_display_quotes_whitespace_and_hides_control_characters() -> None:
    result = redact_command(["echo", "a b", "", "x\x1by"], project_root=PROJECT, home=HOME)
    assert result.display() == 'echo "a b" "" x?y'


def test_unicode_is_preserved() -> None:
    assert redact("echo", "héllo wörld ✓", "日本語") == ("echo", "héllo wörld ✓", "日本語")


def test_redaction_is_deterministic() -> None:
    argv = ["tool", "--token", "abc", "https://u:p@h/x", native("f")]
    first = redact_command(argv, project_root=PROJECT, home=HOME)
    assert redact_command(argv, project_root=PROJECT, home=HOME) == first


def test_empty_argv_is_rejected() -> None:
    with pytest.raises(ValueError):
        redact_command([], project_root=PROJECT)
