"""Typed evidence events recorded during an AgentBodycam session.

An :class:`Event` is an immutable, fully stamped record as stored on disk. Producers
(recorder, adapters) never build events directly; they describe an observation with an
:class:`EventDraft` and the event store assigns identity, ordering and timestamps.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias, TypeVar

EVENT_SCHEMA_VERSION = 1

MAX_SUMMARY_CHARS = 1_000
MAX_PATH_CHARS = 4_096
# Events describe observations; they must not carry file contents or large logs.
MAX_EVENT_BYTES = 256 * 1024

JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
JsonObject: TypeAlias = dict[str, JsonValue]

EVENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

_E = TypeVar("_E", bound=StrEnum)


class EventValidationError(ValueError):
    """An event, draft or session record does not satisfy the schema."""


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_STOPPED = "session.stopped"
    FILE_CREATED = "file.created"
    FILE_MODIFIED = "file.modified"
    FILE_DELETED = "file.deleted"
    GIT_SNAPSHOT = "git.snapshot"
    COMMAND_STARTED = "command.started"
    COMMAND_FINISHED = "command.finished"
    CHECK_PASSED = "check.passed"
    CHECK_FAILED = "check.failed"
    ERROR_OBSERVED = "error.observed"


class EventSource(StrEnum):
    """Which component observed the event."""

    AGENTBODYCAM = "agentbodycam"
    FILESYSTEM = "filesystem"
    GIT = "git"
    COMMAND = "command"


class EvidenceCategory(StrEnum):
    SESSION = "session"
    FILE = "file"
    GIT = "git"
    COMMAND = "command"
    CHECK = "check"
    ERROR = "error"


class EvidenceKind(StrEnum):
    """Whether something was directly observed or derived from observations."""

    OBSERVED = "observed"
    INFERRED = "inferred"


CATEGORY_BY_TYPE: Mapping[EventType, EvidenceCategory] = {
    EventType.SESSION_STARTED: EvidenceCategory.SESSION,
    EventType.SESSION_STOPPED: EvidenceCategory.SESSION,
    EventType.FILE_CREATED: EvidenceCategory.FILE,
    EventType.FILE_MODIFIED: EvidenceCategory.FILE,
    EventType.FILE_DELETED: EvidenceCategory.FILE,
    EventType.GIT_SNAPSHOT: EvidenceCategory.GIT,
    EventType.COMMAND_STARTED: EvidenceCategory.COMMAND,
    EventType.COMMAND_FINISHED: EvidenceCategory.COMMAND,
    EventType.CHECK_PASSED: EvidenceCategory.CHECK,
    EventType.CHECK_FAILED: EvidenceCategory.CHECK,
    EventType.ERROR_OBSERVED: EvidenceCategory.ERROR,
}


def dumps_canonical(data: object) -> str:
    """Stable JSON encoding: sorted keys, compact separators, UTF-8 friendly, no NaN."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as a fixed-width UTC ISO 8601 string."""
    if value.tzinfo is None:
        raise EventValidationError("timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise EventValidationError(f"{field_name} must be a string timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EventValidationError(f"{field_name} is not an ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise EventValidationError(f"{field_name} must include a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise EventValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"{field_name} must be a string")
    return value


def require_optional_str(value: object, field_name: str) -> str | None:
    return None if value is None else require_str(value, field_name)


def require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EventValidationError(f"{field_name} must be an integer")
    return value


def require_enum(enum_type: type[_E], value: object, field_name: str) -> _E:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise EventValidationError(f"{field_name} must be a string: {value!r}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise EventValidationError(f"unknown {field_name}: {value!r}") from exc


def validate_relative_path(path: str) -> str:
    """Validate a normalized project-relative POSIX path such as ``src/app.py``."""
    if not path or len(path) > MAX_PATH_CHARS:
        raise EventValidationError("path must be a non-empty project-relative path")
    if "\x00" in path or "\\" in path:
        raise EventValidationError(f"path must use '/' separators only: {path!r}")
    if path.startswith("/") or _WINDOWS_DRIVE.match(path):
        raise EventValidationError(f"path must be relative to the project root: {path!r}")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise EventValidationError(
                f"path must be normalized and stay inside the project: {path!r}"
            )
    return path


def normalize_json(value: object, *, where: str = "payload") -> JsonValue:
    """Return a deep copy of ``value`` restricted to plain JSON types."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventValidationError(f"{where} contains a non-finite number")
        return value
    if isinstance(value, list | tuple):
        return [normalize_json(item, where=f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        return normalize_object(value, where=where)
    raise EventValidationError(f"{where} contains a non-JSON value of type {type(value).__name__}")


def normalize_object(value: object, *, where: str = "payload") -> JsonObject:
    if not isinstance(value, Mapping):
        raise EventValidationError(f"{where} must be a JSON object")
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise EventValidationError(f"{where} keys must be strings")
        result[key] = normalize_json(item, where=f"{where}.{key}")
    return result


def _validate_summary(summary: str) -> None:
    if not summary.strip():
        raise EventValidationError("summary must not be empty")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise EventValidationError(f"summary exceeds {MAX_SUMMARY_CHARS} characters")


@dataclass(frozen=True, slots=True)
class EventDraft:
    """An observation waiting to be stamped and appended by the event store."""

    type: EventType
    source: EventSource
    summary: str
    path: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence: EvidenceKind = EvidenceKind.OBSERVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", require_enum(EventType, self.type, "event type"))
        object.__setattr__(self, "source", require_enum(EventSource, self.source, "source"))
        object.__setattr__(
            self, "evidence", require_enum(EvidenceKind, self.evidence, "evidence kind")
        )
        _validate_summary(require_str(self.summary, "summary"))
        if self.path is not None:
            validate_relative_path(require_str(self.path, "path"))
        object.__setattr__(self, "payload", normalize_object(self.payload))


_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "session_id",
        "sequence",
        "timestamp",
        "monotonic_ns",
        "type",
        "source",
        "category",
        "evidence",
        "summary",
        "path",
        "payload",
    }
)


@dataclass(frozen=True, slots=True)
class Event:
    """A stamped, immutable evidence event exactly as persisted in ``events.jsonl``."""

    event_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    monotonic_ns: int | None
    type: EventType
    source: EventSource
    category: EvidenceCategory
    evidence: EvidenceKind
    summary: str
    path: str | None
    payload: JsonObject
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if require_int(self.schema_version, "schema_version") != EVENT_SCHEMA_VERSION:
            raise EventValidationError(f"unsupported event schema_version: {self.schema_version}")
        if not EVENT_ID_PATTERN.match(require_str(self.event_id, "event_id")):
            raise EventValidationError(f"invalid event_id: {self.event_id!r}")
        if not require_str(self.session_id, "session_id"):
            raise EventValidationError("session_id must not be empty")
        if require_int(self.sequence, "sequence") < 1:
            raise EventValidationError(f"sequence must be >= 1: {self.sequence}")
        object.__setattr__(self, "timestamp", require_aware(self.timestamp, "timestamp"))
        if self.monotonic_ns is not None and require_int(self.monotonic_ns, "monotonic_ns") < 0:
            raise EventValidationError("monotonic_ns must be non-negative")
        event_type = require_enum(EventType, self.type, "event type")
        category = require_enum(EvidenceCategory, self.category, "category")
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "source", require_enum(EventSource, self.source, "source"))
        object.__setattr__(
            self, "evidence", require_enum(EvidenceKind, self.evidence, "evidence kind")
        )
        if CATEGORY_BY_TYPE[event_type] is not category:
            raise EventValidationError(
                f"category {category.value!r} does not match event type {event_type.value!r}"
            )
        _validate_summary(require_str(self.summary, "summary"))
        if self.path is not None:
            validate_relative_path(require_str(self.path, "path"))
        object.__setattr__(self, "payload", normalize_object(self.payload))

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": format_timestamp(self.timestamp),
            "monotonic_ns": self.monotonic_ns,
            "type": self.type.value,
            "source": self.source.value,
            "category": self.category.value,
            "evidence": self.evidence.value,
            "summary": self.summary,
            "path": self.path,
            "payload": normalize_object(self.payload),
        }

    def to_json_line(self) -> str:
        """Deterministic single-line JSON encoding, without the trailing newline."""
        return dumps_canonical(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> Event:
        if not isinstance(data, dict):
            raise EventValidationError("event must be a JSON object")
        missing = _EVENT_FIELDS - data.keys()
        unknown = data.keys() - _EVENT_FIELDS
        if missing:
            raise EventValidationError(f"event is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise EventValidationError(
                f"event has unknown fields: {', '.join(sorted(map(str, unknown)))}"
            )
        monotonic = data["monotonic_ns"]
        return cls(
            schema_version=require_int(data["schema_version"], "schema_version"),
            event_id=require_str(data["event_id"], "event_id"),
            session_id=require_str(data["session_id"], "session_id"),
            sequence=require_int(data["sequence"], "sequence"),
            timestamp=parse_timestamp(data["timestamp"], "timestamp"),
            monotonic_ns=None if monotonic is None else require_int(monotonic, "monotonic_ns"),
            type=require_enum(EventType, data["type"], "event type"),
            source=require_enum(EventSource, data["source"], "source"),
            category=require_enum(EvidenceCategory, data["category"], "category"),
            evidence=require_enum(EvidenceKind, data["evidence"], "evidence kind"),
            summary=require_str(data["summary"], "summary"),
            path=require_optional_str(data["path"], "path"),
            payload=normalize_object(data["payload"]),
        )
