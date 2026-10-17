"""Small cross-platform, standard-library-only exclusive file lock.

The lock is an OS lock on a dedicated, empty lock file: ``fcntl.flock`` on POSIX, and
``msvcrt.locking`` (``LockFile``) on one byte far beyond end-of-file on Windows, where
byte-range locks are mandatory and would otherwise make the file unreadable to others.
Both are owned by the open handle, so

- two :class:`FileLock` objects contend even inside one process (on every platform),
- the operating system releases the lock when the handle is closed or the owning process
  dies, however it dies. A lock file left on disk therefore means nothing by itself; only
  a held OS lock does.

Lock files are never deleted: removing a lock file while another process has it open would
let two processes "hold" locks on different files with the same name.

Waiting is done by retrying a non-blocking attempt with exponential backoff (1 ms up to
50 ms), always bounded by a timeout. There is no fairness guarantee between waiters.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
import threading
import time
import weakref
from pathlib import Path
from types import TracebackType

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

DEFAULT_TIMEOUT_SECONDS = 10.0
_MIN_BACKOFF_SECONDS = 0.001
_MAX_BACKOFF_SECONDS = 0.05
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_WINDOWS_LOCK_OFFSET = 1 << 30


class FileLockError(Exception):
    """Base class for file lock failures."""


class LockTimeoutError(FileLockError):
    """The lock is held elsewhere and could not be acquired within the timeout."""

    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__(f"timed out after {timeout:g}s waiting for lock: {path}")
        self.path = path
        self.timeout = timeout


class UnsafeLockFileError(FileLockError):
    """The lock path is a symlink, junction or not a regular file."""


def _is_link(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _check_lstat(path: Path) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if _is_link(st):
        raise UnsafeLockFileError(f"refusing to use a symlinked lock file: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeLockFileError(f"lock path is not a regular file: {path}")
    return st


def open_lock_file(path: Path, *, create: bool) -> int:
    """Open ``path`` without following links and verify the handle is that very file.

    Raises :class:`FileNotFoundError` if it is missing and ``create`` is false.
    """
    _check_lstat(path)
    flags = os.O_RDWR
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_BINARY", "O_NOINHERIT"):
        flags |= getattr(os, name, 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        listed = _check_lstat(path)
        if (
            listed is None
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino)
        ):
            raise UnsafeLockFileError(f"lock file changed while it was being opened: {path}")
        # Owner-only on POSIX so lock files cannot become 0644 under a permissive umask.
        if os.name == "posix" and stat.S_IMODE(opened.st_mode) != 0o600:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is None:
                raise UnsafeLockFileError(f"fchmod is required for lock file permissions: {path}")
            fchmod(fd, 0o600)
            opened = os.fstat(fd)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise UnsafeLockFileError(f"cannot set owner-only permissions on lock file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


if sys.platform == "win32":

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _close_quietly(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


class FileLock:
    """Exclusive OS lock on ``path``. Not reentrant; safe to share between threads.

    ``with FileLock(path):`` blocks for at most ``timeout`` seconds, then raises
    :class:`LockTimeoutError`.
    """

    def __init__(
        self, path: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, create: bool = True
    ) -> None:
        _validate_timeout(timeout)
        self.path = Path(path)
        self.timeout = timeout
        self._create = create
        self._fd: int | None = None
        self._finalizer: weakref.finalize[[int], FileLock] | None = None
        self._guard = threading.Lock()

    @property
    def locked(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    def acquire(self, timeout: float | None = None) -> None:
        """Wait up to ``timeout`` seconds (default: the constructor's) for the lock.

        ``timeout=0`` makes a single non-blocking attempt.
        """
        wait = self.timeout if timeout is None else timeout
        _validate_timeout(wait)
        if not self._acquire(wait):
            raise LockTimeoutError(self.path, wait)

    def try_acquire(self) -> bool:
        """One non-blocking attempt. Returns whether the lock is now held."""
        return self._acquire(0.0)

    def _acquire(self, timeout: float) -> bool:
        with self._guard:
            if self._fd is not None:
                raise FileLockError(f"lock is already held by this FileLock: {self.path}")
            fd = open_lock_file(self.path, create=self._create)
            try:
                deadline = time.monotonic() + timeout
                backoff = _MIN_BACKOFF_SECONDS
                while not _try_lock(fd):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _close_quietly(fd)
                        return False
                    time.sleep(min(backoff, remaining))
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
            except BaseException:
                _close_quietly(fd)
                raise
            self._fd = fd
            # Closing the handle releases the OS lock if the owner forgets to.
            self._finalizer = weakref.finalize(self, _close_quietly, fd)
            return True

    def release(self) -> None:
        """Release the lock. Idempotent."""
        with self._guard:
            fd, self._fd = self._fd, None
            finalizer, self._finalizer = self._finalizer, None
            if fd is None:
                return
            if finalizer is not None:
                finalizer.detach()
            try:
                _unlock(fd)
            finally:
                _close_quietly(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def is_locked(path: Path) -> bool:
    """Non-blocking probe: is ``path`` currently locked by some other handle?

    Never creates the lock file; a missing file is reported as not locked. The probe takes
    and immediately drops the lock when it is free, so it can briefly delay an acquirer.
    """
    try:
        fd = open_lock_file(path, create=False)
    except FileNotFoundError:
        return False
    try:
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    finally:
        _close_quietly(fd)


def _validate_timeout(timeout: float) -> None:
    if not 0 <= timeout < float("inf"):
        raise ValueError(f"lock timeout must be a finite, non-negative number: {timeout!r}")
