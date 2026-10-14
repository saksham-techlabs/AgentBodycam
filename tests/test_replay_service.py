"""ReplayService: the transport-independent, read-only replay queries."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentbodycam.core.forensics import analyze_session
from agentbodycam.core.replay import ReplayError, ReplayErrorCode, ReplayService
from agentbodycam.core.store import EventStore
from agentbodycam.models.forensics import RecordingState, TimelineItem
from agentbodycam.models.replay import TIMELINE_MAX_LIMIT
from forensics_helpers import SessionBuilder, probe_active, probe_inactive
from recording_helpers import try_symlink
from replay_helpers import store_snapshot

TIMELINE_KEYS = {
    "sequence",
    "event_id",
    "timestamp",
    "type",
    "category",
    "source",
    "summary",
    "path",
    "command_id",
    "evidence",
}
MALFORMED_IDS = [
    "",
    "..",
    "../../etc/passwd",
    "..\\..\\windows",
    "%2e%2e",
    "%2E%2E%2Fetc",
    "20261014T000000Z-0000000000000000%2f..",
    "20261014T000000Z-0000000000000000/..",
    "20261014T000000Z-ABCDEF0000000000",
    "20261014T000000Z-000000000000000",
    " 20261014T000000Z-0000000000000000",
    "C:\\Windows",
    "/etc/passwd",
]


def _code(exc: pytest.ExceptionInfo[ReplayError]) -> ReplayErrorCode:
    return exc.value.code


def _long_session(store: EventStore, files: int) -> str:
    builder = SessionBuilder(store)
    for number in range(files):
        builder.file("created" if number % 3 == 0 else "modified", f"src/f{number % 7}.py")
    builder.stop()
    return builder.session_id


@pytest.fixture
def stopped(store: EventStore) -> str:
    """PASS, edit, FAIL, edit, PASS of the same check, then a clean stop."""
    b = SessionBuilder(store)
    b.run_check(True)
    b.file("modified", "app.py")
    b.run_check(False)
    b.file("modified", "app.py")
    b.git(state="dirty", counts=(0, 1, 0, 0))
    b.run_check(True)
    b.stop()
    return b.session_id


def test_empty_project_creates_nothing(project: Path) -> None:
    service = ReplayService(project)
    assert service.project().to_dict() == {"api_version": 1, "session_count": 0}
    assert service.list_sessions().to_dict() == {"api_version": 1, "sessions": [], "total": 0}
    sid = "20261014T000000Z-0000000000000000"
    for call in (service.session_detail, service.report, service.timeline):
        with pytest.raises(ReplayError) as exc:
            call(sid)
        assert _code(exc) is ReplayErrorCode.SESSION_NOT_FOUND
        assert exc.value.message == "Session not found."
    assert not (project / ".agentbodycam").exists()


def test_missing_project_directory_is_empty(tmp_path: Path) -> None:
    service = ReplayService(tmp_path / "does-not-exist")
    assert service.project().session_count == 0
    assert not (tmp_path / "does-not-exist").exists()


def test_session_list_newest_first_with_states(store: EventStore) -> None:
    stopped = SessionBuilder(store)
    stopped.stop()
    crashed = SessionBuilder(store)  # status recording, no live recorder
    crashed.file("created", "a.txt")
    created = SessionBuilder(store, start=False)
    live = SessionBuilder(store)
    service = ReplayService(store.project_root, probe=probe_active(live.session_id))

    listing = service.list_sessions()
    ids = [s.session_id for s in listing.sessions]
    assert ids == [live.session_id, created.session_id, crashed.session_id, stopped.session_id]
    assert listing.to_dict()["total"] == 4
    by_id = {s.session_id: s for s in listing.sessions}
    session = store.load_session(stopped.session_id)
    assert by_id[stopped.session_id].to_dict() == {
        "session_id": stopped.session_id,
        "readable": True,
        "status": "stopped",
        "recording_state": "stopped",
        "analysis_complete": True,
        "started_at": session.to_dict()["started_at"],
        "ended_at": session.to_dict()["ended_at"],
        "event_count": 2,
    }
    states = {sid: (s.recording_state, s.analysis_complete) for sid, s in by_id.items()}
    assert states[crashed.session_id] == (RecordingState.ENDED_WITHOUT_STOP, True)
    assert states[live.session_id] == (RecordingState.RECORDING, False)
    assert states[created.session_id] == (RecordingState.NOT_STARTED, False)
    assert service.project().session_count == 4
    assert listing.to_dict() == service.list_sessions().to_dict()  # deterministic


def test_unreadable_session_is_listed_not_fatal(store: EventStore, stopped: str) -> None:
    broken = SessionBuilder(store)
    with store.events_path(broken.session_id).open("ab") as fh:
        fh.write(b"{not json\n")
    service = ReplayService(store.project_root, probe=probe_inactive)
    sessions = service.list_sessions().sessions
    # Same start second: the unreadable one (start time unknown) sorts last.
    assert [(s.session_id, s.readable) for s in sessions] == [
        (stopped, True),
        (broken.session_id, False),
    ]
    assert sessions[1].to_dict()["status"] is None
    for call in (service.session_detail, service.report, service.timeline):
        with pytest.raises(ReplayError) as exc:
            call(broken.session_id)
        assert _code(exc) is ReplayErrorCode.SESSION_UNREADABLE
        assert str(store.project_root) not in exc.value.message
        assert "events.jsonl" not in exc.value.message


def test_session_detail_comes_from_the_forensic_report(store: EventStore, stopped: str) -> None:
    service = ReplayService(store.project_root, probe=probe_inactive)
    detail = service.session_detail(stopped).to_dict()
    report = analyze_session(store, stopped, probe=probe_inactive)
    assert detail == {
        "api_version": 1,
        "session_id": stopped,
        "status": "stopped",
        "recording_state": "stopped",
        "analysis_complete": True,
        "started_at": report.started_at,
        "ended_at": report.ended_at,
        "event_count": report.event_count,
        "event_counts": dict(report.event_counts),
        "command_summary": report.command_summary.to_dict(),
        "observable_failure_count": 1,
        "recovery_count": 1,
        "unresolved_failure_count": 0,
        "warning_count": 0,
    }
    assert "project_root" not in detail and "platform" not in detail


def test_report_is_exactly_the_forensic_report(store: EventStore, stopped: str) -> None:
    service = ReplayService(store.project_root, probe=probe_inactive)
    expected = analyze_session(store, stopped, probe=probe_inactive)
    assert service.report_dict(stopped) == expected.to_dict()
    assert service.report(stopped).to_json() == expected.to_json()
    assert service.report_dict(stopped) == service.report_dict(stopped)


def test_live_and_crashed_reports(store: EventStore) -> None:
    live = SessionBuilder(store)
    live.file("created", "a.txt")
    live_report = ReplayService(store.project_root, probe=probe_active(live.session_id)).report(
        live.session_id
    )
    assert live_report.recording_state is RecordingState.RECORDING
    assert live_report.analysis_complete is False
    crashed = ReplayService(store.project_root, probe=probe_inactive).report(live.session_id)
    assert crashed.recording_state is RecordingState.ENDED_WITHOUT_STOP
    assert crashed.analysis_complete is True
    assert store.load_session(live.session_id).status.value == "recording"  # not repaired


@pytest.mark.parametrize("bad", MALFORMED_IDS)
def test_malformed_session_ids(store: EventStore, stopped: str, bad: str) -> None:
    service = ReplayService(store.project_root, probe=probe_inactive)
    for call in (service.session_detail, service.report, service.timeline):
        with pytest.raises(ReplayError) as exc:
            call(bad)
        assert _code(exc) is ReplayErrorCode.INVALID_SESSION_ID


# -- timeline pagination --------------------------------------------------------------------


LongSession = tuple[ReplayService, str, EventStore]


@pytest.fixture(scope="module")
def long_session(tmp_path_factory: pytest.TempPathFactory) -> LongSession:
    """Built once (these tests only read it): 248 file events + start/stop = 250 events."""
    store = EventStore(tmp_path_factory.mktemp("long"))
    return ReplayService(store.project_root), _long_session(store, 248), store


def test_timeline_default_page(long_session: LongSession) -> None:
    service, sid, _ = long_session
    page = service.timeline(sid)
    data = page.to_dict()
    assert data["api_version"] == 1 and data["session_id"] == sid
    assert (data["after_sequence"], data["limit"], data["total"]) == (0, 100, 250)
    assert [i.sequence for i in page.items] == list(range(1, 101))
    assert (data["next_after_sequence"], data["has_more"]) == (100, True)
    items = data["items"]
    assert isinstance(items, list) and len(items) == 100
    assert all(isinstance(item, dict) and set(item) == TIMELINE_KEYS for item in items)


def test_timeline_cursor_and_limits(long_session: LongSession) -> None:
    service, sid, _ = long_session
    page = service.timeline(sid, after_sequence=240)
    assert [i.sequence for i in page.items] == list(range(241, 251))
    assert (page.next_after_sequence, page.has_more) == (250, False)
    one = service.timeline(sid, after_sequence=7, limit=1)
    assert [i.sequence for i in one.items] == [8]
    assert (one.next_after_sequence, one.has_more) == (8, True)
    big = service.timeline(sid, limit=TIMELINE_MAX_LIMIT)
    assert len(big.items) == 250 and not big.has_more
    beyond = service.timeline(sid, after_sequence=10_000)
    assert beyond.items == () and beyond.next_after_sequence == 10_000 and not beyond.has_more
    at_end = service.timeline(sid, after_sequence=250)
    assert at_end.items == () and not at_end.has_more


@pytest.mark.parametrize(
    ("after", "limit"), [(-1, 10), (0, 0), (0, -5), (0, TIMELINE_MAX_LIMIT + 1), (-3, 0)]
)
def test_timeline_rejects_out_of_range(long_session: LongSession, after: int, limit: int) -> None:
    service, sid, _ = long_session
    with pytest.raises(ReplayError) as exc:
        service.timeline(sid, after_sequence=after, limit=limit)
    assert _code(exc) is ReplayErrorCode.INVALID_PARAMETER


@pytest.mark.parametrize("limit", [1, 7, 100, 500])
def test_timeline_pages_reproduce_the_forensic_timeline(
    long_session: LongSession, limit: int
) -> None:
    service, sid, store = long_session
    seen: list[TimelineItem] = []
    cursor, pages = 0, 0
    while True:
        page = service.timeline(sid, after_sequence=cursor, limit=limit)
        assert len(page.items) <= limit
        seen += page.items
        pages += 1
        assert page.next_after_sequence > cursor or not page.items
        cursor = page.next_after_sequence
        if not page.has_more:
            break
    assert pages == -(-250 // limit)
    assert len({item.event_id for item in seen}) == len(seen) == 250  # no duplicates
    report = analyze_session(store, sid)
    assert [item.to_dict() for item in seen] == [item.to_dict() for item in report.timeline.items]


def test_timeline_exposes_no_raw_payload(store: EventStore) -> None:
    b = SessionBuilder(store)
    b.git()  # payload holds the absolute repository root
    b.run_check(True, ["python", "-m", "pytest", "--token", "ghp_" + "Zx9" * 12])
    b.stop()
    page = ReplayService(store.project_root).timeline(b.session_id, limit=500)
    text = repr(page.to_dict())
    assert "private-root" not in text
    assert "payload" not in text and "argv" not in text
    assert "Zx9Zx9" not in text
    assert {item.command_id for item in page.items} - {None} != set()


# -- read-only -------------------------------------------------------------------------------


def test_service_is_read_only_and_runs_nothing(
    store: EventStore, stopped: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashed = SessionBuilder(store)  # status recording: the liveness probe runs for it
    crashed.git()
    before = store_snapshot(store.project_root)

    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise AssertionError("the replay service must not start processes")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    service = ReplayService(store.project_root)  # real active-session probe
    service.project()
    service.list_sessions()
    for sid in (stopped, crashed.session_id):
        service.session_detail(sid)
        service.report(sid)
        service.timeline(sid, limit=500)
    assert store_snapshot(store.project_root) == before


def test_symlinked_store_is_refused(project: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sessions").mkdir(parents=True)
    if not try_symlink(project / ".agentbodycam", elsewhere, directory=True):
        pytest.skip("cannot create a directory symlink here")
    service = ReplayService(project)
    for call in (service.project, service.list_sessions):
        with pytest.raises(ReplayError) as exc:
            call()
        assert _code(exc) is ReplayErrorCode.STORE_UNSAFE
