"""Git evidence recorded by the universal recorder, against real temporary repositories."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from agentbodycam.cli import main
from agentbodycam.core.git_observer import GitObservation, GitObserver
from agentbodycam.core.recorder import GitStatus, Recorder
from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventSource, EventType
from git_helpers import GIT, commit_all, git, git_dir_state, head, init_repo, requires_git
from recording_helpers import all_events, file_events, store_bytes, write

pytestmark = requires_git


def git_events(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type is EventType.GIT_SNAPSHOT]


def git_errors(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type is EventType.ERROR_OBSERVED and e.source is EventSource.GIT]


def make_recorder(store: EventStore, **kwargs: object) -> Recorder:
    kwargs.setdefault("git_interval", 0.0)
    kwargs.setdefault("git_observer", GitObserver(store.project_root, executable=GIT))
    rec = Recorder(store, **kwargs)  # type: ignore[arg-type]
    rec.start()
    rec.take_baseline()
    return rec


@pytest.fixture
def repo(project: Path) -> Path:
    init_repo(project)
    write(project / "app.py", "print('v1')\n")
    commit_all(project, "Initial commit")
    return project


# -- availability ------------------------------------------------------------------------


def test_non_git_project_records_files_and_no_git_events(project: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    write(project / "a.txt", "a")
    events = rec.scan_once() + rec.scan_once()
    assert file_events(events) == [("file.created", "a.txt")]
    assert git_events(events) == [] and git_errors(events) == []
    assert rec.git_status is GitStatus.NOT_A_REPOSITORY
    session = rec.stop()
    stored = all_events(store, session.session_id)
    assert [e.type for e in stored] == [
        EventType.SESSION_STARTED,
        EventType.FILE_CREATED,
        EventType.SESSION_STOPPED,
    ]
    assert stored[0].payload["git"] == {
        "enabled": True,
        "executable_found": True,
        "interval_seconds": 0.0,
    }
    assert stored[-1].payload["git_snapshots"] == 0
    assert stored[-1].payload["git_status"] == "not_a_repository"


def test_git_executable_unavailable_does_not_stop_file_recording(
    repo: Path, store: EventStore
) -> None:
    rec = make_recorder(store, git_observer=GitObserver(repo, executable=None))
    write(repo / "new.txt", "x")
    events = rec.scan_once()
    assert file_events(events) == [("file.created", "new.txt")]
    [error] = git_errors(events)
    assert error.payload["error"] == "git_not_found"
    assert rec.git_status is GitStatus.GIT_NOT_FOUND
    write(repo / "new.txt", "xy")
    again = rec.scan_once()
    assert file_events(again) == [("file.modified", "new.txt")]
    assert git_errors(again) == []  # reported once, not on every scan
    started = all_events(store, rec.session.session_id)[0]
    assert started.payload["git"] == {
        "enabled": True,
        "executable_found": False,
        "interval_seconds": 0.0,
    }


def test_git_missing_outside_a_repository_is_silent(project: Path, store: EventStore) -> None:
    rec = make_recorder(store, git_observer=GitObserver(project, executable=None))
    assert rec.scan_once() == []
    assert rec.git_status is GitStatus.GIT_NOT_FOUND


def test_git_recording_can_be_disabled(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store, record_git=False, git_observer=None)
    assert rec.git is None
    assert rec.observe_git() == []
    assert git_events(rec.scan_once()) == []
    assert rec.git_status is GitStatus.DISABLED


# -- snapshots ---------------------------------------------------------------------------


def test_initial_snapshot_after_session_start(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    [event] = rec.observe_git()
    assert event.type is EventType.GIT_SNAPSHOT
    assert event.source is EventSource.GIT
    assert event.path is None
    assert event.sequence == 2  # right after session.started
    payload = event.payload
    assert payload["repository"] is True
    assert payload["state"] == "clean"
    head_info = payload["head"]
    assert isinstance(head_info, dict)
    assert head_info["commit"] == head(repo)
    assert head_info["branch"] == "main"
    assert payload["changed"] == ["repository"]
    assert payload["scan"] == 0
    assert "head_change" not in payload
    assert event.summary.startswith("Git repository: on branch main at ")
    assert rec.git_status is GitStatus.REPOSITORY


def test_no_duplicate_git_events_when_nothing_changes(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    assert len(git_events(rec.scan_once())) == 1
    for _ in range(4):
        assert rec.scan_once() == []
    assert rec.stats.git_snapshots == 1


def test_dirty_then_clean_transitions(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    write(repo / "app.py", "print('v2')\n")
    events = rec.scan_once()
    assert file_events(events) == [("file.modified", "app.py")]
    [dirty] = git_events(events)
    assert dirty.summary == "Working tree became dirty"
    assert dirty.payload["unstaged"] == [{"path": "app.py", "status": "modified"}]
    assert set(dirty.payload["changed"]) >= {"state", "unstaged", "counts"}  # type: ignore[arg-type]

    git(repo, "add", "app.py")
    [staged] = git_events(rec.scan_once())
    assert staged.summary == "Git status changed (1 staged, 0 unstaged, 0 untracked)"
    assert staged.payload["staged"] == [{"path": "app.py", "status": "modified"}]

    git(repo, "checkout", "-q", "HEAD", "--", "app.py")
    [clean] = git_events(rec.scan_once())
    assert clean.summary == "Working tree became clean"
    assert clean.payload["state"] == "clean"


def test_untracked_deleted_and_renamed_are_recorded(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    write(repo / "notes with space.md", "x")
    [untracked] = git_events(rec.scan_once())
    assert untracked.payload["untracked"] == ["notes with space.md"]
    (repo / "notes with space.md").unlink()
    git(repo, "mv", "app.py", "main.py")
    [renamed] = git_events(rec.scan_once())
    assert renamed.payload["untracked"] == []
    assert renamed.payload["staged"] == [
        {"path": "main.py", "status": "renamed", "original_path": "app.py"}
    ]
    git(repo, "rm", "-q", "--cached", "main.py")
    [deleted] = git_events(rec.scan_once())
    assert {"path": "app.py", "status": "deleted"} in deleted.payload["staged"]  # type: ignore[operator]


def test_commit_during_session_records_head_change(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    before = head(repo)
    write(repo / "feature.py", "x = 1\n")
    git(repo, "add", "feature.py")
    rec.scan_once()
    git(repo, "commit", "-q", "-m", "Add feature")
    after = head(repo)
    [event] = git_events(rec.scan_once())
    assert event.summary == (f"HEAD changed {before[:7]} -> {after[:7]}; working tree became clean")
    change = event.payload["head_change"]
    assert change == {
        "previous": before,
        "current": after,
        "relation": "descendant",
        "commits": [{"commit": after, "subject": "Add feature"}],
        "commits_truncated": False,
    }
    assert "author@example.invalid" not in store_bytes(repo).decode("utf-8")


def test_first_commit_in_new_repository(project: Path, store: EventStore) -> None:
    init_repo(project)
    rec = make_recorder(store)
    [initial] = rec.observe_git()
    assert initial.summary == "Git repository: on branch main (no commits yet), working tree clean"
    write(project / "a.txt", "a")
    sha = commit_all(project, "First commit")
    [event] = git_events(rec.scan_once())
    assert event.summary == f"HEAD changed (none) -> {sha[:7]}"
    change = event.payload["head_change"]
    assert isinstance(change, dict)
    assert change["relation"] == "no_previous_commit"
    assert change["commits"] == [{"commit": sha, "subject": "First commit"}]


def test_branch_switch_and_detached_head(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    git(repo, "checkout", "-q", "-b", "feature")
    [switched] = git_events(rec.scan_once())
    assert switched.summary == "Branch changed main -> feature"
    assert "head_change" not in switched.payload  # same commit

    git(repo, "checkout", "-q", "--detach")
    [detached] = git_events(rec.scan_once())
    assert detached.summary == f"HEAD detached at {head(repo)[:7]}"
    head_info = detached.payload["head"]
    assert isinstance(head_info, dict)
    assert head_info["detached"] is True and head_info["branch"] is None


def test_repository_created_and_removed_mid_session(project: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    assert rec.observe_git() == []
    init_repo(project)
    [appeared] = git_events(rec.scan_once())
    assert appeared.payload["repository"] is True
    (project / ".git").rename(project.parent / "moved-git-dir")
    [gone] = git_events(rec.scan_once())
    assert gone.payload["repository"] is False
    assert gone.payload["reason"] == "not_a_repository"
    assert gone.summary == "Git repository no longer observable (not_a_repository)"
    assert rec.scan_once() == []


def test_unicode_paths_in_git_snapshot(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    write(repo / "ünïcødé dir" / "ファイル 1.md", "x")
    [event] = git_events(rec.scan_once())
    assert event.payload["untracked"] == ["ünïcødé dir/ファイル 1.md"]
    reread = git_events(all_events(store, rec.session.session_id))[-1]
    assert reread.payload["untracked"] == ["ünïcødé dir/ファイル 1.md"]


# -- polling frequency -------------------------------------------------------------------


class CountingObserver(GitObserver):
    def __init__(self, root: Path) -> None:
        super().__init__(root, executable=GIT)
        self.calls = 0

    def observe(self) -> GitObservation:
        self.calls += 1
        return super().observe()


def test_git_is_polled_less_often_than_files(repo: Path, store: EventStore) -> None:
    now = [0.0]
    observer = CountingObserver(repo)
    rec = make_recorder(store, git_observer=observer, git_interval=2.0, monotonic=lambda: now[0])
    for _ in range(8):  # scans at t=0.0 .. 3.5; Git is due only at t=0.0 and t=2.0
        rec.scan_once()
        now[0] += 0.5
    assert observer.calls == 2
    rec.scan_once(force_git=True)
    assert observer.calls == 3


def test_run_captures_final_git_state_on_stop(repo: Path, store: EventStore) -> None:
    now = [0.0]
    rec: Recorder

    def sleep(seconds: float) -> None:
        now[0] += seconds
        write(repo / "late.txt", "x")
        rec.request_stop()

    rec = Recorder(
        store,
        git_observer=GitObserver(repo, executable=GIT),
        git_interval=60.0,
        sleep=sleep,
        monotonic=lambda: now[0],
    )
    session = rec.run()
    events = all_events(store, session.session_id)
    snapshots = git_events(events)
    assert [s.payload["state"] for s in snapshots] == ["clean", "dirty"]
    assert file_events(events) == [("file.created", "late.txt")]
    assert events[-1].payload["git_snapshots"] == 2


# -- failures are isolated ---------------------------------------------------------------


class ExplodingObserver(GitObserver):
    def observe(self) -> GitObservation:
        raise RuntimeError("bug in git observation")


def test_unexpected_git_failure_never_stops_file_recording(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store, git_observer=ExplodingObserver(repo, executable=GIT))
    write(repo / "a.txt", "a")
    events = rec.scan_once()
    assert file_events(events) == [("file.created", "a.txt")]
    [error] = git_errors(events)
    assert error.payload["error"] == "RuntimeError"
    write(repo / "b.txt", "b")
    again = rec.scan_once()
    assert file_events(again) == [("file.created", "b.txt")]
    assert git_errors(again) == []
    assert rec.git_status is GitStatus.ERROR
    session = rec.stop()
    assert all_events(store, session.session_id)[-1].payload["errors_observed"] == 1


def test_corrupted_repository_reported_once_then_recovers(repo: Path, store: EventStore) -> None:
    rec = make_recorder(store)
    rec.observe_git()
    index = repo / ".git" / "index"
    good = index.read_bytes()
    index.write_bytes(b"corrupted index")
    write(repo / "x.txt", "x")
    events = rec.scan_once()
    assert file_events(events) == [("file.created", "x.txt")]
    [error] = git_errors(events)
    assert error.payload["operation"] == "status"
    assert error.payload["error"] == "command_failed"
    assert error.summary == "Git error during status: command_failed"
    assert git_errors(rec.scan_once()) == []
    index.write_bytes(good)
    [recovered] = git_events(rec.scan_once())
    assert recovered.payload["untracked"] == ["x.txt"]


def test_git_timeout_is_reported_without_stopping(repo: Path, store: EventStore) -> None:
    observer = GitObserver(repo, executable=GIT, query_timeout=1e-9, status_timeout=1e-9)
    rec = make_recorder(store, git_observer=observer)
    write(repo / "a.txt", "a")
    events = rec.scan_once()
    assert file_events(events) == [("file.created", "a.txt")]
    assert [e.payload["error"] for e in git_errors(events)] == ["timeout"]


# -- privacy and read-only guarantees ----------------------------------------------------


def test_no_remote_urls_credentials_or_contents_are_stored(repo: Path, store: EventStore) -> None:
    git(repo, "remote", "add", "origin", "https://alice:ghp_SECRET999@example.invalid/x.git")
    git(repo, "config", "credential.helper", "store-SECRET-HELPER")
    rec = make_recorder(store)
    rec.observe_git()
    write(repo / "app.py", "print('CONTENT-MARKER-42')\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "Tweak app")
    write(repo / ".env", "API_KEY=SUPER-SECRET")
    [snapshot] = git_events(rec.scan_once())
    assert snapshot.payload["redacted_paths"] == 1
    assert ".env" not in snapshot.to_json_line()
    rec.stop()
    written = store_bytes(repo)
    for secret in (
        b"ghp_SECRET999",
        b"example.invalid",
        b"SECRET-HELPER",
        b"CONTENT-MARKER-42",
        b"SUPER-SECRET",
        b"API_KEY",
        b"author@example.invalid",
    ):
        assert secret not in written, secret


def test_recording_never_mutates_git(repo: Path, store: EventStore) -> None:
    write(repo / "app.py", "print('dirty')\n")
    write(repo / "staged.txt", "x")
    git(repo, "add", "staged.txt")
    write(repo / "staged.txt", "y")
    before = git_dir_state(repo)
    rec = make_recorder(store)
    for _ in range(3):
        rec.scan_once()
    rec.stop()
    assert git_dir_state(repo) == before
    assert git(repo, "status", "--porcelain=v1") == " M app.py\nAM staged.txt\n"


# -- CLI ---------------------------------------------------------------------------------


def test_cli_record_in_git_repository(repo: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["record", "--project", str(repo), "--duration", "0", "--interval", "0.05"],
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
    text = out.getvalue()
    assert "GIT       Git repository: on branch main at " in text
    assert "Watching for file and Git changes" in text
    assert "Git snapshots recorded: 1" in text


def test_cli_record_outside_git_repository(project: Path) -> None:
    out = io.StringIO()
    assert main(["record", "--project", str(project), "--duration", "0"], out=out) == 0
    text = out.getvalue()
    assert "Git: not a Git repository (Git state not recorded)" in text
    assert "Watching for file changes" in text
    assert "GIT " not in text
