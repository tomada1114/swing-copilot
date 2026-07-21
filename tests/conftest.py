"""Shared test fixtures."""

from __future__ import annotations

import pytest

from swing_copilot.config import Settings, load_settings


@pytest.fixture
def settings() -> Settings:
    return load_settings("config/settings.yaml")
