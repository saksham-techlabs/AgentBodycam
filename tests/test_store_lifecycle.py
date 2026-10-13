from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbodycam.core.store import EventStore, SessionNotFoundError, SessionStateError
from agentbodycam.models.session import SessionStatus


def session_json(store: EventStore, session_id: str) -> dict[str, object]:
    data = json.loads((store.session_dir(session_id) / "session.json").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def test_mark_recording_then_stopped(store: EventStore) -> None:
    sid = store.create_session().session_id
    recording = store.mark_recording(sid)
    assert recording.status is SessionStatus.RECORDING
    assert recording.ended_at is None
    assert session_json(store, sid)["status"] == "recording"

    stopped = store.mark_stopped(sid)
    assert stopped.status is SessionStatus.STOPPED
    assert stopped.ended_at is not None
    assert stopped.ended_at >= stopped.started_at
    on_disk = session_json(store, sid)
    assert on_disk["status"] == "stopped"
    assert on_disk["ended_at"] is not None
    assert store.load_session(sid) == stopped


def test_transitions_leave_no_temp_files(store: EventStore) -> None:
    sid = store.create_session().session_id
    store.mark_recording(sid)
    store.mark_stopped(sid)
    assert sorted(p.name for p in store.session_dir(sid).iterdir()) == [
        "events.jsonl",
        "session.json",
    ]


@pytest.mark.parametrize("steps", [["stop"], ["record", "record"], ["record", "stop", "stop"]])
def test_invalid_transitions_rejected(store: EventStore, steps: list[str]) -> None:
    sid = store.create_session().session_id
    *ok, bad = steps
    for step in ok:
        (store.mark_recording if step == "record" else store.mark_stopped)(sid)
    before = session_json(store, sid)
    with pytest.raises(SessionStateError):
        (store.mark_recording if bad == "record" else store.mark_stopped)(sid)
    assert session_json(store, sid) == before


def test_transition_unknown_session(project: Path, store: EventStore) -> None:
    store.ensure_layout()
    with pytest.raises(SessionNotFoundError):
        store.mark_recording("20991231T235959Z-0000000000000000")
