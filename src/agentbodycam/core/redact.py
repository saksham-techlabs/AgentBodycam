"""Conservative, deterministic redaction of a command line before it is recorded.

Only the *recorded evidence* is redacted; the command itself always runs with its original,
untouched arguments. The rules prefer hiding too much over leaking a secret:

- values of options and assignments whose name looks sensitive (``--token X``,
  ``--token=X``, ``password=X``, ``Authorization: Bearer X``, ``-pass pass:X``),
- credentials in URLs (``https://user:pass@host``) and sensitive URL query parameters,
- recognisable API keys and tokens anywhere (GitHub, OpenAI/Anthropic-style ``sk-``, AWS,
  Slack, Google, Stripe, npm, JWTs, private-key blocks) and long mixed-case random-looking
  strings,
- absolute paths: inside the project they become project-relative POSIX paths, anywhere
  else ``<external-path>``; the project and home directories are never stored verbatim.

The result is also bounded in item count, item length and total size.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

REDACTED = "<redacted>"
EXTERNAL_PATH = "<external-path>"
PROJECT_PLACEHOLDER = "<project>"
HOME_PLACEHOLDER = "<home>"
TRUNCATION_MARK = "...<truncated>"

MAX_ARGV_ITEMS = 64
MAX_ARG_CHARS = 256
MAX_ARGV_CHARS = 4096
MAX_DISPLAY_CHARS = 300

# Matched as substrings of a normalised name (lowercase, "_" and "." turned into "-").
_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "api-key",
    "accesskey",
    "access-key",
    "privatekey",
    "private-key",
    "credential",
    "auth",
    "bearer",
    "cookie",
    "session-key",
    "signature",
)
# Matched only as the whole normalised name: too generic to match inside other words.
_SENSITIVE_NAMES = frozenset({"pass", "pwd", "key", "pat", "sig"})

_NAME = r"[A-Za-z_][A-Za-z0-9_.\-]*"
_FLAG = re.compile(rf"^-{{1,2}}(?P<name>{_NAME})$")
_ASSIGNMENT = re.compile(rf"^(?P<name>[-/]{{0,2}}{_NAME})(?P<sep>\s*[=:]\s*)(?P<value>.+)$", re.S)
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)(\s+)[^\s\"']+")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/?#@\s]+)@")
_QUERY_PARAM = re.compile(rf"(?P<lead>[?&;])(?P<name>{_NAME})=(?P<value>[^&#;\s]*)")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_TOKEN_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bglpat-[A-Za-z0-9_\-]{16,}",
        r"\bsk-[A-Za-z0-9_\-]{16,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
        r"\bAIza[0-9A-Za-z_\-]{30,}",
        r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}",
        r"\bnpm_[A-Za-z0-9]{30,}",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
    )
)
# Long runs of token characters mixing upper case, lower case and digits look random;
# '/' and '\' are excluded so ordinary paths are not swallowed.
_RANDOM_LOOKING = re.compile(r"[A-Za-z0-9_\-+=]{32,}")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


def is_sensitive_name(name: str) -> bool:
    normalised = name.lstrip("-/").lower().replace("_", "-").replace(".", "-")
    if not normalised:
        return False
    return normalised in _SENSITIVE_NAMES or any(part in normalised for part in _SENSITIVE_PARTS)


@dataclass(frozen=True, slots=True)
class RedactedCommand:
    """A command line as it may be recorded."""

    executable: str
    argv: tuple[str, ...]
    argument_count: int
    redacted: bool
    truncated: bool
    paths_rewritten: bool

    def display(self, limit: int = MAX_DISPLAY_CHARS) -> str:
        """A short single-line rendering for summaries (display only, not shell syntax)."""
        parts = [_display_item(item) for item in self.argv]
        if self.truncated:
            parts.append("...")
        text = " ".join(parts)
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        return text


def _display_item(item: str) -> str:
    item = "".join(ch if ch.isprintable() else "?" for ch in item)
    if not item or any(ch.isspace() for ch in item):
        return f'"{item}"'
    return item


class _Redactor:
    def __init__(self, project_root: Path, home: Path | None) -> None:
        self.project_root = project_root
        self.home = home
        self.redacted = False
        self.paths_rewritten = False
        # Longest first, so the project (usually inside home) wins.
        roots = [(project_root, PROJECT_PLACEHOLDER)]
        if home is not None:
            roots.append((home, HOME_PLACEHOLDER))
        self._embedded = [
            (re.compile(re.escape(variant), re.IGNORECASE if _WINDOWS else 0), placeholder)
            for root, placeholder in sorted(roots, key=lambda r: -len(str(r[0])))
            for variant in dict.fromkeys((str(root), str(root).replace("\\", "/")))
            if len(variant) > 1
        ]

    def redact(self) -> str:
        self.redacted = True
        return REDACTED

    def item(self, text: str) -> str:
        if _PRIVATE_KEY.search(text):
            return self.redact()
        match = _ASSIGNMENT.match(text)
        if match and is_sensitive_name(match["name"]) and not _looks_absolute(text):
            return f"{match['name']}{match['sep']}{self.redact()}"
        text = self.scrub(text)
        return self._paths(text)

    def scrub(self, text: str) -> str:
        def userinfo(m: re.Match[str]) -> str:
            return f"{m['scheme']}{self.redact()}@"

        def query(m: re.Match[str]) -> str:
            if not is_sensitive_name(m["name"]) or not m["value"]:
                return m.group(0)
            return f"{m['lead']}{m['name']}={self.redact()}"

        def bearer(m: re.Match[str]) -> str:
            return f"{m[1]}{m[2]}{self.redact()}"

        def token(_m: re.Match[str]) -> str:
            return self.redact()

        def random_looking(m: re.Match[str]) -> str:
            value = m.group(0)
            if (
                any(c.isupper() for c in value)
                and any(c.islower() for c in value)
                and any(c.isdigit() for c in value)
            ):
                return self.redact()
            return value

        text = _URL_USERINFO.sub(userinfo, text)
        text = _QUERY_PARAM.sub(query, text)
        text = _BEARER.sub(bearer, text)
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(token, text)
        return _RANDOM_LOOKING.sub(random_looking, text)

    def _paths(self, text: str) -> str:
        if _looks_absolute(text):
            return self._path(text)
        match = None if _URL.match(text) else _ASSIGNMENT.match(text)
        if match and _looks_absolute(match["value"]):
            return f"{match['name']}{match['sep']}{self._path(match['value'])}"
        for pattern, placeholder in self._embedded:
            text, count = pattern.subn(placeholder, text)
            if count:
                self.paths_rewritten = True
        return text

    def _path(self, text: str) -> str:
        self.paths_rewritten = True
        relative = project_relative(text, self.project_root, self.home)
        return EXTERNAL_PATH if relative is None else relative


_WINDOWS = sys.platform == "win32"


def _looks_absolute(text: str) -> bool:
    if text.startswith("~"):
        return True
    if _WINDOWS:
        return bool(_WINDOWS_ABSOLUTE.match(text))
    return text.startswith("/")


def project_relative(text: str, project_root: Path, home: Path | None = None) -> str | None:
    """``text`` as a normalized project-relative POSIX path (``.`` for the root), or ``None``.

    Purely lexical: the filesystem is not consulted and symlinks are not resolved.
    """
    if text.startswith("~"):
        if home is None:
            return None
        rest = text[1:]
        if rest and rest[0] not in "/\\":
            return None  # ~otheruser
        text = str(home) + rest
    pure: PureWindowsPath | PurePosixPath
    pure = PureWindowsPath(text) if _WINDOWS else PurePosixPath(text)
    normalised = type(pure)(os.path.normpath(str(pure)))
    root = type(pure)(project_root)
    if not normalised.is_absolute() or not normalised.is_relative_to(root):
        return None
    relative = normalised.relative_to(root)
    parts = relative.parts
    if any(part in ("", "..") for part in parts):
        return None
    return "/".join(parts) if parts else "."


def _cap(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_ARG_CHARS:
        return text, False
    return text[: MAX_ARG_CHARS - len(TRUNCATION_MARK)] + TRUNCATION_MARK, True


def redact_command(
    argv: Sequence[str], *, project_root: Path, home: Path | None = None
) -> RedactedCommand:
    """Redact and bound ``argv`` (executable first) for recording. Deterministic."""
    if not argv:
        raise ValueError("argv must not be empty")
    redactor = _Redactor(Path(project_root), home)
    items: list[str] = []
    truncated = False
    total = 0
    redact_next = False
    for raw in argv:
        if redact_next and not raw.startswith("--"):
            item = redactor.redact()
            redact_next = False
        else:
            flag = _FLAG.match(raw)
            redact_next = bool(flag and is_sensitive_name(flag["name"])) or (
                raw.lower() == "bearer"
            )
            item = redactor.item(raw)
        item, cut = _cap(item)
        truncated = truncated or cut
        if len(items) >= MAX_ARGV_ITEMS or total + len(item) > MAX_ARGV_CHARS:
            truncated = True
            break
        items.append(item)
        total += len(item)
    executable = _executable_name(argv[0], redactor)
    return RedactedCommand(
        executable=executable,
        argv=tuple(items),
        argument_count=len(argv) - 1,
        redacted=redactor.redacted,
        truncated=truncated,
        paths_rewritten=redactor.paths_rewritten,
    )


def _executable_name(argv0: str, redactor: _Redactor) -> str:
    """The program's file name only; its directory is never recorded."""
    name = re.split(r"[\\/]", argv0)[-1] or argv0
    name, _ = _cap(redactor.scrub(name))
    return name or REDACTED


def default_home() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None
