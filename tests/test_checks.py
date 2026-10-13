"""Pure, deterministic check classification (no processes are started here)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import agentbodycam.core.checks as checks_module
from agentbodycam.core.checks import (
    FALLBACK_NAME,
    CheckClassification,
    CheckKind,
    CheckReason,
    ClassificationSource,
    classify_command,
    explicit_classification,
    judge,
    program_name,
    unwrap_shell,
)

TEST, BUILD, LINT, TYPECHECK = (
    CheckKind.TEST,
    CheckKind.BUILD,
    CheckKind.LINT,
    CheckKind.TYPECHECK,
)
HIDDEN = "SUPERSECRET-9f8e7d"


def auto(argv: str | list[str]) -> tuple[CheckKind, str] | None:
    result = classify_command(argv.split() if isinstance(argv, str) else argv)
    if result is None:
        return None
    assert result.source is ClassificationSource.AUTOMATIC
    return result.kind, result.name


RECOGNISED = [
    # tests
    ("pytest", TEST, "pytest"),
    ("pytest -x tests/test_app.py", TEST, "pytest"),
    ("python -m pytest", TEST, "pytest"),
    ("python3 -m pytest -q", TEST, "pytest"),
    ("python3.12 -m pytest", TEST, "pytest"),
    ("py -m pytest", TEST, "pytest"),
    ("py -3.12 -m pytest", TEST, "pytest"),
    ("python -B -u -m pytest", TEST, "pytest"),
    ("python -m unittest", TEST, "unittest"),
    ("python -m unittest discover -s tests", TEST, "unittest"),
    ("cargo test", TEST, "cargo test"),
    ("cargo test --release", TEST, "cargo test"),
    ("go test ./...", TEST, "go test"),
    ("dotnet test", TEST, "dotnet test"),
    # builds
    ("python -m build", BUILD, "build"),
    ("python -m build --wheel", BUILD, "build"),
    ("cargo build", BUILD, "cargo build"),
    ("go build ./cmd/app", BUILD, "go build"),
    ("dotnet build", BUILD, "dotnet build"),
    # lint
    ("ruff check", LINT, "ruff"),
    ("ruff check .", LINT, "ruff"),
    ("python -m ruff check .", LINT, "ruff"),
    ("eslint .", LINT, "eslint"),
    ("eslint src --max-warnings 0", LINT, "eslint"),
    # typecheck
    ("mypy", TYPECHECK, "mypy"),
    ("mypy src", TYPECHECK, "mypy"),
    ("python -m mypy", TYPECHECK, "mypy"),
    ("python -m mypy .", TYPECHECK, "mypy"),
    ("pyright", TYPECHECK, "pyright"),
    ("python -m pyright", TYPECHECK, "pyright"),
    ("tsc --noEmit", TYPECHECK, "tsc"),
    ("tsc -p tsconfig.json --noemit", TYPECHECK, "tsc"),
    ("npx tsc --noEmit", TYPECHECK, "tsc"),
    ("npx eslint .", LINT, "eslint"),
    ("npx pyright", TYPECHECK, "pyright"),
    # package managers
    ("npm test", TEST, "npm test"),
    ("npm test -- --watch=false", TEST, "npm test"),
    ("npm run test", TEST, "npm test"),
    ("npm run-script test", TEST, "npm test"),
    ("npm run build", BUILD, "npm build"),
    ("npm run lint", LINT, "npm lint"),
    ("npm run typecheck", TYPECHECK, "npm typecheck"),
    ("pnpm test", TEST, "pnpm test"),
    ("pnpm run test", TEST, "pnpm test"),
    ("pnpm build", BUILD, "pnpm build"),
    ("pnpm run build", BUILD, "pnpm build"),
    ("pnpm lint", LINT, "pnpm lint"),
    ("pnpm run lint", LINT, "pnpm lint"),
    ("pnpm typecheck", TYPECHECK, "pnpm typecheck"),
    ("pnpm run typecheck", TYPECHECK, "pnpm typecheck"),
    ("yarn test", TEST, "yarn test"),
    ("yarn run test", TEST, "yarn test"),
    ("yarn build", BUILD, "yarn build"),
    ("yarn run build", BUILD, "yarn build"),
    ("yarn lint", LINT, "yarn lint"),
    ("yarn run lint", LINT, "yarn lint"),
    ("yarn typecheck", TYPECHECK, "yarn typecheck"),
    ("yarn run typecheck", TYPECHECK, "yarn typecheck"),
]


@pytest.mark.parametrize(("argv", "kind", "name"), RECOGNISED)
def test_recognised_commands(argv: str, kind: CheckKind, name: str) -> None:
    assert auto(argv) == (kind, name)


INFORMATIONAL = [
    # pytest
    "pytest --version",
    "pytest -V",
    "pytest --help",
    "pytest -h",
    "python -m pytest --version",
    "python -m pytest --help",
    "python -m pytest --collect-only",
    "python -m pytest tests -q --collect-only",
    "pytest --co",
    "pytest --fixtures",
    "pytest --markers",
    "pytest --setup-plan",
    # unittest / build
    "python -m unittest --help",
    "python -m unittest discover -h",
    "python -m build --version",
    # mypy / pyright
    "mypy --version",
    "mypy -V",
    "mypy --help",
    "python -m mypy --version",
    "pyright --version",
    "pyright --help",
    "npx pyright --version",
    # ruff / eslint
    "ruff check --help",
    "ruff check --show-settings",
    "ruff check . --show-files",
    "python -m ruff check --help",
    "eslint --version",
    "eslint -v",
    "eslint --help",
    "eslint --print-config config.js",
    "eslint --env-info",
    "npx eslint --version",
    # tsc
    "tsc --noEmit --help",
    "tsc --noEmit --version",
    "tsc --noEmit -v",
    "tsc --noEmit --showConfig",
    "tsc --noemit --SHOWCONFIG",
    "tsc --noEmit --listFilesOnly",
    "npx tsc --noEmit --showConfig",
    # cargo
    "cargo test --help",
    "cargo test -h",
    "cargo test --no-run",
    "cargo test -- --list",
    "cargo build --help",
    # go
    "go test -h",
    "go test -help",
    "go test -list TestFoo",
    "go test -list=TestFoo",
    "go test --list=TestFoo ./...",
    "go test -n ./...",
    "go build -h",
    "go build -n",
    # dotnet
    "dotnet test --help",
    "dotnet test -h",
    "dotnet test -?",
    "dotnet test --list-tests",
    "dotnet test -t",
    "dotnet build --help",
    "dotnet build /?",
    # package-manager scripts
    "npm test -- --help",
    "npm test --help",
    "npm run test -- --help",
    "npm run build -- --help",
    "npm run lint -- --version",
    "pnpm test -- --help",
    "pnpm lint -- --help",
    "pnpm run typecheck -- -h",
    "yarn lint -- --help",
    "yarn typecheck -- --help",
    "yarn build -- -v",
]


@pytest.mark.parametrize("argv", INFORMATIONAL)
def test_informational_invocations_are_not_automatic_checks(argv: str) -> None:
    assert auto(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["cmd", "/c", "npm", "test", "--", "--help"],
        ["cmd", "/c", "pytest --version"],
        ["sh", "-c", "cargo test --no-run"],
    ],
)
def test_informational_invocations_behind_wrappers(argv: list[str]) -> None:
    assert auto(argv) is None


@pytest.mark.parametrize(
    ("argv", "kind", "name"),
    [
        ("pytest -q", TEST, "pytest"),
        ("python -m pytest tests", TEST, "pytest"),
        ("python -m pytest -k version", TEST, "pytest"),  # a value, not the flag
        ("python -m unittest discover", TEST, "unittest"),
        ("mypy src", TYPECHECK, "mypy"),
        ("ruff check .", LINT, "ruff"),
        ("eslint src", LINT, "eslint"),
        ("tsc --noEmit", TYPECHECK, "tsc"),
        ("cargo test", TEST, "cargo test"),
        ("cargo test -- --nocapture", TEST, "cargo test"),
        ("cargo build", BUILD, "cargo build"),
        ("go test ./...", TEST, "go test"),
        ("go test -run TestFoo ./...", TEST, "go test"),
        ("go test -v ./...", TEST, "go test"),
        ("dotnet test", TEST, "dotnet test"),
        ("npm test", TEST, "npm test"),
        ("npm test -- --watch=false", TEST, "npm test"),
        ("npm run build", BUILD, "npm build"),
        ("pnpm lint", LINT, "pnpm lint"),
        ("yarn typecheck", TYPECHECK, "yarn typecheck"),
    ],
)
def test_genuine_runs_are_still_automatic_checks(argv: str, kind: CheckKind, name: str) -> None:
    assert auto(argv) == (kind, name)


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        ("pytest --version", "pytest"),
        ("python -m pytest --collect-only", "pytest"),
        ("mypy --version", "mypy"),
        ("cargo test --no-run", "cargo test"),
        ("npm test -- --help", "npm test"),
    ],
)
def test_explicit_check_still_classifies_informational_invocations(argv: str, name: str) -> None:
    result = explicit_classification(TEST, argv.split())
    assert result == CheckClassification(TEST, name, ClassificationSource.EXPLICIT)


@pytest.mark.parametrize(
    "argv",
    [
        r"C:\Python312\python.exe -m pytest",
        "/usr/bin/python3 -m pytest",
        ".venv/bin/pytest",
        r".venv\Scripts\pytest.exe",
        "PYTEST.EXE",
    ],
)
def test_program_paths_and_windows_suffixes(argv: str) -> None:
    assert auto(argv) == (TEST, "pytest")


@pytest.mark.parametrize(
    ("argv", "kind", "name"),
    [
        (["cmd", "/c", "npm", "test"], TEST, "npm test"),
        (["cmd.exe", "/c", "npm", "run", "build"], BUILD, "npm build"),
        (["CMD.EXE", "/C", "pnpm", "lint"], LINT, "pnpm lint"),
        (["cmd", "/d", "/s", "/c", "yarn", "typecheck"], TYPECHECK, "yarn typecheck"),
        (["cmd", "/c", "npm test"], TEST, "npm test"),
        ([r"C:\Windows\System32\cmd.exe", "/c", "npx", "tsc", "--noEmit"], TYPECHECK, "tsc"),
        (["cmd", "/c", "python", "-m", "pytest"], TEST, "pytest"),
        (["sh", "-c", "npm test"], TEST, "npm test"),
        (["bash", "-c", "cargo build --release"], BUILD, "cargo build"),
        (["/bin/zsh", "-c", "python3 -m mypy src"], TYPECHECK, "mypy"),
    ],
)
def test_explicit_shell_wrappers_are_unwrapped(argv: list[str], kind: CheckKind, name: str) -> None:
    assert auto(argv) == (kind, name)


@pytest.mark.parametrize(
    "argv",
    [
        # ambiguous or unknown
        "python script.py",
        "python -c pass",
        "python",
        "python -m",
        "python -m http.server",
        "python -m ruff format .",
        "python -W error -m pytest",  # an option with a value: not parsed, not guessed
        "npm run custom",
        "npm run",
        "npm install",
        "npm build",  # not the build script
        "npm --prefix app test",
        "pnpm install",
        "pnpm",
        "yarn",
        "yarn run deploy",
        "make",
        "make test",
        "bash build.sh",
        "ruff",
        "ruff format --check .",
        "ruff .",
        "tsc",
        "tsc --build",
        "tsc --noEmit false",
        "cargo",
        "cargo run",
        "cargo clippy",
        "go vet ./...",
        "go",
        "dotnet run",
        "npx",
        "npx prettier --check .",
        "node test.js",
        "git status",
        "unittest",
        # shell wrappers used in any other way
        "cmd",
        "cmd /c",
        "cmd /k npm test",
        "cmd npm test",
        "sh build.sh",
        "bash -lc pytest",
        "sh -c",
    ],
)
def test_ambiguous_or_unknown_commands_are_not_classified(argv: str) -> None:
    assert auto(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["cmd", "/c", "npm", "test", "&&", "rm", "-rf", "x"],
        ["cmd", "/c", "npm", "test", "|", "more"],
        ["cmd", "/c", "npm test > out.txt"],
        ["cmd", "/c", "npm", "run", "%SCRIPT%"],
        ["cmd", "/c", '"npm" test'],
        ["cmd", "/c", "npm", "test", "^&", "x"],
        ["sh", "-c", "npm test && npm run build"],
        ["sh", "-c", "npm test; rm -rf x"],
        ["sh", "-c", "pytest $ARGS"],
        ["sh", "-c", "pytest 'a b'"],
        ["sh", "-c", "pytest `x`"],
        ["sh", "-c", "pytest\nrm -rf x"],
        ["sh", "-c", "pytest *"],
        ["sh", "-c", "npm test", "argv0"],
        ["sh", "-c", "sh -c pytest"],
        ["cmd", "/c", "cmd", "/c", "npm", "test"],  # one level of wrapping only
    ],
)
def test_shell_syntax_inside_a_wrapper_is_never_guessed(argv: list[str]) -> None:
    assert auto(argv) is None


def test_unwrap_shell() -> None:
    assert unwrap_shell(["pytest", "-q"]) == ["pytest", "-q"]
    assert unwrap_shell(["cmd", "/c", "npm", "test"]) == ["npm", "test"]
    assert unwrap_shell(["cmd", "/c", "npm  run   build"]) == ["npm", "run", "build"]
    assert unwrap_shell(["sh", "-c", "go test ./..."]) == ["go", "test", "./..."]
    assert unwrap_shell([]) is None
    assert unwrap_shell(["cmd", "/c", "npm & x"]) is None


def test_program_name() -> None:
    assert program_name(r"C:\Tools\Node\NPM.CMD") == "npm"
    assert program_name("/usr/local/bin/cargo") == "cargo"
    assert program_name("pytest.exe") == "pytest"
    assert program_name(".exe") == ".exe"


def test_classification_never_mutates_argv() -> None:
    argv = ["cmd", "/c", "npm", "run", "build"]
    snapshot = list(argv)
    classify_command(argv)
    explicit_classification("test", argv)
    assert argv == snapshot


def test_classification_is_deterministic() -> None:
    for argv, _, _ in RECOGNISED:
        assert classify_command(argv.split()) == classify_command(argv.split())


# -- explicit -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(CheckKind))
def test_explicit_kinds(kind: CheckKind) -> None:
    result = explicit_classification(kind.value, ["python", "script.py"])
    assert result == CheckClassification(kind, "python", ClassificationSource.EXPLICIT)


def test_invalid_explicit_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="'compile'"):
        explicit_classification("compile", ["make"])
    assert [k.value for k in CheckKind] == ["test", "build", "lint", "typecheck"]


def test_explicit_kind_overrides_automatic_classification() -> None:
    result = explicit_classification(BUILD, ["python", "-m", "pytest"])
    assert result.kind is BUILD
    assert result.name == "pytest"  # the recognised tool still names it
    assert result.source is ClassificationSource.EXPLICIT


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["make"], "make"),
        (["bash", "build.sh"], "bash"),
        (["cmd", "/c", "build.bat"], "build"),
        (["cmd", "/c", "npm", "run", "custom"], "npm"),
        ([r"C:\Users\someone\bin\My Tool.exe", "x"], FALLBACK_NAME),
        (["/home/someone/bin/tool_v2"], "tool_v2"),
        (["/home/someone/bin/Tool V2"], FALLBACK_NAME),
        (["./run-checks.sh"], "run-checks.sh"),
        (["x" * 40], FALLBACK_NAME),
        (["ghp_" + "a1b2c3d4e5f6g7h8i9j0k1"], FALLBACK_NAME),
        (["sk-" + "abcdef0123456789abcd"], FALLBACK_NAME),
        (["cmd", "/c", "npm test && echo"], "cmd"),
        ([""], FALLBACK_NAME),
    ],
)
def test_explicit_names_for_unrecognised_commands(argv: list[str], name: str) -> None:
    assert explicit_classification(TEST, argv).name == name


# -- privacy ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "--token", HIDDEN],
        ["python", "-m", "pytest", f"--api-key={HIDDEN}", f"/home/someone/{HIDDEN}"],
        ["cmd", "/c", "npm", "test", "--", f"password={HIDDEN}"],
        ["npm", "run", "build", f"https://bob:{HIDDEN}@example.com"],
        ["eslint", f"C:\\Users\\someone\\{HIDDEN}"],
    ],
)
def test_secret_arguments_never_reach_the_classification(argv: list[str]) -> None:
    for result in (classify_command(argv), explicit_classification(LINT, argv)):
        assert result is not None
        assert HIDDEN not in result.name
        assert "someone" not in result.name
        assert " " not in result.name or result.name.split(" ")[0] in ("npm", "pnpm", "yarn")


def test_names_are_short_and_plain() -> None:
    names = {name for _, _, name in RECOGNISED}
    for name in names:
        assert len(name) <= 32
        assert all(ch.isalnum() or ch in " ._+-" for ch in name)


def test_classifier_module_starts_no_processes() -> None:
    tree = ast.parse(Path(checks_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"subprocess", "os", "shlex", "shutil", "socket", "pathlib"})


# -- pass / fail -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "exit_code", "expected"),
    [
        ("exited", 0, (True, CheckReason.EXIT_ZERO)),
        ("exited", 1, (False, CheckReason.NONZERO_EXIT)),
        ("exited", 2, (False, CheckReason.NONZERO_EXIT)),
        ("exited", 255, (False, CheckReason.NONZERO_EXIT)),
        ("exited", 0xC000013A, (False, CheckReason.NONZERO_EXIT)),
        ("signaled", None, (False, CheckReason.SIGNALED)),
        ("interrupted", 0, (False, CheckReason.INTERRUPTED)),
        ("interrupted", None, (False, CheckReason.INTERRUPTED)),
        ("spawn_error", None, (False, CheckReason.SPAWN_ERROR)),
    ],
)
def test_judge(outcome: str, exit_code: int | None, expected: tuple[bool, CheckReason]) -> None:
    assert judge(outcome, exit_code) == expected


def test_judge_rejects_unknown_outcomes() -> None:
    with pytest.raises(ValueError, match="unknown command outcome"):
        judge("crashed", 1)
