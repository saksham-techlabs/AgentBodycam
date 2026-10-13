from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentbodycam.core.filelock import (
    FileLock,
    FileLockError,
    LockTimeoutError,
    UnsafeLockFileError,
    is_locked,
)
from recording_helpers import hard_kill_pid, try_symlink

HOLDER = r"""
import os, sys, time
from pathlib import Path
from agentbodycam.core.filelock import FileLock
lock = FileLock(Path(sys.argv[1]))
lock.acquire(timeout=10)
print("locked", os.getpid(), flush=True)
time.sleep(120)
"""


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "dir with spaces ünïcødé" / "test.lock"


@pytest.fixture(autouse=True)
def _lock_dir(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)


def held(lock: FileLock) -> bool:
    return lock.locked


def test_acquire_and_release(lock_path: Path) -> None:
    lock = FileLock(lock_path)
    assert not held(lock)
    lock.acquire()
    assert held(lock)
    assert lock_path.is_file()
    assert lock_path.read_bytes() == b""  # readable while held, and never written to
    lock.release()
    assert not held(lock)
    lock.release()  # idempotent
    assert lock_path.exists()  # lock files are never deleted


def test_context_manager_releases_on_error(lock_path: Path) -> None:
    lock = FileLock(lock_path)
    with pytest.raises(RuntimeError), lock:
        assert lock.locked
        raise RuntimeError("boom")
    assert not lock.locked
    assert FileLock(lock_path).try_acquire()


def test_two_handles_contend_within_one_process(lock_path: Path) -> None:
    first, second = FileLock(lock_path), FileLock(lock_path)
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_lock_is_not_reentrant(lock_path: Path) -> None:
    lock = FileLock(lock_path)
    with lock, pytest.raises(FileLockError, match="already held"):
        lock.acquire()


def test_timeout_is_bounded_and_clear(lock_path: Path) -> None:
    holder = FileLock(lock_path)
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeoutError, match=r"timed out after 0\.3s") as exc:
            FileLock(lock_path, timeout=0.3).acquire()
        elapsed = time.monotonic() - started
    finally:
        holder.release()
    assert exc.value.path == lock_path
    assert 0.25 <= elapsed < 5


def test_zero_timeout_is_a_single_attempt(lock_path: Path) -> None:
    with FileLock(lock_path):
        started = time.monotonic()
        with pytest.raises(LockTimeoutError):
            FileLock(lock_path).acquire(timeout=0)
        assert time.monotonic() - started < 1


def test_blocking_acquire_waits_for_release(lock_path: Path) -> None:
    holder = FileLock(lock_path)
    holder.acquire()
    timer = threading.Timer(0.2, holder.release)
    timer.start()
    try:
        waiter = FileLock(lock_path, timeout=10)
        started = time.monotonic()
        waiter.acquire()
        assert time.monotonic() - started >= 0.1
        waiter.release()
    finally:
        timer.join()


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan")])
def test_invalid_timeouts_rejected(lock_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        FileLock(lock_path, timeout=timeout)
    with pytest.raises(ValueError, match="timeout"):
        FileLock(lock_path).acquire(timeout=timeout)


def test_probe(lock_path: Path) -> None:
    assert not is_locked(lock_path)
    assert not lock_path.exists()  # the probe never creates the file
    with FileLock(lock_path):
        assert is_locked(lock_path)
    assert not is_locked(lock_path)


def test_create_false_requires_existing_file(lock_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FileLock(lock_path, create=False).acquire()
    assert not lock_path.exists()


def test_threads_are_mutually_excluded(lock_path: Path) -> None:
    counter = lock_path.parent / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(25):
                with FileLock(lock_path, timeout=30):
                    value = int(counter.read_text(encoding="utf-8"))
                    counter.write_text(str(value + 1), encoding="utf-8")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert counter.read_text(encoding="utf-8") == str(6 * 25)


def test_directory_is_not_a_lock_file(lock_path: Path) -> None:
    lock_path.mkdir()
    with pytest.raises(UnsafeLockFileError, match="not a regular file"):
        FileLock(lock_path).acquire()
    with pytest.raises(UnsafeLockFileError):
        is_locked(lock_path)


def test_symlinked_lock_file_rejected(lock_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(b"")
    if not try_symlink(lock_path, target):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafeLockFileError, match="symlink"):
        FileLock(lock_path).acquire()
    with pytest.raises(UnsafeLockFileError):
        is_locked(lock_path)


def test_dangling_symlink_is_not_created_through(lock_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "created-through-link.lock"
    if not try_symlink(lock_path, target):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafeLockFileError):
        FileLock(lock_path).acquire()
    assert not target.exists()


# -- real separate processes -------------------------------------------------------------


def _start_holder(lock_path: Path) -> tuple[subprocess.Popen[str], int]:
    """Start a process holding the lock; returns it and the PID of the real interpreter."""
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None
    word, pid = proc.stdout.readline().split()
    assert word == "locked"
    return proc, int(pid)


def _hard_kill(proc: subprocess.Popen[str], pid: int) -> None:
    """SIGKILL / TerminateProcess: no cleanup code runs in the lock holder."""
    if pid != proc.pid:  # a Windows venv python.exe is a launcher for the real interpreter
        hard_kill_pid(pid)
    proc.kill()
    proc.wait(timeout=30)


def test_lock_held_by_another_process(lock_path: Path) -> None:
    proc, pid = _start_holder(lock_path)
    try:
        assert is_locked(lock_path)
        assert not FileLock(lock_path).try_acquire()
        with pytest.raises(LockTimeoutError):
            FileLock(lock_path, timeout=0.2).acquire()
    finally:
        _hard_kill(proc, pid)


def test_killed_process_releases_lock(lock_path: Path) -> None:
    proc, pid = _start_holder(lock_path)
    try:
        assert is_locked(lock_path)
    finally:
        _hard_kill(proc, pid)
    lock = FileLock(lock_path, timeout=10)
    lock.acquire()  # bounded wait: Windows may take a moment to drop a dead owner's lock
    assert lock.locked
    lock.release()
    assert not is_locked(lock_path)
