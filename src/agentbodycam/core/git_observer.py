"""Read-only observation of a project's Git state for the universal recorder.

The observer runs a small, fixed set of read-only Git commands (``rev-parse``, ``status``,
``merge-base --is-ancestor``, ``log``) and turns their machine-readable output into a
structured snapshot. It never commits, checks out, resets, stashes, fetches, pushes or
changes configuration, never writes the index (``GIT_OPTIONAL_LOCKS=0``), never lazily
fetches missing promisor objects (``GIT_NO_LAZY_FETCH=1``), and never reads remote URLs,
``.git/config``, credentials or file contents. Missing promised objects yield unavailable
or problem evidence — never network access.

Snapshots say *what* the Git state was when observed; they never say who changed it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agentbodycam.core.ignore import DEFAULT_IGNORE_POLICY, IgnorePolicy
from agentbodycam.models.events import (
    EventValidationError,
    JsonObject,
    JsonValue,
    dumps_canonical,
    validate_relative_path,
)
from agentbodycam.utils.git import GitResult, GitRunError, find_git_executable, run_git

DEFAULT_GIT_INTERVAL_SECONDS = 2.0
STATUS_TIMEOUT_SECONDS = 15.0
QUERY_TIMEOUT_SECONDS = 5.0
MAX_STATUS_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_QUERY_OUTPUT_BYTES = 256 * 1024
# Per-list cap on paths written into one event; counts always cover every path.
MAX_LISTED_PATHS = 200
MAX_PAYLOAD_BYTES = 160 * 1024
MAX_COMMITS_LISTED = 20
MAX_SUBJECT_CHARS = 200
MAX_DETAIL_CHARS = 300
SHORT_SHA = 7

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_STORE_EXCLUDE = ":(exclude).agentbodycam"

_CHANGE_CODES = {
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
}
_CONFLICT_CODES = {
    "DD": "both_deleted",
    "AU": "added_by_us",
    "UD": "deleted_by_them",
    "UA": "added_by_them",
    "DU": "deleted_by_us",
    "AA": "both_added",
    "UU": "both_modified",
}


class GitUnavailable(StrEnum):
    GIT_NOT_FOUND = "git_not_found"
    NOT_A_REPOSITORY = "not_a_repository"


@dataclass(frozen=True, slots=True)
class GitEntry:
    """One path in the index or working tree, relative to the project root."""

    path: str
    status: str
    original_path: str | None = None

    def evidence(self) -> JsonObject:
        data: JsonObject = {"path": self.path, "status": self.status}
        if self.original_path is not None:
            data["original_path"] = self.original_path
        return data


@dataclass(frozen=True, slots=True)
class GitState:
    """Git state of the project as observed by one ``git status``. Never holds contents."""

    root: str
    prefix: str  # project directory relative to the repository root ("" if the same)
    worktree: str  # "main" or "linked"
    commit: str | None
    branch: str | None
    detached: bool
    upstream: str | None
    ahead: int | None
    behind: int | None
    staged: tuple[GitEntry, ...] = ()
    unstaged: tuple[GitEntry, ...] = ()
    untracked: tuple[str, ...] = ()
    conflicted: tuple[GitEntry, ...] = ()
    redacted: int = 0  # sensitive paths counted but not named
    unrepresentable: int = 0  # paths that cannot be stored as project-relative paths

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicted)

    def payload(self) -> JsonObject:
        """Canonical, size-bounded snapshot payload (identical states give equal payloads)."""
        limit = MAX_LISTED_PATHS
        while True:
            data = self._payload(limit)
            if limit == 0 or len(dumps_canonical(data).encode("utf-8")) <= MAX_PAYLOAD_BYTES:
                return data
            limit //= 2

    def _payload(self, limit: int) -> JsonObject:
        def bounded(items: Sequence[JsonValue]) -> list[JsonValue]:
            return list(items[:limit])

        truncated = any(
            len(items) > limit
            for items in (self.staged, self.unstaged, self.untracked, self.conflicted)
        )
        upstream: JsonValue = None
        if self.upstream is not None:
            upstream = {"name": self.upstream, "ahead": self.ahead, "behind": self.behind}
        return {
            "repository": True,
            "root": self.root,
            "project_prefix": self.prefix,
            "worktree": self.worktree,
            "head": {
                "commit": self.commit,
                "branch": self.branch,
                "detached": self.detached,
                "unborn": self.commit is None,
            },
            "upstream": upstream,
            "state": "clean" if self.clean else "dirty",
            "counts": {
                "staged": len(self.staged),
                "unstaged": len(self.unstaged),
                "untracked": len(self.untracked),
                "conflicted": len(self.conflicted),
            },
            "staged": bounded([e.evidence() for e in self.staged if not _redacted(e)]),
            "unstaged": bounded([e.evidence() for e in self.unstaged if not _redacted(e)]),
            "untracked": bounded([p for p in self.untracked if p != _REDACTED]),
            "conflicted": bounded([e.evidence() for e in self.conflicted if not _redacted(e)]),
            "listed_paths_truncated": truncated,
            "redacted_paths": self.redacted,
            "unrepresentable_paths": self.unrepresentable,
        }


# Placeholder for a sensitive path: it is counted but its name is never kept.
_REDACTED = "\x00redacted"


def _redacted(entry: GitEntry) -> bool:
    return entry.path == _REDACTED


def unavailable_payload(reason: GitUnavailable) -> JsonObject:
    return {"repository": False, "reason": reason.value}


@dataclass(frozen=True, slots=True)
class GitProblem:
    """A Git failure, described with safe metadata only (no absolute paths, no URLs)."""

    operation: str
    error: str
    exit_code: int | None = None
    errno: int | None = None
    detail: str | None = None

    def key(self) -> tuple[str, str, int | None]:
        return (self.operation, self.error, self.exit_code)

    def payload(self) -> JsonObject:
        return {
            "operation": self.operation,
            "error": self.error,
            "exit_code": self.exit_code,
            "errno": self.errno,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class GitObservation:
    """Exactly one of ``state``, ``unavailable`` or ``problem`` is set."""

    state: GitState | None = None
    unavailable: GitUnavailable | None = None
    problem: GitProblem | None = None


class _Failed(Exception):
    def __init__(self, problem: GitProblem) -> None:
        super().__init__(problem.error)
        self.problem = problem


class _NotARepository(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Layout:
    root: str
    prefix: str
    worktree: str


def short_sha(commit: str | None) -> str:
    return "(none)" if commit is None else commit[:SHORT_SHA]


class GitObserver:
    """Observes the Git repository containing ``project_root``. Read-only."""

    def __init__(
        self,
        project_root: Path,
        *,
        executable: str | None,
        policy: IgnorePolicy = DEFAULT_IGNORE_POLICY,
        env: Mapping[str, str] | None = None,
        status_timeout: float = STATUS_TIMEOUT_SECONDS,
        query_timeout: float = QUERY_TIMEOUT_SECONDS,
        max_status_output: int = MAX_STATUS_OUTPUT_BYTES,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.executable = executable
        self.policy = policy
        self._env = env
        self.status_timeout = status_timeout
        self.query_timeout = query_timeout
        self.max_status_output = max_status_output
        self._layout: _Layout | None = None

    @classmethod
    def discover(
        cls, project_root: Path, *, policy: IgnorePolicy = DEFAULT_IGNORE_POLICY
    ) -> GitObserver:
        """An observer using the ``git`` found on PATH (never the current directory)."""
        return cls(project_root, executable=find_git_executable(), policy=policy)

    # -- commands ----------------------------------------------------------------------

    def _run(self, operation: str, args: list[str], *, status: bool = False) -> GitResult:
        if self.executable is None:
            raise _Failed(GitProblem(operation, GitUnavailable.GIT_NOT_FOUND.value))
        try:
            return run_git(
                self.executable,
                args,
                cwd=self.project_root,
                timeout=self.status_timeout if status else self.query_timeout,
                max_output=self.max_status_output if status else MAX_QUERY_OUTPUT_BYTES,
                env=self._env,
            )
        except GitRunError as exc:
            detail = None if exc.detail is None else self._sanitize(exc.detail)
            raise _Failed(GitProblem(operation, exc.kind, None, exc.errno, detail)) from None

    def _check(self, operation: str, result: GitResult) -> GitResult:
        if result.returncode == 0:
            return result
        message = result.stderr.decode("utf-8", "replace")
        if "not a git repository" in message.casefold():
            raise _NotARepository
        raise _Failed(
            GitProblem(
                operation, "command_failed", result.returncode, None, self._sanitize(message)
            )
        )

    def _sanitize(self, text: str) -> str | None:
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not line:
            return None
        for form in (str(self.project_root), self.project_root.as_posix()):
            line = line.replace(form, "<project>")
        if self._layout is not None:
            for form in (self._layout.root, Path(self._layout.root).as_posix()):
                line = line.replace(form, "<repository>")
        line = _URL_CREDENTIALS.sub(r"\1<redacted>@", line)
        line = "".join(ch if ch.isprintable() else "?" for ch in line)
        return line[:MAX_DETAIL_CHARS]

    # -- observation -------------------------------------------------------------------

    def observe(self) -> GitObservation:
        """One read-only look at the repository. Never raises for Git or OS failures."""
        if self.executable is None:
            return GitObservation(unavailable=GitUnavailable.GIT_NOT_FOUND)
        try:
            if self._layout is None:
                self._layout = self._discover_layout()
            result = self._check(
                "status",
                self._run(
                    "status",
                    [
                        "status",
                        "--porcelain=v2",
                        "--branch",
                        "-z",
                        "--untracked-files=all",
                        "--ignore-submodules=all",
                        "--renames",
                        "--",
                        ".",
                        _STORE_EXCLUDE,
                    ],
                    status=True,
                ),
            )
            return GitObservation(state=self._parse_status(result.stdout, self._layout))
        except _NotARepository:
            self._layout = None
            return GitObservation(unavailable=GitUnavailable.NOT_A_REPOSITORY)
        except _Failed as exc:
            return GitObservation(problem=exc.problem)

    def _discover_layout(self) -> _Layout:
        args = ["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"]
        result = self._check("rev-parse", self._run("rev-parse", args))
        lines = result.stdout.decode("utf-8", "replace").splitlines()
        prefix_result = self._check(
            "rev-parse", self._run("rev-parse", ["rev-parse", "--show-prefix"])
        )
        prefix = prefix_result.stdout.decode("utf-8", "replace").strip("\r\n").rstrip("/")
        if len(lines) != 3 or not all(lines):
            raise _Failed(GitProblem("rev-parse", "unparseable_output"))
        top, git_dir, common_dir = lines
        common = Path(common_dir)
        if not common.is_absolute():
            common = self.project_root / common
        same = _resolve(Path(git_dir)) == _resolve(common)
        return _Layout(root=str(Path(top)), prefix=prefix, worktree="main" if same else "linked")

    def _project_path(self, raw: bytes, prefix: str) -> str | None:
        """Project-relative path for a repository-relative path from Git, or ``None``."""
        text = raw.decode("utf-8", "surrogateescape")
        if prefix:
            head = text[: len(prefix) + 1]
            if head != f"{prefix}/" and head.casefold() != f"{prefix}/".casefold():
                return None
            text = text[len(prefix) + 1 :]
        try:
            validate_relative_path(text)
            text.encode("utf-8")
        except (EventValidationError, UnicodeEncodeError):
            return None
        return text

    def _sensitive(self, path: str) -> bool:
        return any(self.policy.is_sensitive(part) for part in path.split("/"))

    def _parse_status(self, output: bytes, layout: _Layout) -> GitState:
        commit: str | None = None
        branch: str | None = None
        detached = False
        upstream: str | None = None
        ahead: int | None = None
        behind: int | None = None
        staged: list[GitEntry] = []
        unstaged: list[GitEntry] = []
        untracked: list[str] = []
        conflicted: list[GitEntry] = []
        redacted = unrepresentable = 0

        def resolve(raw: bytes) -> str | None:
            nonlocal redacted, unrepresentable
            path = self._project_path(raw, layout.prefix)
            if path is None:
                unrepresentable += 1
                return None
            if self._sensitive(path):
                redacted += 1
                return _REDACTED
            return path

        records = output.split(b"\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            kind = record[:2]
            if record.startswith(b"# "):
                key, _, value = record[2:].decode("utf-8", "replace").partition(" ")
                if key == "branch.oid":
                    commit = value if _SHA.match(value) else None
                elif key == "branch.head":
                    detached = value == "(detached)"
                    branch = None if detached else value
                elif key == "branch.upstream":
                    upstream = value
                elif key == "branch.ab":
                    ahead, behind = _ahead_behind(value)
                continue
            if kind == b"? ":
                path = resolve(record[2:])
                if path is not None:
                    untracked.append(path)
                continue
            if kind in (b"1 ", b"2 ", b"u "):
                fields = record.split(b" ", {b"1 ": 8, b"2 ": 9, b"u ": 10}[kind])
                original: bytes | None = None
                if kind == b"2 ":
                    original = records[index] if index < len(records) else None
                    index += 1
                if len(fields) < 3:
                    unrepresentable += 1
                    continue
                xy = fields[1].decode("ascii", "replace")
                path = resolve(fields[-1])
                if path is None:
                    continue
                if kind == b"u ":
                    conflicted.append(GitEntry(path, _CONFLICT_CODES.get(xy, "unmerged")))
                    continue
                original_path = None
                if original is not None and path != _REDACTED:
                    original_path = self._project_path(original, layout.prefix)
                    if original_path is not None and self._sensitive(original_path):
                        original_path = None
                for code, bucket in ((xy[:1], staged), (xy[1:2], unstaged)):
                    if code and code != ".":
                        renamed = code in ("R", "C")
                        bucket.append(
                            GitEntry(
                                path,
                                _CHANGE_CODES.get(code, "unknown"),
                                original_path if renamed else None,
                            )
                        )
        return GitState(
            root=layout.root,
            prefix=layout.prefix,
            worktree=layout.worktree,
            commit=commit,
            branch=branch,
            detached=detached,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            staged=tuple(sorted(staged, key=_entry_key)),
            unstaged=tuple(sorted(unstaged, key=_entry_key)),
            untracked=tuple(sorted(untracked)),
            conflicted=tuple(sorted(conflicted, key=_entry_key)),
            redacted=redacted,
            unrepresentable=unrepresentable,
        )

    # -- HEAD changes ------------------------------------------------------------------

    def head_change(self, previous: str | None, current: str | None) -> JsonObject:
        """Factual description of a HEAD move: relation and the commits it brought in."""
        data: JsonObject = {"previous": previous, "current": current}
        if current is None or not _SHA.match(current):
            data.update(relation="unborn", commits=[], commits_truncated=False)
            return data
        if previous is not None and not _SHA.match(previous):
            previous = None
        try:
            relation = "no_previous_commit"
            if previous is not None:
                relation = self._relation(previous, current)
            exclude = previous if relation in ("descendant", "ancestor", "diverged") else None
            commits, truncated = self._commits(current, exclude, relation == "unknown")
        except _NotARepository:
            data.update(relation="unknown", commits=None, commits_error="not_a_repository")
            return data
        except _Failed as exc:
            data.update(relation="unknown", commits=None, commits_error=exc.problem.error)
            return data
        data.update(relation=relation, commits=commits, commits_truncated=truncated)
        return data

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        args = ["merge-base", "--is-ancestor", ancestor, descendant]
        result = self._run("merge-base", args)
        if result.returncode in (0, 1):
            return result.returncode == 0
        return None

    def _relation(self, previous: str, current: str) -> str:
        forward = self._is_ancestor(previous, current)
        if forward is None:
            return "unknown"
        if forward:
            return "descendant"
        backward = self._is_ancestor(current, previous)
        if backward is None:
            return "unknown"
        return "ancestor" if backward else "diverged"

    def _commits(
        self, current: str, exclude: str | None, only_current: bool
    ) -> tuple[list[JsonValue], bool]:
        limit = 1 if only_current else MAX_COMMITS_LISTED + 1
        args = [
            "log",
            "-z",
            "--no-color",
            "--no-decorate",
            "--no-show-signature",
            "--encoding=UTF-8",
            "--format=%H%x1f%s",
            f"--max-count={limit}",
            current,
        ]
        if exclude is not None:
            args.append(f"^{exclude}")
        args.append("--")
        result = self._check("log", self._run("log", args))
        commits: list[JsonValue] = []
        for record in result.stdout.split(b"\0"):
            sha, sep, subject = record.decode("utf-8", "replace").partition("\x1f")
            sha = sha.strip()
            if not sep or not _SHA.match(sha):
                continue
            clean = "".join(ch if ch.isprintable() else " " for ch in subject).strip()
            commits.append({"commit": sha, "subject": clean[:MAX_SUBJECT_CHARS]})
        truncated = len(commits) > MAX_COMMITS_LISTED
        return commits[:MAX_COMMITS_LISTED], truncated


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _entry_key(entry: GitEntry) -> tuple[str, str]:
    return (entry.path, entry.status)


def _ahead_behind(value: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"\+(\d+) -(\d+)", value.strip())
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


# -- summaries ---------------------------------------------------------------------------


def _head(payload: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    head = payload.get("head")
    return head if isinstance(head, dict) else {}


def _str_or_none(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _counts_text(payload: Mapping[str, JsonValue]) -> str:
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        return ""
    parts = [f"{counts.get(name, 0)} {name}" for name in ("staged", "unstaged", "untracked")]
    if counts.get("conflicted"):
        parts.append(f"{counts['conflicted']} conflicted")
    return ", ".join(parts)


def _position(payload: Mapping[str, JsonValue]) -> str:
    head = _head(payload)
    commit = _str_or_none(head.get("commit"))
    if head.get("detached"):
        return f"detached HEAD at {short_sha(commit)}"
    branch = _str_or_none(head.get("branch")) or "(unknown)"
    if commit is None:
        return f"on branch {branch} (no commits yet)"
    return f"on branch {branch} at {short_sha(commit)}"


def _tree_text(payload: Mapping[str, JsonValue]) -> str:
    if payload.get("state") == "clean":
        return "working tree clean"
    return f"working tree dirty ({_counts_text(payload)})"


def describe_snapshot(
    previous: Mapping[str, JsonValue] | None, current: Mapping[str, JsonValue]
) -> tuple[str, list[str]]:
    """Factual one-line summary and the list of top-level fields that changed."""
    if not current.get("repository"):
        reason = current.get("reason")
        return f"Git repository no longer observable ({reason})", ["repository"]
    if previous is None or not previous.get("repository"):
        text = f"Git repository: {_position(current)}, {_tree_text(current)}"
        return text, ["repository"]

    changed = sorted(key for key in current if current.get(key) != previous.get(key))
    parts: list[str] = []
    old_head, new_head = _head(previous), _head(current)
    old_commit = _str_or_none(old_head.get("commit"))
    new_commit = _str_or_none(new_head.get("commit"))
    if (old_head.get("branch"), old_head.get("detached")) != (
        new_head.get("branch"),
        new_head.get("detached"),
    ):
        if new_head.get("detached"):
            parts.append(f"HEAD detached at {short_sha(new_commit)}")
        else:
            old = _str_or_none(old_head.get("branch")) or "detached HEAD"
            parts.append(f"branch changed {old} -> {new_head.get('branch')}")
    if old_commit != new_commit:
        parts.append(f"HEAD changed {short_sha(old_commit)} -> {short_sha(new_commit)}")
    if previous.get("state") != current.get("state"):
        parts.append(f"working tree became {current.get('state')}")
    elif any(key in changed for key in ("counts", "staged", "unstaged", "untracked", "conflicted")):
        parts.append(f"Git status changed ({_counts_text(current)})")
    if "upstream" in changed:
        parts.append("upstream tracking changed")
    if not parts:
        parts.append("Git state changed")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:], changed
