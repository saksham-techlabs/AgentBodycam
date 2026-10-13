from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from agentbodycam.models.events import (
    CATEGORY_BY_TYPE,
    Event,
    EventDraft,
    EventSource,
    EventType,
    EventValidationError,
    EvidenceCategory,
    EvidenceKind,
    format_timestamp,
    validate_relative_path,
)

SESSION_ID = "20261014T024500Z-0123456789abcdef"
WHEN = datetime(2026, 10, 14, 2, 45, 0, 123456, tzinfo=UTC)


def make_event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "event_id": "a" * 32,
        "session_id": SESSION_ID,
        "sequence": 1,
        "timestamp": WHEN,
        "monotonic_ns": 42,
        "type": EventType.FILE_MODIFIED,
        "source": EventSource.FILESYSTEM,
        "category": EvidenceCategory.FILE,
        "evidence": EvidenceKind.OBSERVED,
        "summary": "Modified src/app.py",
        "path": "src/app.py",
        "payload": {"size": 10, "nested": {"ok": True, "items": [1, 2.5, None, "x"]}},
    }
    fields.update(overrides)
    return Event(**fields)


def test_every_event_type_has_a_category() -> None:
    assert set(CATEGORY_BY_TYPE) == set(EventType)


def test_event_type_values_are_dotted_names() -> None:
    assert EventType.FILE_CREATED.value == "file.created"
    assert EventType.ERROR_OBSERVED.value == "error.observed"
    assert all("." in member.value for member in EventType)


def test_draft_defaults_to_observed_evidence() -> None:
    draft = EventDraft(type=EventType.SESSION_STARTED, source=EventSource.AGENTBODYCAM, summary="x")
    assert draft.evidence is EvidenceKind.OBSERVED
    assert draft.payload == {}
    assert draft.path is None


def test_draft_coerces_string_enums() -> None:
    draft = EventDraft(type="file.created", source="filesystem", summary="x")  # type: ignore[arg-type]
    assert draft.type is EventType.FILE_CREATED
    assert draft.source is EventSource.FILESYSTEM


@pytest.mark.parametrize(
    ("field", "value"),
    [("type", "file.exploded"), ("source", "telepathy"), ("evidence", "guessed")],
)
def test_draft_rejects_unknown_enum_values(field: str, value: str) -> None:
    kwargs: dict[str, Any] = {
        "type": EventType.FILE_CREATED,
        "source": EventSource.FILESYSTEM,
        "summary": "x",
        field: value,
    }
    with pytest.raises(EventValidationError):
        EventDraft(**kwargs)


@pytest.mark.parametrize("summary", ["", "   ", "x" * 1001])
def test_draft_rejects_bad_summary(summary: str) -> None:
    with pytest.raises(EventValidationError):
        EventDraft(type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary=summary)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "C:/Windows",
        "c:relative",
        "src\\app.py",
        "../outside",
        "src/../../outside",
        "./src",
        "src//app.py",
        "src/",
        "bad\x00name",
        "..",
    ],
)
def test_rejects_unsafe_or_unnormalized_paths(path: str) -> None:
    with pytest.raises(EventValidationError):
        validate_relative_path(path)
    with pytest.raises(EventValidationError):
        EventDraft(
            type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary="x", path=path
        )


@pytest.mark.parametrize(
    "path", ["README.md", "src/app.py", "my docs/notes file.md", "docs/ünïcødé/日本語.md", ".env"]
)
def test_accepts_project_relative_paths(path: str) -> None:
    assert validate_relative_path(path) == path


@pytest.mark.parametrize(
    "payload",
    [
        {"x": object()},
        {"x": math.nan},
        {"x": math.inf},
        {"x": {1, 2}},
        {"x": b"bytes"},
        {"nested": {3: "non-string key"}},
    ],
)
def test_draft_rejects_non_json_payload(payload: dict[str, Any]) -> None:
    with pytest.raises(EventValidationError):
        EventDraft(
            type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary="x", payload=payload
        )


def test_draft_payload_is_deep_copied() -> None:
    original: dict[str, Any] = {"items": [1, 2], "meta": {"k": "v"}}
    draft = EventDraft(
        type=EventType.FILE_CREATED, source=EventSource.FILESYSTEM, summary="x", payload=original
    )
    original["items"].append(3)
    original["meta"]["k"] = "changed"
    assert draft.payload == {"items": [1, 2], "meta": {"k": "v"}}


def test_draft_tuples_become_lists() -> None:
    draft = EventDraft(
        type=EventType.FILE_CREATED,
        source=EventSource.FILESYSTEM,
        summary="x",
        payload={"t": (1, 2)},
    )
    assert draft.payload == {"t": [1, 2]}


def test_event_round_trips_through_json() -> None:
    event = make_event()
    restored = Event.from_dict(json.loads(event.to_json_line()))
    assert restored == event


def test_serialization_is_deterministic_and_compact() -> None:
    a = make_event(payload={"b": 1, "a": 2})
    b = make_event(payload={"a": 2, "b": 1})
    line = a.to_json_line()
    assert line == b.to_json_line()
    assert "\n" not in line
    assert ", " not in line
    assert ": " not in line
    keys = list(json.loads(line))
    assert keys == sorted(keys)


def test_serialization_keeps_unicode_readable() -> None:
    event = make_event(summary="Modified 日本語.md", path="docs/日本語.md")
    assert "日本語" in event.to_json_line()


def test_timestamps_are_utc_with_microseconds() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    event = make_event(timestamp=datetime(2026, 10, 14, 8, 15, 0, tzinfo=ist))
    assert event.timestamp == datetime(2026, 10, 14, 2, 45, 0, tzinfo=UTC)
    assert event.to_dict()["timestamp"] == "2026-10-14T02:45:00.000000Z"
    assert format_timestamp(WHEN) == "2026-10-14T02:45:00.123456Z"


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(EventValidationError):
        make_event(timestamp=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "not-hex"},
        {"event_id": "A" * 32},
        {"sequence": 0},
        {"sequence": -1},
        {"sequence": True},
        {"monotonic_ns": -5},
        {"category": EvidenceCategory.GIT},
        {"session_id": ""},
        {"schema_version": 2},
    ],
)
def test_event_rejects_invalid_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(EventValidationError):
        make_event(**overrides)


def test_monotonic_may_be_null() -> None:
    assert make_event(monotonic_ns=None).monotonic_ns is None


def test_event_can_represent_inferred_kind_in_schema() -> None:
    # The schema knows the distinction; the store is what refuses inferred evidence.
    assert make_event(evidence=EvidenceKind.INFERRED).evidence is EvidenceKind.INFERRED


def test_from_dict_rejects_missing_and_unknown_fields() -> None:
    data = make_event().to_dict()
    missing = dict(data)
    del missing["summary"]
    with pytest.raises(EventValidationError, match="missing fields: summary"):
        Event.from_dict(missing)
    with pytest.raises(EventValidationError, match="unknown fields: extra"):
        Event.from_dict({**data, "extra": 1})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("timestamp", "yesterday"),
        ("timestamp", "2026-10-14T02:45:00"),
        ("timestamp", 12345),
        ("sequence", "1"),
        ("type", 7),
        ("summary", None),
        ("path", 5),
        ("payload", []),
        ("schema_version", "1"),
    ],
)
def test_from_dict_rejects_wrong_types(key: str, value: object) -> None:
    data: dict[str, Any] = dict(make_event().to_dict())
    data[key] = value
    with pytest.raises(EventValidationError):
        Event.from_dict(data)


def test_from_dict_rejects_non_object() -> None:
    with pytest.raises(EventValidationError):
        Event.from_dict(["not", "an", "object"])


def test_event_is_immutable() -> None:
    event = make_event()
    with pytest.raises(AttributeError):
        event.sequence = 2  # type: ignore[misc]
