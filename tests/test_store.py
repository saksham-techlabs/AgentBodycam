from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agentbodycam import __version__
from agentbodycam.core.store import (
    EVENTS_FILENAME,
    GITIGNORE_CONTENT,
    SESSION_FILENAME,
    CorruptEventLogError,
    CorruptSessionError,
    EventStore,
    EvidenceRejectedError,
    SessionNotFoundError,
    StoreError,
    UnsafePathError,
)
from agentbodycam.models.events import (
    MAX_EVENT_BYTES,
    Event,
    EventDraft,
    EventSource,
    EventType,
    EventValidationError,
    EvidenceCategory,
    EvidenceKind,
)
from agentbodycam.models.session import InvalidSessionIdError, SessionStatus

StoreFactory = Callable[[Path], EventStore]


def draft(n: int = 0, **overrides: object) -> EventDraft:
    fields: dict[str, object] = {
        "type": EventType.FILE_MODIFIED,
        "source": EventSource.FILESYSTEM,
        "summary": f"Modified file {n}",
        "path": f"src/file_{n}.py",
        "payload": {"n": n},
    }
    fields.update(overrides)
    return EventDraft(**fields)  # type: ignore[arg-type]


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def raw_line(store: EventStore, session_id: str, **overrides: object) -> str:
    """A valid serialized event for ``session_id`` with selected fields replaced."""
    data: dict[str, object] = {
        "schema_version": 1,
        "event_id": "0" * 32,
        "session_id": session_id,
        "sequence": 1,
        "timestamp": "2026-10-14T02:45:00.000000Z",
        "monotonic_ns": None,
        "type": "file.created",
        "source": "filesystem",
        "category": "file",
        "evidence": "observed",
        "summary": "Created a.txt",
        "path": "a.txt",
        "payload": {},
    }
    data.update(overrides)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


# -- session creation -------------------------------------------------------------------


def test_create_session_builds_expected_layout(store: EventStore, project: Path) -> None:
    session = store.create_session()
    sid = session.session_id
    assert tree(project) == [
        ".agentbodycam",
        ".agentbodycam/.gitignore",
        ".agentbodycam/sessions",
        f".agentbodycam/sessions/{sid}",
        f".agentbodycam/sessions/{sid}/{EVENTS_FILENAME}",
        f".agentbodycam/sessions/{sid}/{SESSION_FILENAME}",
    ]
    assert store.session_dir(sid) == project.resolve() / ".agentbodycam" / "sessions" / sid


def test_created_session_is_empty_and_not_recording(store: EventStore) -> None:
    session = store.create_session()
    assert session.status is SessionStatus.CREATED
    assert session.ended_at is None
    assert session.agentbodycam_version == __version__
    assert store.events_path(session.session_id).read_bytes() == b""
    assert store.read_events(session.session_id) == []
    assert store.count_events(session.session_id) == 0


def test_session_json_is_readable_and_deterministic(store: EventStore, project: Path) -> None:
    session = store.create_session()
    path = store.session_dir(session.session_id) / SESSION_FILENAME
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["session_id"] == session.session_id
    assert data["project_root"] == str(project.resolve())
    assert data["status"] == "created"
    assert data["started_at"] == "2026-10-14T02:45:00.000000Z"
    assert list(data) == sorted(data)
    assert text.endswith("\n")
    assert store.load_session(session.session_id) == session


def test_gitignore_created_to_ignore_everything(store: EventStore, project: Path) -> None:
    store.create_session()
    gitignore = project / ".agentbodycam" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == GITIGNORE_CONTENT
    assert "*" in gitignore.read_text(encoding="utf-8").splitlines()


def test_existing_gitignore_is_preserved(store: EventStore, project: Path) -> None:
    (project / ".agentbodycam").mkdir()
    (project / ".agentbodycam" / ".gitignore").write_text("*\n# mine\n", encoding="utf-8")
    store.create_session()
    assert (project / ".agentbodycam" / ".gitignore").read_text(encoding="utf-8") == "*\n# mine\n"


def test_create_does_not_touch_project_files(store: EventStore, project: Path) -> None:
    (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "src").mkdir()
    store.create_session()
    store.create_session()
    outside = [p for p in tree(project) if not p.startswith(".agentbodycam")]
    assert outside == ["app.py", "src"]
    assert (project / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_session_ids_are_unique_across_creates(store: EventStore) -> None:
    ids = {store.create_session().session_id for _ in range(25)}
    assert len(ids) == 25


def test_create_retries_on_id_collision(store: EventStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentbodycam.core import store as store_module

    ids = iter(["20261014T024500Z-aaaaaaaaaaaaaaaa"] * 2 + ["20261014T024500Z-bbbbbbbbbbbbbbbb"])
    monkeypatch.setattr(store_module, "new_session_id", lambda _now: next(ids))
    assert store.create_session().session_id.endswith("aaaaaaaaaaaaaaaa")
    assert store.create_session().session_id.endswith("bbbbbbbbbbbbbbbb")


def test_create_requires_existing_project_root(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="not a directory"):
        EventStore(tmp_path / "missing").create_session()
    assert not (tmp_path / "missing").exists()


# -- listing and loading ----------------------------------------------------------------


def test_list_on_fresh_project_is_empty_and_creates_nothing(
    store: EventStore, project: Path
) -> None:
    assert store.list_session_ids() == []
    assert store.list_sessions() == []
    assert tree(project) == []


def test_list_multiple_sessions_in_start_order(store: EventStore) -> None:
    created = [store.create_session() for _ in range(3)]
    listed = store.list_sessions()
    assert [s.session_id for s in listed] == [s.session_id for s in created]
    assert listed == created


def test_list_ignores_unrelated_entries(store: EventStore) -> None:
    session = store.create_session()
    (store.sessions_dir / "notes.txt").write_text("hi", encoding="utf-8")
    (store.sessions_dir / "not-a-session").mkdir()
    assert store.list_session_ids() == [session.session_id]


def test_load_unknown_session(store: EventStore) -> None:
    store.create_session()
    with pytest.raises(SessionNotFoundError):
        store.load_session("20991231T235959Z-0000000000000000")


def test_load_session_with_corrupt_metadata(store: EventStore) -> None:
    sid = store.create_session().session_id
    (store.session_dir(sid) / SESSION_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptSessionError):
        store.load_session(sid)


def test_load_session_missing_metadata(store: EventStore) -> None:
    sid = store.create_session().session_id
    (store.session_dir(sid) / SESSION_FILENAME).unlink()
    with pytest.raises(CorruptSessionError, match=r"missing session\.json"):
        store.load_session(sid)


def test_load_session_with_mismatched_id(store: EventStore) -> None:
    first = store.create_session().session_id
    second = store.create_session().session_id
    other = (store.session_dir(second) / SESSION_FILENAME).read_text(encoding="utf-8")
    (store.session_dir(first) / SESSION_FILENAME).write_text(other, encoding="utf-8")
    with pytest.raises(CorruptSessionError, match="does not match"):
        store.load_session(first)


# -- appending and reading --------------------------------------------------------------


def test_append_assigns_sequence_and_metadata(store: EventStore) -> None:
    sid = store.create_session().session_id
    first = store.append_event(sid, draft(1))
    second = store.append_event(sid, draft(2))
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.session_id == sid
    assert first.category is EvidenceCategory.FILE
    assert first.evidence is EvidenceKind.OBSERVED
    assert first.event_id != second.event_id
    assert first.timestamp < second.timestamp
    assert first.monotonic_ns is not None and second.monotonic_ns is not None
    assert first.monotonic_ns < second.monotonic_ns


def test_read_preserves_sequence_order(store: EventStore) -> None:
    sid = store.create_session().session_id
    appended = [store.append_event(sid, draft(n)) for n in range(50)]
    events = store.read_events(sid)
    assert events == appended
    assert [e.sequence for e in events] == list(range(1, 51))
    assert [e.payload["n"] for e in events] == list(range(50))


def test_events_file_is_canonical_jsonl(store: EventStore) -> None:
    sid = store.create_session().session_id
    events = [store.append_event(sid, draft(n)) for n in range(3)]
    data = store.events_path(sid).read_bytes()
    assert b"\r" not in data
    assert data.decode("utf-8") == "".join(e.to_json_line() + "\n" for e in events)


def test_append_is_append_only(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    before = store.events_path(sid).read_bytes()
    store.append_event(sid, draft(2))
    assert store.events_path(sid).read_bytes().startswith(before)


def test_append_rejects_inferred_evidence(store: EventStore) -> None:
    sid = store.create_session().session_id
    with pytest.raises(EvidenceRejectedError):
        store.append_event(sid, draft(1, evidence=EvidenceKind.INFERRED))
    assert store.read_events(sid) == []


def test_append_rejects_oversized_events(store: EventStore) -> None:
    sid = store.create_session().session_id
    with pytest.raises(EventValidationError, match="limit"):
        store.append_event(sid, draft(1, payload={"blob": "x" * MAX_EVENT_BYTES}))
    assert store.events_path(sid).read_bytes() == b""
    assert store.append_event(sid, draft(2)).sequence == 1


def test_append_to_unknown_session(store: EventStore) -> None:
    store.create_session()
    with pytest.raises(SessionNotFoundError):
        store.append_event("20991231T235959Z-0000000000000000", draft())


def test_append_continues_after_other_writer(
    store: EventStore, project: Path, make_store: StoreFactory
) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    other = make_store(project)
    assert other.append_event(sid, draft(2)).sequence == 2
    assert store.append_event(sid, draft(3)).sequence == 3
    assert [e.sequence for e in store.read_events(sid)] == [1, 2, 3]


def test_new_store_instance_reads_existing_events(store: EventStore, project: Path) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    reopened = EventStore(project)
    assert reopened.append_event(sid, draft(2)).sequence == 2
    assert len(reopened.read_events(sid)) == 2


def test_unicode_payload_and_paths_round_trip(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.append_event(
        sid,
        draft(1, summary="Modified 日本語 file", path="docs/ünï cødé.md", payload={"k": "ß✓"}),
    )
    [event] = store.read_events(sid)
    assert event.path == "docs/ünï cødé.md"
    assert event.payload == {"k": "ß✓"}
    assert "日本語".encode() in store.events_path(sid).read_bytes()


def test_all_event_types_can_be_stored(store: EventStore) -> None:
    sid = store.create_session().session_id
    for event_type in EventType:
        store.append_event(
            sid,
            EventDraft(type=event_type, source=EventSource.AGENTBODYCAM, summary=event_type.value),
        )
    assert [e.type for e in store.read_events(sid)] == list(EventType)


# -- corrupt logs -----------------------------------------------------------------------


def write_log(store: EventStore, sid: str, content: bytes) -> None:
    store.events_path(sid).write_bytes(content)


@pytest.mark.parametrize(
    ("content", "line", "reason"),
    [
        (b"{not json}\n", 1, "malformed JSON"),
        (b"\n", 1, "blank line"),
        (b"\xff\xfe\n", 1, "invalid UTF-8"),
        (b"[1, 2, 3]\n", 1, "invalid event"),
        (b'{"a": NaN}\n', 1, "malformed JSON"),
        (b'{"a": 1, "a": 2}\n', 1, "duplicate key"),
    ],
)
def test_malformed_lines_raise_clear_errors(
    store: EventStore, content: bytes, line: int, reason: str
) -> None:
    sid = store.create_session().session_id
    write_log(store, sid, content)
    with pytest.raises(CorruptEventLogError, match=reason) as exc:
        store.read_events(sid)
    assert exc.value.line_number == line
    assert str(store.events_path(sid)) in str(exc.value)


def test_truncated_final_line_is_reported(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    store.append_event(sid, draft(2))
    path = store.events_path(sid)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(CorruptEventLogError, match="truncated") as exc:
        store.read_events(sid)
    assert exc.value.line_number == 2


def test_corruption_in_middle_reports_line_number(store: EventStore) -> None:
    sid = store.create_session().session_id
    for n in range(3):
        store.append_event(sid, draft(n))
    path = store.events_path(sid)
    lines = path.read_bytes().split(b"\n")
    lines[1] = b'{"broken": '
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(CorruptEventLogError) as exc:
        store.read_events(sid)
    assert exc.value.line_number == 2


def test_duplicate_sequence_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    content = (
        raw_line(store, sid, sequence=1, event_id="1" * 32)
        + "\n"
        + raw_line(store, sid, sequence=1, event_id="2" * 32)
        + "\n"
    )
    write_log(store, sid, content.encode())
    with pytest.raises(CorruptEventLogError, match="duplicate or out-of-order") as exc:
        store.read_events(sid)
    assert exc.value.line_number == 2


def test_out_of_order_sequence_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    content = "".join(
        raw_line(store, sid, sequence=seq, event_id=str(seq) * 32) + "\n" for seq in (2, 1)
    )
    write_log(store, sid, content.encode())
    with pytest.raises(CorruptEventLogError, match="gap in sequence: got 2, expected 1"):
        store.read_events(sid)


def test_sequence_gap_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    content = "".join(
        raw_line(store, sid, sequence=seq, event_id=str(seq) * 32) + "\n" for seq in (1, 3)
    )
    write_log(store, sid, content.encode())
    with pytest.raises(CorruptEventLogError, match="gap in sequence: got 3, expected 2"):
        store.read_events(sid)


def test_duplicate_event_id_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    content = "".join(raw_line(store, sid, sequence=seq) + "\n" for seq in (1, 2))
    write_log(store, sid, content.encode())
    with pytest.raises(CorruptEventLogError, match="duplicate event_id"):
        store.read_events(sid)


def test_event_from_other_session_rejected(store: EventStore) -> None:
    sid = store.create_session().session_id
    other = "20200101T000000Z-ffffffffffffffff"
    write_log(store, sid, (raw_line(store, other) + "\n").encode())
    with pytest.raises(CorruptEventLogError, match="belongs to session"):
        store.read_events(sid)


def test_valid_handwritten_line_is_accepted(store: EventStore) -> None:
    sid = store.create_session().session_id
    write_log(store, sid, (raw_line(store, sid) + "\n").encode())
    [event] = store.read_events(sid)
    assert isinstance(event, Event)
    assert event.sequence == 1


def test_append_refuses_to_extend_corrupt_log(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    path = store.events_path(sid)
    corrupted = path.read_bytes()[:-5]
    path.write_bytes(corrupted)
    with pytest.raises(CorruptEventLogError):
        store.append_event(sid, draft(2))
    assert path.read_bytes() == corrupted


def test_missing_events_file_is_reported(store: EventStore) -> None:
    sid = store.create_session().session_id
    (store.session_dir(sid) / EVENTS_FILENAME).unlink()
    with pytest.raises(CorruptSessionError, match=r"missing events\.jsonl"):
        store.read_events(sid)


# -- path safety ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["..", "../..", "../../outside", "..\\..\\outside", "/etc", "C:\\Windows", "", "CON"],
)
def test_unsafe_session_ids_are_rejected_everywhere(
    store: EventStore, project: Path, bad_id: str
) -> None:
    store.create_session()
    before = tree(project.parent)
    for operation in (
        lambda: store.session_dir(bad_id),
        lambda: store.load_session(bad_id),
        lambda: store.read_events(bad_id),
        lambda: store.count_events(bad_id),
        lambda: store.append_event(bad_id, draft()),
    ):
        with pytest.raises(InvalidSessionIdError):
            operation()
    assert tree(project.parent) == before


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_symlinked_store_root_is_rejected(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(project / ".agentbodycam", outside)
    store = EventStore(project)
    with pytest.raises(UnsafePathError):
        store.create_session()
    with pytest.raises(UnsafePathError):
        store.list_session_ids()
    assert list(outside.iterdir()) == []


def test_symlinked_session_dir_is_rejected(store: EventStore, tmp_path: Path) -> None:
    store.create_session()
    outside = tmp_path / "outside"
    outside.mkdir()
    sid = "20261014T024500Z-cccccccccccccccc"
    _symlink_or_skip(store.sessions_dir / sid, outside)
    with pytest.raises(UnsafePathError):
        store.load_session(sid)
    with pytest.raises(UnsafePathError):
        store.append_event(sid, draft())
    assert list(outside.iterdir()) == []


def test_store_root_that_is_a_file_is_rejected(project: Path) -> None:
    (project / ".agentbodycam").write_text("not a dir", encoding="utf-8")
    with pytest.raises(UnsafePathError):
        EventStore(project).create_session()
    assert (project / ".agentbodycam").read_text(encoding="utf-8") == "not a dir"


def test_sessions_path_that_is_a_file_is_rejected(project: Path) -> None:
    (project / ".agentbodycam").mkdir()
    (project / ".agentbodycam" / "sessions").write_text("x", encoding="utf-8")
    with pytest.raises(UnsafePathError):
        EventStore(project).create_session()


# -- awkward project paths --------------------------------------------------------------


@pytest.mark.parametrize("name", ["with spaces", "ünïcødé プロジェクト", "dots.and-dashes"])
def test_awkward_project_paths(tmp_path: Path, name: str, make_store: StoreFactory) -> None:
    root = tmp_path / name / "nested dir"
    root.mkdir(parents=True)
    store = make_store(root)
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    assert store.load_session(sid).project_root == str(root.resolve())
    assert store.count_events(sid) == 1
    assert store.list_session_ids() == [sid]


def test_relative_project_root_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path)
    store = EventStore(Path("proj"))
    assert store.project_root == (tmp_path / "proj").resolve()
    assert Path(store.create_session().project_root).is_absolute()


# -- privacy ----------------------------------------------------------------------------


def test_environment_variables_are_not_stored(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "agentbodycam-test-sentinel-value-9f8e7d"
    monkeypatch.setenv("AGENTBODYCAM_TEST_SECRET", sentinel)
    sid = store.create_session().session_id
    store.append_event(sid, draft(1))
    for path in store.session_dir(sid).iterdir():
        content = path.read_text(encoding="utf-8")
        assert sentinel not in content
        assert "AGENTBODYCAM_TEST_SECRET" not in content
