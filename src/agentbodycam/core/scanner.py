"""Read-only project snapshots for the polling recorder.

The scanner walks the project root without following symlinks, junctions or other
link-like entries, records metadata plus a streaming SHA-256 for each regular file, and
never reads or opens anything outside the root. File contents are hashed and discarded;
they are never returned or stored.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from agentbodycam.core.ignore import DEFAULT_IGNORE_POLICY, IgnorePolicy
from agentbodycam.models.events import (
    EventValidationError,
    JsonObject,
    format_timestamp,
    validate_relative_path,
)

HASH_CHUNK_BYTES = 1024 * 1024
# Files larger than this are not hashed; their evidence is size + mtime only.
DEFAULT_MAX_HASH_BYTES = 256 * 1024 * 1024
# Consecutive scans a file may fail transiently (locked, mid-write) before it is reported.
DEFAULT_DEFERRAL_LIMIT = 2

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # read | write | delete: never block the user's editor
    _OPEN_EXISTING = 3
    _FLAG_OPEN_REPARSE_POINT = 0x00200000  # do not follow links
    _FLAG_SEQUENTIAL_SCAN = 0x08000000
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _REPARSE_TAG_NAME_SURROGATE = 0x20000000  # symlinks, junctions and other link-like tags
    _MAX_PATH = 260

    def _open_no_follow(path: str) -> int:
        if len(path) >= _MAX_PATH:
            path = "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
        handle = _CreateFileW(
            path,
            _GENERIC_READ,
            _SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FLAG_OPEN_REPARSE_POINT | _FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _CloseHandle(handle)
            raise

    def _is_link_like(st: os.stat_result) -> bool:
        if stat.S_ISLNK(st.st_mode):
            return True
        reparse = st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        return bool(reparse and st.st_reparse_tag & _REPARSE_TAG_NAME_SURROGATE)

else:

    def _open_no_follow(path: str) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
        return os.open(path, flags)

    def _is_link_like(st: os.stat_result) -> bool:
        return stat.S_ISLNK(st.st_mode)


class HashStatus(StrEnum):
    COMPLETE = "complete"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    UNSTABLE = "unstable"


@dataclass(frozen=True, slots=True)
class FileState:
    """Observed metadata of one regular file. Never contains file contents."""

    size: int
    mtime_ns: int
    file_id: int
    sha256: str | None
    hash_status: HashStatus

    @property
    def fingerprint(self) -> tuple[int, int, int]:
        return (self.size, self.mtime_ns, self.file_id)

    def evidence(self) -> JsonObject:
        mtime = datetime.fromtimestamp(self.mtime_ns / 1_000_000_000, UTC)
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "mtime": format_timestamp(mtime),
            "sha256": self.sha256,
            "hash_status": self.hash_status.value,
        }


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A filesystem problem met while scanning. Holds safe metadata only."""

    operation: str
    error: str
    path: str | None = None
    errno: int | None = None
    detail: str | None = None

    @classmethod
    def from_os_error(cls, operation: str, path: str | None, exc: OSError) -> ScanIssue:
        # str(exc) is avoided: it embeds the absolute path (and so the user's home dir).
        return cls(operation, type(exc).__name__, path or None, exc.errno, exc.strerror)

    def key(self) -> tuple[str, str, str | None]:
        return (self.operation, self.error, self.path)


@dataclass(frozen=True, slots=True)
class ScanResult:
    files: dict[str, FileState]
    issues: list[ScanIssue]


class _Vanished(Exception):
    """The file disappeared or was replaced between listing and hashing."""


class _Unstable(Exception):
    """The file changed while it was being hashed."""


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)


def hash_file(path: str, expected: os.stat_result, max_bytes: int) -> tuple[str | None, HashStatus]:
    """Stream ``path`` through SHA-256 if it is still the regular file described by ``expected``.

    Raises :class:`_Vanished`, :class:`_Unstable` or :class:`OSError`. Never follows links.
    """
    try:
        fd = _open_no_follow(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _Vanished from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # replaced by a symlink after listing
            raise _Vanished from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(opened, expected):
            raise _Vanished
        if opened.st_size > max_bytes:
            return None, HashStatus.TOO_LARGE
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, HASH_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                return None, HashStatus.TOO_LARGE
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if total != after.st_size or (after.st_size, after.st_mtime_ns) != (
        expected.st_size,
        expected.st_mtime_ns,
    ):
        raise _Unstable
    return digest.hexdigest(), HashStatus.COMPLETE


def _representable(rel: str) -> bool:
    try:
        validate_relative_path(rel)
        rel.encode("utf-8")
    except (EventValidationError, UnicodeEncodeError):
        return False
    return True


class ProjectScanner:
    """Builds :class:`ScanResult` snapshots of a project root. Read-only."""

    def __init__(
        self,
        root: Path,
        *,
        policy: IgnorePolicy = DEFAULT_IGNORE_POLICY,
        max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
        deferral_limit: int = DEFAULT_DEFERRAL_LIMIT,
    ) -> None:
        if max_hash_bytes < 0:
            raise ValueError("max_hash_bytes must be non-negative")
        if deferral_limit < 1:
            raise ValueError("deferral_limit must be at least 1")
        self.root = Path(root).resolve()
        self.policy = policy
        self.max_hash_bytes = max_hash_bytes
        self.deferral_limit = deferral_limit
        self._failures: dict[str, int] = {}

    def scan(self, previous: Mapping[str, FileState], *, defer: bool = True) -> ScanResult:
        """Snapshot the project.

        Files that cannot be judged this scan (vanished mid-scan, locked, changing while
        hashed) keep their ``previous`` state so a transient race is never reported as a
        deletion or modification. With ``defer=False`` failures are recorded immediately.
        """
        files: dict[str, FileState] = {}
        issues: list[ScanIssue] = []
        failing: dict[str, int] = {}
        stack: list[tuple[Path, str]] = [(self.root, "")]
        while stack:
            abs_dir, rel_dir = stack.pop()
            try:
                with os.scandir(abs_dir) as entries:
                    names = sorted(entry.name for entry in entries)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                issues.append(ScanIssue.from_os_error("list_directory", rel_dir, exc))
                self._carry_subtree(previous, rel_dir, files)
                continue
            for name in names:
                if self.policy.is_ignored(name):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if not _representable(rel):
                    issues.append(ScanIssue("list_directory", "UnrepresentablePath"))
                    continue
                abs_path = abs_dir / name
                try:
                    st = abs_path.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    issues.append(ScanIssue.from_os_error("stat", rel, exc))
                    self._carry(previous, rel, files)
                    continue
                if _is_link_like(st):
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append((abs_path, rel))
                elif stat.S_ISREG(st.st_mode):
                    state = self._observe_file(
                        rel, abs_path, st, previous.get(rel), issues, failing, defer=defer
                    )
                    if state is not None:
                        files[rel] = state
        self._failures = failing
        return ScanResult(files=files, issues=issues)

    @staticmethod
    def _carry(previous: Mapping[str, FileState], rel: str, files: dict[str, FileState]) -> None:
        if rel in previous:
            files[rel] = previous[rel]

    @staticmethod
    def _carry_subtree(
        previous: Mapping[str, FileState], rel_dir: str, files: dict[str, FileState]
    ) -> None:
        prefix = f"{rel_dir}/" if rel_dir else ""
        files.update((p, s) for p, s in previous.items() if p.startswith(prefix))

    def _observe_file(
        self,
        rel: str,
        abs_path: Path,
        st: os.stat_result,
        prev: FileState | None,
        issues: list[ScanIssue],
        failing: dict[str, int],
        *,
        defer: bool,
    ) -> FileState | None:
        fingerprint = (st.st_size, st.st_mtime_ns, st.st_ino)
        if (
            prev is not None
            and prev.fingerprint == fingerprint
            and prev.hash_status in (HashStatus.COMPLETE, HashStatus.TOO_LARGE)
        ):
            return prev

        def deferred() -> bool:
            failing[rel] = self._failures.get(rel, 0) + 1
            return defer and failing[rel] < self.deferral_limit

        def fallback(status: HashStatus) -> FileState:
            return FileState(st.st_size, st.st_mtime_ns, st.st_ino, None, status)

        try:
            digest, status = hash_file(str(abs_path), st, self.max_hash_bytes)
        except _Vanished:
            return prev
        except _Unstable:
            return prev if deferred() else fallback(HashStatus.UNSTABLE)
        except OSError as exc:
            if deferred():
                return prev
            issues.append(ScanIssue.from_os_error("hash", rel, exc))
            return fallback(HashStatus.UNREADABLE)
        return FileState(st.st_size, st.st_mtime_ns, st.st_ino, digest, status)


class ChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class FileChange:
    kind: ChangeKind
    path: str
    before: FileState | None
    after: FileState | None
    # For modifications: "sha256" when both hashes differ, "metadata" when a hash is
    # unavailable on either side and size or mtime changed.
    evidence: str | None = None


def _modified(old: FileState, new: FileState) -> str | None:
    if old.sha256 is not None and new.sha256 is not None:
        return "sha256" if old.sha256 != new.sha256 else None
    if (old.size, old.mtime_ns) != (new.size, new.mtime_ns):
        return "metadata"
    return None


def diff_snapshots(
    before: Mapping[str, FileState], after: Mapping[str, FileState]
) -> list[FileChange]:
    """Changes between two snapshots: created, then modified, then deleted, each by path."""
    changes: list[FileChange] = [
        FileChange(ChangeKind.CREATED, p, None, after[p]) for p in _sorted(after, before)
    ]
    for path in sorted(before.keys() & after.keys()):
        evidence = _modified(before[path], after[path])
        if evidence is not None:
            changes.append(
                FileChange(ChangeKind.MODIFIED, path, before[path], after[path], evidence)
            )
    changes += [FileChange(ChangeKind.DELETED, p, before[p], None) for p in _sorted(before, after)]
    return changes


def _sorted(present: Iterable[str], absent: Mapping[str, FileState]) -> list[str]:
    return sorted(p for p in present if p not in absent)
