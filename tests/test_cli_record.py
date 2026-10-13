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
from pathlib import Path

import pytest

from agentbodycam.cli import _display, main
from agentbodycam.core.store import EventStore
from agentbodycam.models.session import SessionStatus
from recording_helpers import file_events, write

SESSION_LINE = re.compile(r"^Session: (\S+)$", re.MULTILINE)


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_help_lists_record() -> None:
    code, out, _ = run()
    assert code == 0
    assert "record" in out


def test_record_with_duration_in_process(project: Path) -> None:
    write(project / "existing.py", "x")
    code, out, err = run(
        "record", "--project", str(project), "--duration", "0", "--interval", "0.05"
    )
    assert code == 0, err
    assert out.startswith("AgentBodycam recording\nSession: ")
    assert f"Project: {project.resolve()}" in out
    assert "Baseline snapshot: 1 file\n" in out
    assert "Press Ctrl+C to stop." in out
    assert "Recording stopped." in out
    assert "File events recorded: 0 (0 created, 0 modified, 0 deleted)" in out
    session_ids = SESSION_LINE.findall(out)
    assert len(session_ids) == 2 and session_ids[0] == session_ids[1]
    session = EventStore(project).load_session(session_ids[0])
    assert session.status is SessionStatus.STOPPED
    assert session.ended_at is not None


def test_record_defaults_to_current_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    code, _, err = run("record", "--duration", "0")
    assert code == 0, err
    assert len(EventStore(project).list_session_ids()) == 1


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--interval", "0"),
        ("--interval", "-1"),
        ("--interval", "abc"),
        ("--interval", "nan"),
        ("--duration", "-5"),
        ("--duration", "inf"),
    ],
)
def test_record_rejects_bad_numbers(
    project: Path, flag: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["record", "--project", str(project), flag, value])
    assert exc.value.code == 2
    assert not (project / ".agentbodycam").exists()
    capsys.readouterr()


def test_record_missing_project_fails_cleanly(tmp_path: Path) -> None:
    code, out, err = run("record", "--project", str(tmp_path / "missing"), "--duration", "0")
    assert code == 1
    assert "not a directory" in err
    assert "Recording stopped" not in out


def test_display_escapes_control_characters() -> None:
    assert _display("ok/naïve file.py") == "ok/naïve file.py"
    assert _display("evil\x1b[31mred\nline") == "evil\\x1b[31mred\\nline"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids control chars in names")
def test_record_output_escapes_hostile_filenames(project: Path) -> None:
    write(project / "evil\x1b[2Jname.txt", "x")
    code, out, err = run("record", "--project", str(project), "--duration", "0")
    assert code == 0, err
    assert "\x1b" not in out


# -- real process, real signal -----------------------------------------------------------


def _reader(stream: io.TextIOBase, lines: queue.Queue[str]) -> None:
    for line in iter(stream.readline, ""):
        lines.put(line)


def _wait_for(lines: queue.Queue[str], needle: str, seen: list[str], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(line)
        if needle in line:
            return
    raise AssertionError(f"timed out waiting for {needle!r}; output so far: {''.join(seen)!r}")


def test_record_subprocess_stops_cleanly_on_ctrl_c(tmp_path: Path) -> None:
    project = tmp_path / "ünïcødé project" / "with spaces"
    project.mkdir(parents=True)
    write(project / "app.py", "v1\n")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-controlled arguments
        [
            sys.executable,
            "-m",
            "agentbodycam",
            "record",
            "--project",
            str(project),
            "--interval",
            "0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        creationflags=flags,
    )
    assert proc.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()
    seen: list[str] = []
    try:
        _wait_for(lines, "Press Ctrl+C to stop.", seen)
        write(project / "src" / "new file.py", "print(1)\n")
        write(project / "app.py", "version 2\n")
        _wait_for(lines, "src/new file.py", seen)
        # Windows cannot deliver Ctrl+C to a separate process group; Ctrl+Break is the
        # equivalent console interrupt and takes the same clean-stop path.
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    while not lines.empty():
        seen.append(lines.get())
    output = "".join(seen)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
    assert "Recording stopped." in output
    assert "CREATED   src/new file.py" in output
    assert "MODIFIED  app.py" in output
    session_id = SESSION_LINE.findall(output)[0]

    store = EventStore(project)
    session = store.load_session(session_id)
    assert session.status is SessionStatus.STOPPED
    assert session.ended_at is not None
    events = store.read_events(session_id)
    assert events[0].type.value == "session.started"
    assert events[-1].type.value == "session.stopped"
    assert events[-1].payload["reason"] == "requested"
    assert sorted(file_events(events)) == [
        ("file.created", "src/new file.py"),
        ("file.modified", "app.py"),
    ]
