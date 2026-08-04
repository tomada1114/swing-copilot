"""Shared test fixtures."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from swing_copilot.config import Settings, load_settings
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import NoReturn

# `DailyDependencies.output_dir` defaults to the repo-relative "reports", so a
# test that forgets to override it silently overwrites the operator's real
# `reports/latest.md` with fixture data (observed 2026-08-03). Resolve it
# absolutely: tests that `monkeypatch.chdir` must still be measured against the
# repository's directory, not against whatever "reports" means inside tmp_path.
_REPO_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test crosses an uninjected external boundary."""

    def blocked_connect(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "Real network access is forbidden in the test suite"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


def _mtime_ns(path: Path) -> int | None:
    """Modification time in nanoseconds, or `None` when the path is absent."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _reports_fingerprint() -> tuple[int | None, int | None]:
    """Cheap signature of the repo's real report directory.

    Two stat calls, not a tree walk: the suite runs this around every test.
    `latest.md` is watched alongside the directory because
    `write_markdown_report` always rewrites it, including when the dated
    subdirectory already exists and the parent's mtime therefore does not move.
    """
    return (
        _mtime_ns(_REPO_REPORTS_DIR),
        _mtime_ns(_REPO_REPORTS_DIR / "latest.md"),
    )


@pytest.fixture(autouse=True)
def _block_repo_report_writes() -> Iterator[None]:
    """Fail the test that writes into the repository's real `reports/`.

    Filesystem tests must stay in `tmp_path`. This catches the omission the
    socket blocker cannot: a default output path that resolves to real,
    operator-owned data rather than to an external boundary.
    """
    before = _reports_fingerprint()
    yield
    after = _reports_fingerprint()
    if before != after:
        msg = (
            f"Test wrote into the repository's real {_REPO_REPORTS_DIR}/ "
            "directory. Pass an isolated output directory (tmp_path) instead."
        )
        raise AssertionError(msg)


@pytest.fixture
def settings() -> Settings:
    return load_settings("config/settings.yaml")


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    """Initialized isolated state store for storage/paper contract tests."""
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store
