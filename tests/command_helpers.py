"""Helpers for wrapped-command tests: real recorder processes and child scripts."""

from __future__ import annotations

import io
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agentbodycam.cli import main
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventType

ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
SESSION_LINE = re.compile(r"^Session: (\S+)$", re.MULTILINE)
COMMAND_TYPES = {EventType.COMMAND_STARTED, EventType.COMMAND_FINISHED}


def cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "agentbodycam", *args]


def run_cli(*argv: str) -> tuple[int, str]:
    """In-process ``agentbodycam ...``; returns (exit code, stderr text)."""
    err = io.StringIO()
    code = main(list(argv), out=io.StringIO(), err=err)
    return code, err.getvalue()


def py(code: str, *args: str) -> list[str]:
    """argv running ``code`` with the test interpreter."""
    return [sys.executable, "-c", code, *args]


def command_events(store: EventStore, sid: str) -> list[Event]:
    return [e for e in store.read_events(sid) if e.type in COMMAND_TYPES]


def wait_until(predicate: Callable[[], bool], timeout: float = 60, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def new_group_flags() -> int:
    """Windows: a new process group, so a console control event can target only it."""
    return subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0


def interrupt(proc: subprocess.Popen[str]) -> None:
    """Ctrl+Break (Windows, whole process group) or SIGINT (POSIX, that process only)."""
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


class RecorderProcess:
    """``agentbodycam record`` running in its own process."""

    def __init__(self, project: Path, interval: float = 0.1) -> None:
        self.project = project
        self.proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and test arguments
            cli("record", "--project", str(project), "--interval", str(interval)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=ENV,
            creationflags=new_group_flags(),
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self.output: list[str] = []
        assert self.proc.stdout is not None
        threading.Thread(target=self._read, args=(self.proc.stdout,), daemon=True).start()
        self.session_id = SESSION_LINE.findall(self.wait_for("Press Ctrl+C to stop."))[0]

    def _read(self, stream: io.TextIOBase) -> None:
        for line in iter(stream.readline, ""):
            self._lines.put(line)

    def wait_for(self, needle: str, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.1)
            except queue.Empty:
                if self.proc.poll() is not None and self._lines.empty():
                    break
                continue
            self.output.append(line)
            if needle in line:
                return "".join(self.output)
        raise AssertionError(f"recorder never printed {needle!r}: {''.join(self.output)!r}")

    def stop(self) -> None:
        interrupt(self.proc)
        self.wait_for("Recording stopped.")
        assert self.proc.wait(timeout=60) == 0

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=60)
