"""Read-only Git detection and a bounded, shell-free Git command runner.

Only read-only Git commands are ever passed to :func:`run_git`. Commands run without a
shell, with a fixed executable, a timeout and a cap on captured output, and with
``GIT_OPTIONAL_LOCKS=0`` so even ``git status`` never rewrites the index. Lazy fetching
of missing promisor objects is disabled (``GIT_NO_LAZY_FETCH=1``), so observation never
contacts a remote merely because an object is absent.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from agentbodycam.models.environment import ToolStatus

_VERSION_TIMEOUT_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024

# Variables that would point Git at another repository, index or object store than the
# one discovered from the project directory, or make it write trace files.
_REDIRECTING_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_EXTERNAL_DIFF",
    }
)
_FORCED_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",  # never take index.lock / write a refreshed index
    "GIT_TERMINAL_PROMPT": "0",  # never prompt for anything
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",  # stable, untranslated error messages
    "LANGUAGE": "C",
    # Partial clones must not demand-fetch missing objects during observation.
    "GIT_NO_LAZY_FETCH": "1",
}
# Configuration overrides applied to every command: no fsmonitor hook/daemon, no colour,
# and no transport that could retrieve objects from a remote during observation.
_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "color.ui=never",
    "-c",
    "protocol.http.allow=never",
    "-c",
    "protocol.https.allow=never",
    "-c",
    "protocol.git.allow=never",
    "-c",
    "protocol.ssh.allow=never",
    "-c",
    "protocol.file.allow=never",
)


def find_git_executable(path_env: str | None = None) -> str | None:
    """Absolute path of the first trusted ``git`` executable on PATH, or ``None``.

    Unlike :func:`shutil.which`, the current directory is never searched (Windows would
    otherwise run a ``git.exe`` / ``git.cmd`` / ``git.bat`` planted in the project) and
    relative PATH entries are skipped. On Windows only ``git.exe`` is accepted — never a
    project-local ``git.cmd`` or ``git.bat``.
    """
    search = os.environ.get("PATH", "") if path_env is None else path_env
    name = "git.exe" if sys.platform == "win32" else "git"
    for entry in search.split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not Path(entry).is_absolute():
            continue
        candidate = Path(entry) / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        # Defense in depth: never treat a shell-script shim as Git.
        if candidate.suffix.lower() in {".cmd", ".bat", ".com"}:
            continue
        return str(candidate)
    return None


def detect_git() -> ToolStatus:
    """Report whether a trusted ``git`` executable is on PATH and its version, if obtainable.

    Uses :func:`find_git_executable` — the same policy as the recorder — so a project-local
    ``git.cmd`` / ``git.bat`` / relative PATH entry is never selected or executed.
    """
    path = find_git_executable()
    if path is None:
        return ToolStatus(name="git", available=False)

    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ToolStatus(name="git", available=True, path=path)

    output = completed.stdout.strip()
    version = output if completed.returncode == 0 and output else None
    return ToolStatus(name="git", available=True, path=path, version=version)


def git_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment Git commands run with: the caller's, minus redirections, plus safety."""
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if key.upper() not in _REDIRECTING_ENV and not key.upper().startswith("GIT_TRACE")
    }
    env.update(_FORCED_ENV)
    return env


@dataclass(frozen=True, slots=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitRunError(Exception):
    """A Git command could not run to completion (timeout, oversized output, OS error)."""

    def __init__(self, kind: str, detail: str | None = None, errno: int | None = None) -> None:
        super().__init__(kind if detail is None else f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.errno = errno


class _Collector:
    """Reads a pipe to EOF, keeping at most ``limit`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int, on_overflow: Callable[[], None]) -> None:
        self.chunks: list[bytes] = []
        self.size = 0
        self.overflow = False
        self._stream = stream
        self._limit = limit
        self._on_overflow = on_overflow
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with contextlib.suppress(OSError, ValueError):  # pipe closed under us
            while chunk := self._stream.read(_READ_CHUNK_BYTES):
                if self.overflow:
                    continue
                self.size += len(chunk)
                if self.size > self._limit:
                    self.overflow = True
                    self._on_overflow()
                    continue
                self.chunks.append(chunk)

    def data(self) -> bytes:
        return b"".join(self.chunks)


def _creation_flags() -> int:
    flags = 0
    if sys.platform == "win32":
        # No console: Ctrl+C / Ctrl+Break aimed at the recorder never reach Git.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


def run_git(
    executable: str,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    max_output: int,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    """Run ``executable <safe config> *args`` in ``cwd`` without a shell.

    Raises :class:`GitRunError` with kind ``timeout``, ``output_too_large`` or ``os_error``.
    A non-zero exit status is *not* an error here; callers inspect ``returncode``.
    """
    try:
        proc = subprocess.Popen(
            [executable, *_SAFE_CONFIG, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(env),
            creationflags=_creation_flags(),
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        raise GitRunError("os_error", exc.strerror, exc.errno) from None

    def kill() -> None:
        with contextlib.suppress(OSError):
            proc.kill()

    if proc.stdout is None or proc.stderr is None:  # pragma: no cover - PIPE always set
        kill()
        raise GitRunError("os_error", "no output pipes")
    out = _Collector(proc.stdout, max_output, kill)
    err = _Collector(proc.stderr, MAX_STDERR_BYTES, lambda: None)
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill()
        proc.wait()
        raise GitRunError("timeout", f"no result within {timeout:g}s") from None
    except BaseException:  # e.g. KeyboardInterrupt: never leave Git running behind us
        kill()
        proc.wait()
        raise
    finally:
        for collector in (out, err):
            collector.thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            stream.close()
    if out.overflow:
        raise GitRunError("output_too_large", f"more than {max_output} bytes of output")
    return GitResult(returncode, out.data(), err.data())
