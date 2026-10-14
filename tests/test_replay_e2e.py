"""Replay API end to end: the real CLI process, real recorders and real wrapped commands."""

from __future__ import annotations

import io
import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentbodycam.cli import _run_replay_api, build_parser, main
from agentbodycam.core.recorder import Recorder
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import EventType
from command_helpers import (
    ENV,
    RecorderProcess,
    cli,
    interrupt,
    new_group_flags,
    run_cli,
    wait_until,
)
from forensics_helpers import CAUSAL_LANGUAGE, SessionBuilder
from git_helpers import commit_all, init_repo, requires_git
from recording_helpers import write
from replay_helpers import ApiClient, Endpoint, Reply, serve, store_snapshot

LISTENING = re.compile(r"^Listening: http://127\.0\.0\.1:(\d+)$", re.MULTILINE)
TOKEN_LINE = re.compile(r"^API token: (\S+)$", re.MULTILINE)
CONTENT_MARKER = "CONTENT_MARKER_e1d4a7"
OUTPUT_MARKER = "STDOUT_MARKER_93b2f0"
ENV_MARKER = "ENV_MARKER_58c0aa"
SECRET = "ghp_" + "Rt5" * 12
CHECK_SCRIPT = (
    "import pathlib, sys\n"
    f"print('{OUTPUT_MARKER}')\n"
    "sys.exit(0 if pathlib.Path('ok.flag').exists() else 1)\n"
)


class ApiProcess:
    """``agentbodycam replay-api`` running in its own process."""

    def __init__(self, project: Path, *extra: str) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and test arguments
            cli("replay-api", "--project", str(project), *extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=ENV,
            creationflags=new_group_flags(),
        )
        self.lines: list[str] = []
        self._ready = threading.Event()
        assert self.proc.stdout is not None
        threading.Thread(target=self._read, args=(self.proc.stdout,), daemon=True).start()
        if not self._ready.wait(60):
            self.kill()
            raise AssertionError(f"replay-api never became ready: {self.output!r}")
        port = LISTENING.search(self.output)
        token = TOKEN_LINE.search(self.output)
        assert port is not None and token is not None, self.output
        self.endpoint = Endpoint("127.0.0.1", int(port.group(1)), token.group(1))

    @property
    def output(self) -> str:
        return "".join(self.lines)

    def _read(self, stream: io.TextIOBase) -> None:
        for line in iter(stream.readline, ""):
            self.lines.append(line)
            if "Press Ctrl+C to stop." in line:
                self._ready.set()
        self._ready.set()

    def stop(self) -> int:
        interrupt(self.proc)
        return self.proc.wait(timeout=60)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=60)


def port_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return False
    except OSError:
        return True


# -- the CLI process -------------------------------------------------------------------------


def test_cli_process_serves_and_stops_cleanly(project: Path, tmp_path: Path) -> None:
    api = ApiProcess(project)
    try:
        startup = api.output
        assert startup.startswith("AgentBodycam Replay API\n")
        assert "Authentication: enabled" in startup
        assert "no Replay UI yet" in startup and "Press Ctrl+C to stop." in startup
        assert startup.count(api.endpoint.token) == 1  # printed once, for the developer
        client = ApiClient(api.endpoint)
        assert client.get("/api/v1/health", token=None).status == 200
        assert client.get("/api/v1/sessions", token=None).status == 401
        assert client.get("/api/v1/sessions").json["sessions"] == []
        assert client.request("POST", "/api/v1/sessions").status == 405
    finally:
        code = api.stop()
    assert code == 0
    assert "Replay API stopped." in api.output
    assert port_closed(api.endpoint.port)
    assert not (project / ".agentbodycam").exists()  # starting the API created nothing
    assert list(project.iterdir()) == []
    token = api.endpoint.token.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token not in path.read_bytes(), path
    assert api.endpoint.token not in api.output.split("API token:", 1)[1].split("\n", 1)[1]
    assert api.proc.stderr is not None and api.proc.stderr.read() == ""


def test_cli_process_reads_sessions_without_printing_them(store: EventStore) -> None:
    b = SessionBuilder(store)
    b.run_check(False)
    b.stop()
    before = store_snapshot(store.project_root)
    api = ApiProcess(store.project_root)
    try:
        assert b.session_id not in api.output
        client = ApiClient(api.endpoint)
        listing = client.get("/api/v1/sessions").json
        assert [s["session_id"] for s in listing["sessions"]] == [b.session_id]
        report = client.get(f"/api/v1/sessions/{b.session_id}/report")
        out = io.StringIO()
        main(["analyze", "--json", "--project", str(store.project_root), b.session_id], out=out)
        assert report.body.decode("utf-8") == out.getvalue()
    finally:
        assert api.stop() == 0
    assert store_snapshot(store.project_root) == before
    # The request log shows route templates only.
    assert "GET /api/v1/sessions/{session_id}/report 200" in api.output
    assert b.session_id not in api.output


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.20", "10.1.2.3", "example.com"],  # noqa: S104
)
def test_cli_refuses_public_and_lan_hosts(
    project: Path, host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["replay-api", "--project", str(project), "--host", host])
    assert exc.value.code == 2
    assert "only binds to loopback addresses" in capsys.readouterr().err
    assert not (project / ".agentbodycam").exists()


@pytest.mark.parametrize("port", ["-1", "65536", "http", "1.5"])
def test_cli_rejects_bad_ports(project: Path, port: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["replay-api", "--project", str(project), "--port", port])
    assert exc.value.code == 2


def test_cli_missing_project(tmp_path: Path) -> None:
    err = io.StringIO()
    code = main(["replay-api", "--project", str(tmp_path / "missing")], out=io.StringIO(), err=err)
    assert code == 1 and "does not exist" in err.getvalue()
    assert not (tmp_path / "missing").exists()


def test_cli_defaults_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    args = build_parser().parse_args(["replay-api"])
    assert (args.host, args.port, args.project) == ("127.0.0.1", 0, None)
    with pytest.raises(SystemExit):
        main(["replay-api", "--help"])
    text = capsys.readouterr().out
    assert "Bearer" in text and "loopback" in text and "Read-only" in text


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_cli_runner_in_process(project: Path, host: str) -> None:
    args = build_parser().parse_args(["replay-api", "--project", str(project), "--host", host])
    out, err = io.StringIO(), io.StringIO()
    stop = threading.Event()
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(_run_replay_api(args, out, err, stop)))
    runner.start()
    try:
        wait_until(lambda: "Press Ctrl+C to stop." in out.getvalue(), what="startup")
        port, token = LISTENING.search(out.getvalue()), TOKEN_LINE.search(out.getvalue())
        assert port is not None and token is not None  # localhost is served on 127.0.0.1
        client = ApiClient(Endpoint("127.0.0.1", int(port.group(1)), token.group(1)))
        assert client.get("/api/v1/project").json == {"api_version": 1, "session_count": 0}
    finally:
        stop.set()
        runner.join(timeout=30)
    assert result == [0] and not runner.is_alive()
    assert out.getvalue().endswith("Replay API stopped.\n")
    assert port_closed(int(port.group(1)))


# -- live and crashed recordings -------------------------------------------------------------


@pytest.fixture
def recorder_process(project: Path) -> Iterator[RecorderProcess]:
    proc = RecorderProcess(project, interval=0.05)
    try:
        yield proc
    finally:
        proc.kill()


def test_api_reads_while_a_real_recorder_is_active(
    project: Path, recorder_process: RecorderProcess
) -> None:
    sid = recorder_process.session_id
    server = serve(project)
    client = ApiClient(server)
    base = f"/api/v1/sessions/{sid}"
    stop_writing = threading.Event()

    def writer() -> None:
        number = 0
        while not stop_writing.is_set() and number < 60:
            write(project / f"f{number}.txt", str(number))
            number += 1
            time.sleep(0.01)

    def reader(index: int) -> Reply:
        path = (f"{base}/report", f"{base}/timeline?limit=500", base, "/api/v1/sessions")[index % 4]
        return client.get(path)

    try:
        thread = threading.Thread(target=writer)
        thread.start()
        totals: list[int] = []
        try:
            for _ in range(5):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    replies = list(pool.map(reader, range(16)))
                for reply in replies:
                    assert reply.status == 200, reply.body
                page = client.get(f"{base}/timeline?limit=500").json
                items = page["items"]
                assert isinstance(items, list) and isinstance(page["total"], int)
                seqs = [item["sequence"] for item in items]
                assert seqs == list(range(1, page["total"] + 1))  # contiguous, nothing torn
                totals.append(page["total"])
                report = client.get(f"{base}/report").json
                assert report["recording_state"] == "recording"
                assert report["analysis_complete"] is False
        finally:
            stop_writing.set()
            thread.join()
        assert totals == sorted(totals)
        wait_until(
            lambda: client.get(base).json["event_counts"].get("file.created", 0) >= 1,
            what="file events",
        )
        listing = client.get("/api/v1/sessions").json["sessions"]
        assert listing[0]["recording_state"] == "recording"
        recorder_process.stop()  # the API never blocked or stopped the recorder
        report = client.get(f"{base}/report").json
        assert (report["recording_state"], report["analysis_complete"]) == ("stopped", True)
        types = [e.type for e in EventStore(project).read_events(sid)]
        assert types[-1] is EventType.SESSION_STOPPED
    finally:
        server.stop()


def test_api_reports_a_killed_recorder_without_repairing_it(
    project: Path, recorder_process: RecorderProcess
) -> None:
    sid = recorder_process.session_id
    recorder_process.kill()
    before = store_snapshot(project)
    server = serve(project)
    try:
        client = ApiClient(server)
        report = client.get(f"/api/v1/sessions/{sid}/report").json
        assert report["session_status"] == "recording"
        assert (report["recording_state"], report["analysis_complete"]) == (
            "ended_without_stop",
            True,
        )
        detail = client.get(f"/api/v1/sessions/{sid}").json
        assert detail["recording_state"] == "ended_without_stop"
        [summary] = client.get("/api/v1/sessions").json["sessions"]
        assert summary["recording_state"] == "ended_without_stop"
    finally:
        server.stop()
    assert store_snapshot(project) == before


# -- privacy and forensics over a real recording ---------------------------------------------


@requires_git
def test_real_recording_through_the_api(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("AGENTBODYCAM_TEST_SECRET_ENV", ENV_MARKER)
    init_repo(project)
    write(project / "app.py", f"# {CONTENT_MARKER}\nVALUE = 1\n")
    write(project / "check.py", CHECK_SCRIPT)
    commit_all(project, "initial")
    recorder = Recorder(EventStore(project), git_interval=0)
    sid = recorder.start().session_id
    recorder.take_baseline()
    recorder.observe_git()
    try:
        argv = ["run", "--project", str(project), "--check", "test", "--"]
        code, err = run_cli(*argv, sys.executable, "check.py")
        assert code == 1, err
        write(project / "app.py", f"# {CONTENT_MARKER}\nVALUE = 2\n")
        write(project / "ok.flag", "")
        recorder.scan_once(force_git=True)
        code, err = run_cli(*argv, sys.executable, "check.py")
        assert code == 0, err
        code, err = run_cli(
            "run", "--project", str(project), "--", sys.executable, "-c", "pass", "--token", SECRET
        )
        assert code == 0, err
    finally:
        recorder.stop()
    before = store_snapshot(project)

    server = serve(project)
    try:
        client = ApiClient(server)
        base = f"/api/v1/sessions/{sid}"
        report = client.get(f"{base}/report").json
        first = report["first_observable_failure"]
        assert isinstance(first, dict) and first["failure_type"] == "check_failed"
        recoveries = report["recoveries"]
        assert isinstance(recoveries, dict) and recoveries["total"] == 1
        bodies = [
            client.get(path).body.decode("utf-8")
            for path in (
                "/api/v1/project",
                "/api/v1/sessions",
                base,
                f"{base}/report",
                f"{base}/timeline?limit=500",
            )
        ]
    finally:
        server.stop()
    assert store_snapshot(project) == before

    root = str(project.resolve())
    home = str(Path.home())
    forbidden = [
        CONTENT_MARKER,  # file contents
        OUTPUT_MARKER,  # stdout/stderr
        ENV_MARKER,  # environment variables
        SECRET,  # redacted command argument
        root,
        json.dumps(root)[1:-1],
        root.replace("\\", "/"),
        home,
        json.dumps(home)[1:-1],
        "project_root",
        "python_version",
        "payload",
    ]
    for body in bodies:
        for marker in forbidden:
            assert marker not in body, marker
        assert CAUSAL_LANGUAGE.search(body) is None, CAUSAL_LANGUAGE.search(body)
