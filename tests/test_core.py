"""Tests for the public swing_copilot API."""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
from importlib.metadata import PackageNotFoundError, version

import swing_copilot
from swing_copilot import __all__, __version__, add


class TestAdd:
    def test_positive_numbers(self):
        assert add(1, 2) == 3

    def test_negative_numbers(self):
        assert add(-1, -2) == -3

    def test_zero(self):
        assert add(0, 0) == 0


class TestPackageMetadata:
    def test_public_exports(self):
        assert set(__all__) == {"__version__", "add"}

    def test_version_matches_installed_metadata(self):
        assert __version__ == version("swing-copilot")

    def test_version_falls_back_when_package_not_installed(self, monkeypatch):
        def fake_version(_: str) -> str:
            raise PackageNotFoundError

        with monkeypatch.context() as patched:
            patched.setattr(importlib_metadata, "version", fake_version)
            reloaded = importlib.reload(swing_copilot)

        assert reloaded.__version__ == "0.0.0+unknown"
        importlib.reload(swing_copilot)
