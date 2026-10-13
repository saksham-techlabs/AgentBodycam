from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentbodycam.cli import main
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventDraft, EventSource, EventType

CREATED = re.compile(r"Created AgentBodycam session: (\S+)\nStorage: (.+)\n")


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def create(project: Path) -> str:
    code, out, err = run("session", "create", "--project", str(project))
    assert code == 0, err
    match = CREATED.fullmatch(out)
    assert match, out
    return match.group(1)


def test_help_lists_session_command() -> None:
    code, out, _ = run()
    assert code == 0
    assert "session" in out


def test_session_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["session"])
    assert exc.value.code == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize("subcommand", ["record", "start", "delete"])
def test_unimplemented_session_subcommands_rejected(subcommand: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["session", subcommand])
    assert exc.value.code == 2


def test_create_prints_id_and_storage(project: Path) -> None:
    code, out, err = run("session", "create", "--project", str(project))
    assert code == 0
    assert err == ""
    match = CREATED.fullmatch(out)
    assert match
    sid, storage = match.groups()
    expected = project.resolve() / ".agentbodycam" / "sessions" / sid
    assert Path(storage) == expected
    assert (expected / "session.json").is_file()
    assert (expected / "events.jsonl").read_bytes() == b""
    assert (project / ".agentbodycam" / ".gitignore").is_file()


def test_create_defaults_to_current_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    code, out, _ = run("session", "create")
    assert code == 0
    match = CREATED.fullmatch(out)
    assert match
    assert (project / ".agentbodycam" / "sessions" / match.group(1)).is_dir()


def test_list_empty_project(project: Path) -> None:
    code, out, _ = run("session", "list", "--project", str(project))
    assert code == 0
    assert "No AgentBodycam sessions found" in out
    assert not (project / ".agentbodycam").exists()


def test_list_multiple_sessions(project: Path) -> None:
    ids = [create(project) for _ in range(3)]
    EventStore(project).append_event(
        ids[1],
        EventDraft(type=EventType.SESSION_STARTED, source=EventSource.AGENTBODYCAM, summary="x"),
    )
    code, out, _ = run("session", "list", "--project", str(project))
    assert code == 0
    rows = [
        line.split() for line in out.splitlines() if line.strip().startswith(tuple("0123456789"))
    ]
    assert [row[0] for row in rows] == sorted(ids)
    counts = {row[0]: row[-1] for row in rows}
    assert counts[ids[1]] == "1"
    assert counts[ids[0]] == "0"
    assert all(row[1] == "created" for row in rows)


def test_list_reports_corrupt_session(project: Path) -> None:
    good = create(project)
    bad = create(project)
    events = project / ".agentbodycam" / "sessions" / bad / "events.jsonl"
    events.write_bytes(b"{broken\n")
    code, out, err = run("session", "list", "--project", str(project))
    assert code == 1
    assert good in out
    assert bad in out and "unreadable" in out
    assert "line 1" in err


def test_show_session_metadata(project: Path) -> None:
    sid = create(project)
    store = EventStore(project)
    for n in range(2):
        store.append_event(
            sid,
            EventDraft(type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary=f"{n}"),
        )
    code, out, err = run("session", "show", sid, "--project", str(project))
    assert code == 0, err
    assert f"AgentBodycam session {sid}" in out
    assert "Status        : created" in out
    assert "Ended (UTC)   : -" in out
    assert f"Project root  : {project.resolve()}" in out
    assert "Events        : 2" in out
    assert "Platform" in out


def test_show_unknown_session(project: Path) -> None:
    create(project)
    code, out, err = run(
        "session", "show", "20991231T235959Z-0000000000000000", "--project", str(project)
    )
    assert code == 1
    assert out == ""
    assert "session not found" in err


@pytest.mark.parametrize("bad_id", ["../..", "..\\..", "/etc/passwd", "nope"])
def test_show_rejects_unsafe_ids(project: Path, bad_id: str) -> None:
    code, out, err = run("session", "show", bad_id, "--project", str(project))
    assert code == 2
    assert out == ""
    assert "invalid session ID" in err
    assert not (project / ".agentbodycam").exists()


def test_show_reports_corrupt_event_log(project: Path) -> None:
    sid = create(project)
    (project / ".agentbodycam" / "sessions" / sid / "events.jsonl").write_bytes(b"{}")
    code, out, err = run("session", "show", sid, "--project", str(project))
    assert code == 1
    assert "Events        : unreadable" in out
    assert "truncated" in err


def test_create_in_missing_project_fails_cleanly(tmp_path: Path) -> None:
    code, _, err = run("session", "create", "--project", str(tmp_path / "missing"))
    assert code == 1
    assert "not a directory" in err


def test_cli_subprocess_with_unicode_and_spaces(tmp_path: Path) -> None:
    project = tmp_path / "ünïcødé project" / "with spaces"
    project.mkdir(parents=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed interpreter and test-controlled arguments
            [sys.executable, "-m", "agentbodycam", "session", *args, "--project", str(project)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    created = cli("create")
    assert created.returncode == 0, created.stderr
    match = CREATED.fullmatch(created.stdout.replace("\r\n", "\n"))
    assert match
    sid = match.group(1)

    listed = cli("list")
    assert listed.returncode == 0, listed.stderr
    assert sid in listed.stdout

    shown = cli("show", sid)
    assert shown.returncode == 0, shown.stderr
    assert "Events        : 0" in shown.stdout
    assert str(project.resolve()) in shown.stdout
