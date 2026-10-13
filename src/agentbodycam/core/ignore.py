"""Default ignore policy for the universal recorder.

Matching is by a single path component (a file or directory name), case-insensitively, so
``.ENV`` is treated like ``.env`` on every platform. A matching directory is pruned: nothing
below it is listed, stat-ed or hashed. Ignored entries never appear in any event.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

# Directories (or files) with these exact names are never descended into or recorded.
DEFAULT_IGNORED_NAMES: frozenset[str] = frozenset(
    {
        ".agentbodycam",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
    }
)

# Likely secrets. Matching paths are skipped without being opened, hashed or recorded.
DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
)

# Editor swap/backup files and OS metadata that churn constantly and carry no evidence.
DEFAULT_NOISE_PATTERNS: tuple[str, ...] = (
    "*.swp",
    "*.swo",
    "*.swx",
    "*~",
    ".#*",
    ".DS_Store",
    "Thumbs.db",
)


@dataclass(frozen=True, slots=True)
class IgnorePolicy:
    ignored_names: frozenset[str] = DEFAULT_IGNORED_NAMES
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    noise_patterns: tuple[str, ...] = DEFAULT_NOISE_PATTERNS

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ignored_names", frozenset(n.casefold() for n in self.ignored_names)
        )
        object.__setattr__(
            self, "sensitive_patterns", tuple(p.casefold() for p in self.sensitive_patterns)
        )
        object.__setattr__(self, "noise_patterns", tuple(p.casefold() for p in self.noise_patterns))

    def is_sensitive(self, name: str) -> bool:
        folded = name.casefold()
        return any(fnmatchcase(folded, pattern) for pattern in self.sensitive_patterns)

    def is_ignored(self, name: str) -> bool:
        """True if an entry with this name must be skipped (pruned if it is a directory)."""
        folded = name.casefold()
        if folded in self.ignored_names:
            return True
        return self.is_sensitive(name) or any(
            fnmatchcase(folded, pattern) for pattern in self.noise_patterns
        )


DEFAULT_IGNORE_POLICY = IgnorePolicy()
