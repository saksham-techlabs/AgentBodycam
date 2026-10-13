from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentbodycam.core.store import EventStore


class FakeClock:
    """Deterministic UTC clock that advances one millisecond per call."""

    def __init__(self, start: datetime = datetime(2026, 10, 14, 2, 45, tzinfo=UTC)) -> None:
        self.now = start

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(milliseconds=1)
        return current


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 1_000

    def __call__(self) -> int:
        self.value += 1_000
        return self.value


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "my project"
    root.mkdir()
    return root


@pytest.fixture
def make_store() -> Callable[[Path], EventStore]:
    def factory(root: Path) -> EventStore:
        return EventStore(root, clock=FakeClock(), monotonic_ns=FakeMonotonic())

    return factory


@pytest.fixture
def store(project: Path, make_store: Callable[[Path], EventStore]) -> EventStore:
    return make_store(project)
