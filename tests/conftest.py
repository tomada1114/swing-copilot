"""Shared test fixtures."""

from __future__ import annotations

import socket

import pytest

from swing_copilot.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Fail fast if a test crosses an uninjected external boundary."""

    def blocked_connect(*_args, **_kwargs):
        msg = "Real network access is forbidden in the test suite"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


@pytest.fixture
def settings() -> Settings:
    return load_settings("config/settings.yaml")
