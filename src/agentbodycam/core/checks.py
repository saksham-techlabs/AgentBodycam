"""Deterministic classification of wrapped commands as checks (test, build, lint, typecheck).

Pure functions only: nothing here starts a process, reads a file or looks at command output.

A check is either classified *explicitly* (``agentbodycam run --check KIND -- ...``, always
authoritative) or *automatically*, which recognises only a short whitelist of unmistakable
command lines (``python -m pytest``, ``cargo test``, ``npm run build``, ``tsc --noEmit``,
...). Anything else, including ``python script.py``, ``make`` or an unknown npm script, is
left unclassified: it stays a plain command.

Check names are never built by joining argv. They come from fixed strings for recognised
tools, or (explicit classification of an unrecognised command) from the program's
lowercase base name when it is short and harmless, else ``command``. So arguments, paths
and secrets can never reach a check name.

Whether a check passed is decided only from how the process ended: it passed exactly when
it exited by itself with status 0. Output is never parsed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from agentbodycam.core.redact import looks_secret

MAX_NAME_CHARS = 32
FALLBACK_NAME = "command"


class CheckKind(StrEnum):
    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    TYPECHECK = "typecheck"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class ClassificationSource(StrEnum):
    EXPLICIT = "explicit"  # the user said so with --check
    AUTOMATIC = "automatic"  # a whitelisted command line was recognised


class CheckReason(StrEnum):
    EXIT_ZERO = "exit_zero"
    NONZERO_EXIT = "nonzero_exit"
    SIGNALED = "signaled"
    INTERRUPTED = "interrupted"
    SPAWN_ERROR = "spawn_error"


@dataclass(frozen=True, slots=True)
class CheckClassification:
    kind: CheckKind
    name: str
    source: ClassificationSource


# -- normalisation -----------------------------------------------------------------------

_PROGRAM_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
_PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?|py")
_PY_LAUNCHER_VERSION = re.compile(r"-\d+(?:\.\d+)?(?:-(?:32|64))?")
# Interpreter flags that take no value and do not change what `-m` runs.
_PYTHON_FLAGS = frozenset({"-B", "-E", "-I", "-O", "-OO", "-q", "-s", "-S", "-u"})
_POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_CMD_SWITCHES = frozenset({"/d", "/q", "/s"})
# Shell scripts are only split when they are plain words; anything a shell would
# interpret (quotes, escapes, expansions, operators, globs, newlines) leaves the command
# unclassified.
_POSIX_PLAIN_SCRIPT = re.compile(r"[A-Za-z0-9 ._:/=+@,-]+")
_CMD_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9 ._:/\\=+@,-]+")
_SAFE_NAME = re.compile(rf"[a-z0-9][a-z0-9._+-]{{0,{MAX_NAME_CHARS - 1}}}")


def program_name(arg: str) -> str:
    """Lowercase base name of a program, without directory or Windows suffix."""
    name = re.split(r"[\\/]", arg)[-1].lower()
    for suffix in _PROGRAM_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def unwrap_shell(argv: Sequence[str]) -> list[str] | None:
    """The command an explicit shell wrapper runs, ``argv`` itself if it is not one.

    Recognised wrappers: ``cmd [/d] [/q] [/s] /c <command...>`` and
    ``sh|bash|zsh|dash -c "<command>"``. ``None`` when a wrapper is used in any other way
    or its command contains shell syntax (``&&``, ``|``, ``>``, quotes, variables, ...).
    Classification only: the command line actually run is never changed.
    """
    if not argv:
        return None
    program = program_name(argv[0])
    if program == "cmd":
        index = 1
        while index < len(argv) and argv[index].lower() in _CMD_SWITCHES:
            index += 1
        if index >= len(argv) or argv[index].lower() != "/c":
            return None
        rest = list(argv[index + 1 :])
        if not all(_CMD_PLAIN_TOKEN.fullmatch(token) for token in rest):
            return None
        tokens = rest[0].split() + rest[1:] if len(rest) == 1 else rest
        return tokens or None
    if program in _POSIX_SHELLS:
        if len(argv) != 3 or argv[1] != "-c" or not _POSIX_PLAIN_SCRIPT.fullmatch(argv[2]):
            return None
        return argv[2].split() or None
    return list(argv)


# -- automatic classification ------------------------------------------------------------

_Match = tuple[CheckKind, str]

_PACKAGE_SCRIPTS = {
    "test": CheckKind.TEST,
    "build": CheckKind.BUILD,
    "lint": CheckKind.LINT,
    "typecheck": CheckKind.TYPECHECK,
}
_SUBCOMMAND_TOOLS = frozenset({"cargo", "go", "dotnet"})
_SUBCOMMANDS = {"test": CheckKind.TEST, "build": CheckKind.BUILD}
_PYTHON_MODULES = {
    "pytest": (CheckKind.TEST, "pytest"),
    "unittest": (CheckKind.TEST, "unittest"),
    "build": (CheckKind.BUILD, "build"),
    "mypy": (CheckKind.TYPECHECK, "mypy"),
    "pyright": (CheckKind.TYPECHECK, "pyright"),
}
_NPX_TOOLS = frozenset({"eslint", "tsc", "pyright"})


def _tool(program: str, args: Sequence[str]) -> _Match | None:
    if program == "pytest":
        return CheckKind.TEST, "pytest"
    if program in ("mypy", "pyright"):
        return CheckKind.TYPECHECK, program
    if program == "eslint":
        return CheckKind.LINT, "eslint"
    if program == "ruff" and args[:1] == ["check"]:
        return CheckKind.LINT, "ruff"
    if program == "tsc":
        lowered = [arg.lower() for arg in args]
        if "--noemit" in lowered:
            following = lowered[lowered.index("--noemit") + 1 :]
            if following[:1] != ["false"]:
                return CheckKind.TYPECHECK, "tsc"
    return None


def _python_module(program: str, args: Sequence[str]) -> _Match | None:
    index = 0
    if program == "py" and args and _PY_LAUNCHER_VERSION.fullmatch(args[0]):
        index = 1
    while index < len(args) and args[index] in _PYTHON_FLAGS:
        index += 1
    if index + 1 >= len(args) or args[index] != "-m":
        return None
    module, rest = args[index + 1], args[index + 2 :]
    if module == "ruff":
        return (CheckKind.LINT, "ruff") if rest[:1] == ["check"] else None
    return _PYTHON_MODULES.get(module)


def _package_script(manager: str, args: Sequence[str]) -> _Match | None:
    if not args:
        return None
    if manager == "npm":
        if args[0] == "test":
            script = "test"
        elif args[0] in ("run", "run-script") and len(args) > 1:
            script = args[1]
        else:
            return None
    else:  # pnpm and yarn run a package script by its bare name too
        script = args[1] if args[0] == "run" and len(args) > 1 else args[0]
    kind = _PACKAGE_SCRIPTS.get(script)
    return None if kind is None else (kind, f"{manager} {script}")


def _match(argv: Sequence[str]) -> _Match | None:
    tokens = unwrap_shell(argv)
    if not tokens:
        return None
    program, args = program_name(tokens[0]), tokens[1:]
    if _PYTHON.fullmatch(program):
        return _python_module(program, args)
    if program in ("npm", "pnpm", "yarn"):
        return _package_script(program, args)
    if program == "npx":
        if not args or program_name(args[0]) not in _NPX_TOOLS:
            return None
        return _tool(program_name(args[0]), args[1:])
    if program in _SUBCOMMAND_TOOLS:
        kind = _SUBCOMMANDS.get(args[0]) if args else None
        return None if kind is None else (kind, f"{program} {args[0]}")
    return _tool(program, args)


def classify_command(argv: Sequence[str]) -> CheckClassification | None:
    """Automatic classification of a whitelisted command line, else ``None``."""
    match = _match(argv)
    if match is None:
        return None
    return CheckClassification(match[0], match[1], ClassificationSource.AUTOMATIC)


# -- explicit classification -------------------------------------------------------------


def _fallback_name(argv: Sequence[str]) -> str:
    tokens = unwrap_shell(argv) or list(argv)
    program = program_name(tokens[0]) if tokens else ""
    if _SAFE_NAME.fullmatch(program) and not looks_secret(program):
        return program
    return FALLBACK_NAME


def explicit_classification(kind: CheckKind | str, argv: Sequence[str]) -> CheckClassification:
    """``--check KIND``: the kind is the user's; the name is still a generated one."""
    match = _match(argv)
    name = match[1] if match is not None else _fallback_name(argv)
    return CheckClassification(CheckKind(kind), name, ClassificationSource.EXPLICIT)


# -- results -----------------------------------------------------------------------------


def judge(outcome: str, exit_code: int | None) -> tuple[bool, CheckReason]:
    """``(passed, reason)`` from a command outcome (``exited``, ``signaled``, ...).

    Passed only when the process exited by itself with status 0.
    """
    if outcome == "exited":
        if exit_code == 0:
            return True, CheckReason.EXIT_ZERO
        return False, CheckReason.NONZERO_EXIT
    if outcome == "signaled":
        return False, CheckReason.SIGNALED
    if outcome == "interrupted":
        return False, CheckReason.INTERRUPTED
    if outcome == "spawn_error":
        return False, CheckReason.SPAWN_ERROR
    raise ValueError(f"unknown command outcome: {outcome!r}")
