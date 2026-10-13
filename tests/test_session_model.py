from __future__ import annotations

import json
import platform
from datetime import UTC, datetime, timedelta

import pytest

from agentbodycam.models.events import EventValidationError
from agentbodycam.models.session import (
    SESSION_ID_PATTERN,
    InvalidSessionIdError,
    PlatformInfo,
    Session,
    SessionStatus,
    new_session_id,
    validate_session_id,
)

STARTED = datetime(2026, 10, 14, 2, 45, tzinfo=UTC)


def make_session(**overrides: object) -> Session:
    fields: dict[str, object] = {
        "session_id": "20261014T024500Z-0123456789abcdef",
        "project_root": "/home/dev/project",
        "started_at": STARTED,
        "agentbodycam_version": "0.0.1",
        "platform": PlatformInfo.current(),
    }
    fields.update(overrides)
    return Session(**fields)  # type: ignore[arg-type]


def test_session_ids_are_unique() -> None:
    ids = {new_session_id() for _ in range(2000)}
    assert len(ids) == 2000


def test_session_ids_match_pattern_and_encode_start_time() -> None:
    session_id = new_session_id(STARTED)
    assert SESSION_ID_PATTERN.fullmatch(session_id)
    assert session_id.startswith("20261014T024500Z-")
    assert validate_session_id(session_id) == session_id


def test_session_ids_sort_chronologically() -> None:
    earlier = new_session_id(STARTED)
    later = new_session_id(STARTED + timedelta(seconds=1))
    assert sorted([later, earlier]) == [earlier, later]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "..",
        "../20261014T024500Z-0123456789abcdef",
        "20261014T024500Z-0123456789abcdef/..",
        "sessions/20261014T024500Z-0123456789abcdef",
        "..\\20261014T024500Z-0123456789abcdef",
        "C:\\Windows",
        "/etc/passwd",
        "CON",
        "20261014T024500Z-0123456789ABCDEF",
        "20261014T024500Z-0123456789abcdef\n",
        " 20261014T024500Z-0123456789abcdef",
        "20261014T024500Z-0123456789abcde",
        "20261014T024500Z-0123456789abcdef0",
        "20261014T024500Z-0123456789abcdeg",
        "20261014T024500Z-0123456789abcdef\x00",
        None,
        123,
    ],
)
def test_rejects_unsafe_session_ids(bad: object) -> None:
    with pytest.raises(InvalidSessionIdError):
        validate_session_id(bad)


def test_session_round_trip() -> None:
    session = make_session(status=SessionStatus.STOPPED, ended_at=STARTED + timedelta(minutes=5))
    restored = Session.from_dict(json.loads(json.dumps(session.to_dict())))
    assert restored == session


def test_new_session_defaults() -> None:
    session = make_session()
    assert session.status is SessionStatus.CREATED
    assert session.ended_at is None
    assert session.to_dict()["ended_at"] is None
    assert session.to_dict()["started_at"] == "2026-10-14T02:45:00.000000Z"


def test_end_before_start_rejected() -> None:
    with pytest.raises(EventValidationError):
        make_session(ended_at=STARTED - timedelta(seconds=1))


def test_naive_start_rejected() -> None:
    with pytest.raises(EventValidationError):
        make_session(started_at=datetime(2026, 1, 1))


def test_invalid_session_id_rejected_by_model() -> None:
    with pytest.raises(InvalidSessionIdError):
        make_session(session_id="../escape")


def test_unknown_status_rejected() -> None:
    data = make_session().to_dict()
    data["status"] = "exploded"
    with pytest.raises(EventValidationError):
        Session.from_dict(data)


def test_platform_info_excludes_identifying_details() -> None:
    info = PlatformInfo.current().to_dict()
    assert set(info) == {
        "os_name",
        "os_release",
        "machine",
        "python_version",
        "python_implementation",
    }
    assert info["python_implementation"] == platform.python_implementation()


def test_platform_from_dict_requires_fields() -> None:
    with pytest.raises(EventValidationError):
        PlatformInfo.from_dict({"os_name": "Linux"})
    with pytest.raises(EventValidationError):
        PlatformInfo.from_dict("Linux")
