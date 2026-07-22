"""Shared test fixtures."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

from swing_copilot.config import Settings, load_settings
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from pathlib import Path
    from typing import NoReturn


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test crosses an uninjected external boundary."""

    def blocked_connect(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "Real network access is forbidden in the test suite"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


@pytest.fixture
def settings() -> Settings:
    return load_settings("config/settings.yaml")


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    """Initialized isolated state store for storage/paper contract tests."""
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store
