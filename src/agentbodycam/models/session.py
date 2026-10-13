"""Session metadata persisted as ``session.json``."""

from __future__ import annotations

import platform
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from agentbodycam.models.events import (
    EventValidationError,
    JsonObject,
    format_timestamp,
    parse_timestamp,
    require_aware,
    require_enum,
    require_int,
    require_str,
)

SESSION_SCHEMA_VERSION = 1

# UTC start time for natural sort order plus 64 random bits. Lowercase only, so IDs never
# collide on case-insensitive filesystems and can never form a path or a reserved name.
SESSION_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$")


class InvalidSessionIdError(ValueError):
    """A session ID is malformed or could escape the session directory."""


def new_session_id(now: datetime | None = None) -> str:
    moment = require_aware(now or datetime.now(UTC), "now")
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(8)}"


def validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise InvalidSessionIdError(f"invalid session ID: {session_id!r}")
    return session_id


class SessionStatus(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """Coarse platform facts. Deliberately excludes hostname, username and environment."""

    os_name: str
    os_release: str
    machine: str
    python_version: str
    python_implementation: str

    @classmethod
    def current(cls) -> PlatformInfo:
        info = sys.version_info
        return cls(
            os_name=platform.system() or "unknown",
            os_release=platform.release(),
            machine=platform.machine(),
            python_version=f"{info.major}.{info.minor}.{info.micro}",
            python_implementation=platform.python_implementation(),
        )

    def to_dict(self) -> JsonObject:
        return {
            "os_name": self.os_name,
            "os_release": self.os_release,
            "machine": self.machine,
            "python_version": self.python_version,
            "python_implementation": self.python_implementation,
        }

    @classmethod
    def from_dict(cls, data: object) -> PlatformInfo:
        if not isinstance(data, dict):
            raise EventValidationError("platform must be a JSON object")
        return cls(
            os_name=require_str(data.get("os_name"), "platform.os_name"),
            os_release=require_str(data.get("os_release"), "platform.os_release"),
            machine=require_str(data.get("machine"), "platform.machine"),
            python_version=require_str(data.get("python_version"), "platform.python_version"),
            python_implementation=require_str(
                data.get("python_implementation"), "platform.python_implementation"
            ),
        )


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    project_root: str
    started_at: datetime
    agentbodycam_version: str
    platform: PlatformInfo
    status: SessionStatus = SessionStatus.CREATED
    ended_at: datetime | None = None
    schema_version: int = SESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if require_int(self.schema_version, "schema_version") != SESSION_SCHEMA_VERSION:
            raise EventValidationError(f"unsupported session schema_version: {self.schema_version}")
        validate_session_id(self.session_id)
        if not require_str(self.project_root, "project_root"):
            raise EventValidationError("project_root must not be empty")
        object.__setattr__(self, "started_at", require_aware(self.started_at, "started_at"))
        if self.ended_at is not None:
            ended = require_aware(self.ended_at, "ended_at")
            if ended < self.started_at:
                raise EventValidationError("ended_at must not be before started_at")
            object.__setattr__(self, "ended_at", ended)
        object.__setattr__(self, "status", require_enum(SessionStatus, self.status, "status"))

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "project_root": self.project_root,
            "started_at": format_timestamp(self.started_at),
            "ended_at": None if self.ended_at is None else format_timestamp(self.ended_at),
            "status": self.status.value,
            "agentbodycam_version": self.agentbodycam_version,
            "platform": self.platform.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> Session:
        if not isinstance(data, dict):
            raise EventValidationError("session must be a JSON object")
        ended = data.get("ended_at")
        return cls(
            schema_version=require_int(data.get("schema_version"), "schema_version"),
            session_id=validate_session_id(data.get("session_id")),
            project_root=require_str(data.get("project_root"), "project_root"),
            started_at=parse_timestamp(data.get("started_at"), "started_at"),
            ended_at=None if ended is None else parse_timestamp(ended, "ended_at"),
            status=require_enum(SessionStatus, data.get("status"), "status"),
            agentbodycam_version=require_str(
                data.get("agentbodycam_version"), "agentbodycam_version"
            ),
            platform=PlatformInfo.from_dict(data.get("platform")),
        )
