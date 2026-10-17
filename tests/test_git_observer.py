"""GitObserver against real temporary Git repositories (no network, no user config)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentbodycam.core.git_observer import (
    GitObserver,
    GitState,
    GitUnavailable,
    describe_snapshot,
    unavailable_payload,
)
from agentbodycam.models.events import dumps_canonical
from agentbodycam.utils.git import find_git_executable, git_environment
from git_helpers import GIT, commit_all, git, git_dir_state, head, init_repo, requires_git
from recording_helpers import write

pytestmark = requires_git


def observer(root: Path, **kwargs: object) -> GitObserver:
    return GitObserver(root, executable=GIT, **kwargs)  # type: ignore[arg-type]


def state_of(root: Path) -> GitState:
    observation = observer(root).observe()
    assert observation.problem is None, observation.problem
    assert observation.state is not None, observation.unavailable
    return observation.state


@pytest.fixture
def repo(project: Path) -> Path:
    init_repo(project)
    write(project / "app.py", "print('v1')\n")
    write(project / "docs" / "guide.md", "guide\n")
    commit_all(project, "Initial commit")
    return project


# -- availability ------------------------------------------------------------------------


def test_non_git_project_is_unavailable(project: Path) -> None:
    observation = observer(project).observe()
    assert observation.unavailable is GitUnavailable.NOT_A_REPOSITORY
    assert observation.state is None and observation.problem is None


def test_git_executable_missing() -> None:
    observation = GitObserver(Path.cwd(), executable=None).observe()
    assert observation.unavailable is GitUnavailable.GIT_NOT_FOUND


def test_git_executable_that_cannot_run_is_a_problem(repo: Path, tmp_path: Path) -> None:
    observation = GitObserver(repo, executable=str(tmp_path / "no-such-git.exe")).observe()
    assert observation.problem is not None
    assert observation.problem.error == "os_error"
    assert observation.problem.operation == "rev-parse"


def test_find_git_executable_never_uses_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / ("git.exe" if sys.platform == "win32" else "git")
    write(planted, "#!/bin/sh\necho planted\n")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert find_git_executable("") is None
    assert find_git_executable(".") is None
    assert find_git_executable("relative/dir") is None
    assert find_git_executable(str(tmp_path)) == str(planted)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows git.cmd / git.bat trap")
def test_find_git_executable_ignores_cmd_and_bat_shims(tmp_path: Path) -> None:
    (tmp_path / "git.cmd").write_text("@echo off\r\necho planted\r\n", encoding="utf-8")
    (tmp_path / "git.bat").write_text("@echo off\r\necho planted\r\n", encoding="utf-8")
    assert find_git_executable(str(tmp_path)) is None


def test_git_environment_drops_redirections_and_disables_locks() -> None:
    env = git_environment(
        {"PATH": "/bin", "GIT_DIR": "/elsewhere", "GIT_WORK_TREE": "/x", "GIT_TRACE": "1"}
    )
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env and "GIT_TRACE" not in env
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["PATH"] == "/bin"


# -- snapshot contents -------------------------------------------------------------------


def test_repository_without_commits(project: Path) -> None:
    init_repo(project)
    state = state_of(project)
    assert state.commit is None
    assert state.branch == "main"
    assert state.detached is False
    assert state.clean
    payload = state.payload()
    assert payload["head"] == {"commit": None, "branch": "main", "detached": False, "unborn": True}
    assert payload["state"] == "clean"
    assert payload["upstream"] is None


def test_clean_repository(repo: Path) -> None:
    state = state_of(repo)
    assert state.commit == head(repo)
    assert state.clean
    payload = state.payload()
    assert payload["repository"] is True
    assert Path(str(payload["root"])).resolve() == repo.resolve()
    assert payload["project_prefix"] == ""
    assert payload["worktree"] == "main"
    assert payload["counts"] == {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0}
    assert payload["staged"] == payload["unstaged"] == payload["untracked"] == []


def test_untracked_file(repo: Path) -> None:
    write(repo / "src" / "new.py", "x")
    state = state_of(repo)
    assert state.untracked == ("src/new.py",)
    assert not state.clean
    assert state.payload()["state"] == "dirty"


def test_staged_file(repo: Path) -> None:
    write(repo / "new.py", "x")
    git(repo, "add", "new.py")
    state = state_of(repo)
    assert [e.evidence() for e in state.staged] == [{"path": "new.py", "status": "added"}]
    assert state.unstaged == () and state.untracked == ()


def test_unstaged_modification(repo: Path) -> None:
    write(repo / "app.py", "print('v2')\n")
    state = state_of(repo)
    assert [e.evidence() for e in state.unstaged] == [{"path": "app.py", "status": "modified"}]
    assert state.staged == ()


def test_staged_and_unstaged_changes_to_same_file(repo: Path) -> None:
    write(repo / "app.py", "print('v2')\n")
    git(repo, "add", "app.py")
    write(repo / "app.py", "print('v3')\n")
    state = state_of(repo)
    assert [e.status for e in state.staged] == ["modified"]
    assert [e.status for e in state.unstaged] == ["modified"]


def test_deleted_files(repo: Path) -> None:
    (repo / "app.py").unlink()
    git(repo, "rm", "-q", "docs/guide.md")
    state = state_of(repo)
    assert [e.evidence() for e in state.unstaged] == [{"path": "app.py", "status": "deleted"}]
    assert [e.evidence() for e in state.staged] == [{"path": "docs/guide.md", "status": "deleted"}]


def test_renamed_file(repo: Path) -> None:
    git(repo, "mv", "app.py", "main.py")
    state = state_of(repo)
    assert [e.evidence() for e in state.staged] == [
        {"path": "main.py", "status": "renamed", "original_path": "app.py"}
    ]


def test_conflicted_file(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "app.py", "print('feature')\n")
    commit_all(repo, "Feature change")
    git(repo, "checkout", "-q", "main")
    write(repo / "app.py", "print('main')\n")
    commit_all(repo, "Main change")
    git(repo, "merge", "feature", check=False)
    state = state_of(repo)
    assert [e.evidence() for e in state.conflicted] == [
        {"path": "app.py", "status": "both_modified"}
    ]
    assert state.payload()["counts"] == {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 1,
    }


def test_detached_head(repo: Path) -> None:
    sha = head(repo)
    git(repo, "checkout", "-q", "--detach")
    state = state_of(repo)
    assert state.detached is True
    assert state.branch is None
    assert state.commit == sha


def test_branch_name(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/login")
    assert state_of(repo).branch == "feature/login"


def test_no_upstream(repo: Path) -> None:
    state = state_of(repo)
    assert state.upstream is None and state.ahead is None and state.behind is None


def test_local_upstream_ahead_behind_without_network_or_urls(repo: Path) -> None:
    base = head(repo)
    git(repo, "remote", "add", "origin", "https://user:TOKEN-123@example.invalid/repo.git")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
    write(repo / "app.py", "print('ahead')\n")
    commit_all(repo, "Ahead by one")
    state = state_of(repo)
    assert (state.upstream, state.ahead, state.behind) == ("origin/main", 1, 0)
    text = dumps_canonical(state.payload())
    assert "TOKEN-123" not in text
    assert "example.invalid" not in text
    assert "https://" not in text


def test_spaces_and_unicode_paths(repo: Path) -> None:
    names = ["with space/my file.py", "ünïcødé/naïve café.txt", "日本語/ファイル.md"]
    for name in names:
        write(repo / name, "x")
    git(repo, "add", names[0])
    state = state_of(repo)
    assert [e.path for e in state.staged] == [names[0]]
    assert set(state.untracked) == set(names[1:])
    assert state.unrepresentable == 0


def test_project_subdirectory_is_the_recording_boundary(repo: Path) -> None:
    sub = repo / "docs"
    write(sub / "new page.md", "x")
    write(repo / "outside.py", "x")
    write(repo / "app.py", "changed\n")
    state = state_of(sub)
    assert state.prefix == "docs"
    assert state.untracked == ("new page.md",)
    assert state.unstaged == ()
    assert state.payload()["project_prefix"] == "docs"


def test_rename_from_outside_project_omits_original_path(repo: Path) -> None:
    git(repo, "mv", "app.py", "docs/app.py")
    state = state_of(repo / "docs")
    [entry] = state.staged
    assert entry.path == "app.py"
    assert entry.original_path is None


def test_sensitive_paths_are_counted_but_never_named(repo: Path) -> None:
    write(repo / ".env", "SECRET=1")
    write(repo / "certs" / "server.pem", "x")
    write(repo / "visible.txt", "x")
    state = state_of(repo)
    payload = state.payload()
    assert payload["untracked"] == ["visible.txt"]
    assert payload["counts"] == {"staged": 0, "unstaged": 0, "untracked": 3, "conflicted": 0}
    assert payload["redacted_paths"] == 2
    text = dumps_canonical(payload)
    assert ".env" not in text and "server.pem" not in text


def test_agentbodycam_store_is_excluded(repo: Path) -> None:
    write(repo / ".agentbodycam" / "sessions" / "x" / "events.jsonl", "{}")
    assert state_of(repo).clean


def test_linked_worktree(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked tree"
    git(repo, "worktree", "add", "-q", "-b", "wt-branch", str(linked))
    state = state_of(linked)
    assert state.worktree == "linked"
    assert state.branch == "wt-branch"
    assert state_of(repo).worktree == "main"


def test_submodule_contents_are_not_inspected(repo: Path, tmp_path: Path) -> None:
    inner = init_repo(tmp_path / "inner")
    write(inner / "lib.py", "x")
    commit_all(inner, "inner")
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(inner),
        "vendor/inner",
    )
    commit_all(repo, "Add submodule")
    write(repo / "vendor" / "inner" / "dirty.txt", "x")
    write(repo / "vendor" / "inner" / "lib.py", "changed")
    state = state_of(repo)
    assert state.clean


# -- failures ----------------------------------------------------------------------------


def test_corrupted_index_is_a_problem(repo: Path) -> None:
    (repo / ".git" / "index").write_bytes(b"garbage, not an index")
    observation = observer(repo).observe()
    assert observation.problem is not None
    assert observation.problem.operation == "status"
    assert observation.problem.error == "command_failed"
    assert observation.problem.exit_code not in (None, 0)
    assert str(repo) not in (observation.problem.detail or "")


def test_timeout_is_a_problem(repo: Path) -> None:
    observation = observer(repo, query_timeout=1e-9).observe()
    assert observation.problem is not None
    assert observation.problem.error == "timeout"


def test_oversized_output_is_a_problem(repo: Path) -> None:
    for i in range(20):
        write(repo / f"untracked-{i}.txt", "x")
    observation = observer(repo, max_status_output=64).observe()
    assert observation.problem is not None
    assert observation.problem.error == "output_too_large"


def test_repository_disappearing_becomes_unavailable(repo: Path) -> None:
    obs = observer(repo)
    assert obs.observe().state is not None
    (repo / ".git" / "HEAD").unlink()
    assert obs.observe().unavailable is GitUnavailable.NOT_A_REPOSITORY


# -- HEAD changes ------------------------------------------------------------------------


def test_head_change_after_commit_lists_new_commits(repo: Path) -> None:
    before = head(repo)
    write(repo / "a.txt", "a")
    first = commit_all(repo, "Add a")
    write(repo / "b.txt", "b")
    second = commit_all(repo, "Add b\n\nLong body that must never be stored.")
    change = observer(repo).head_change(before, second)
    assert change["relation"] == "descendant"
    assert change["commits"] == [
        {"commit": second, "subject": "Add b"},
        {"commit": first, "subject": "Add a"},
    ]
    assert change["commits_truncated"] is False
    text = dumps_canonical(change)
    assert "Long body" not in text
    assert "author@example.invalid" not in text and "Test Author" not in text


def test_head_change_backwards_and_diverged(repo: Path) -> None:
    base = head(repo)
    write(repo / "a.txt", "a")
    ahead = commit_all(repo, "Ahead")
    obs = observer(repo)
    back = obs.head_change(ahead, base)
    assert back["relation"] == "ancestor"
    assert back["commits"] == []
    git(repo, "checkout", "-q", "-b", "other", base)
    write(repo / "b.txt", "b")
    other = commit_all(repo, "Other line")
    diverged = obs.head_change(ahead, other)
    assert diverged["relation"] == "diverged"
    assert diverged["commits"] == [{"commit": other, "subject": "Other line"}]


def test_first_commit_head_change(project: Path) -> None:
    init_repo(project)
    write(project / "a.txt", "a")
    sha = commit_all(project, "First")
    change = observer(project).head_change(None, sha)
    assert change["relation"] == "no_previous_commit"
    assert change["commits"] == [{"commit": sha, "subject": "First"}]


def test_head_change_with_unknown_previous_commit(repo: Path) -> None:
    change = observer(repo).head_change("0" * 40, head(repo))
    assert change["relation"] == "unknown"
    commits = change["commits"]
    assert isinstance(commits, list) and len(commits) == 1


def test_head_change_truncates_long_histories(repo: Path) -> None:
    before = head(repo)
    for i in range(25):
        write(repo / "counter.txt", str(i))
        commit_all(repo, f"Commit {i}")
    change = observer(repo).head_change(before, head(repo))
    commits = change["commits"]
    assert isinstance(commits, list) and len(commits) == 20
    assert change["commits_truncated"] is True


# -- summaries ---------------------------------------------------------------------------


def test_describe_snapshot_messages(repo: Path) -> None:
    clean = state_of(repo).payload()
    initial, _ = describe_snapshot(None, clean)
    assert initial.startswith("Git repository: on branch main at ")
    assert initial.endswith("working tree clean")

    write(repo / "new.txt", "x")
    dirty = state_of(repo).payload()
    text, changed = describe_snapshot(clean, dirty)
    assert text == "Working tree became dirty"
    assert "state" in changed and "untracked" in changed

    gone = unavailable_payload(GitUnavailable.NOT_A_REPOSITORY)
    assert describe_snapshot(dirty, gone)[0] == (
        "Git repository no longer observable (not_a_repository)"
    )


def test_observing_never_mutates_the_repository(repo: Path) -> None:
    write(repo / "app.py", "changed\n")
    write(repo / "untracked.txt", "x")
    git(repo, "add", "untracked.txt")
    write(repo / "untracked.txt", "changed again")
    before = git_dir_state(repo)
    obs = observer(repo)
    for _ in range(3):
        assert obs.observe().state is not None
    obs.head_change(head(repo), head(repo))
    assert git_dir_state(repo) == before
