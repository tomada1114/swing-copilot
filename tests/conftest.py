"""Shared test fixtures."""

from __future__ import annotations

import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd
import pytest

from swing_copilot.config import Settings, load_settings
from swing_copilot.storage.database import Database
from swing_copilot.storage.state_store import StateStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, NoReturn

    from swing_copilot.storage.market_store import MarketStore

_REPO_ROOT = Path(__file__).resolve().parent.parent

# `DailyDependencies.output_dir` defaults to the repo-relative "reports", so a
# test that forgets to override it silently overwrites the operator's real
# `reports/latest.md` with fixture data (observed 2026-08-03). Resolve it
# absolutely: tests that `monkeypatch.chdir` must still be measured against the
# repository's directory, not against whatever "reports" means inside tmp_path.
_REPO_REPORTS_DIR = _REPO_ROOT / "reports"

# `data/` is the same trap one layer down: `DEFAULT_DB_PATH`
# ("data/copilot.duckdb"), the dry-run DB ("data/copilot_dry_run.duckdb") and
# `DEFAULT_PARQUET_ROOT` ("data/bars") are all repo-relative, so a test that
# calls a composition root without `monkeypatch.chdir(tmp_path)` opens the
# operator's real DuckDB file (Issue #233, via `_compose_dependencies`).
# Resolved absolutely for the same reason as the reports directory.
_REPO_DATA_DIR = _REPO_ROOT / "data"


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


def _fingerprint(paths: tuple[Path, ...]) -> tuple[int | None, ...]:
    """Cheap signature of one repo directory and the files watched inside it."""
    return tuple(_mtime_ns(path) for path in paths)


def _format_mtime(mtime_ns: int | None) -> str:
    """Render one watched mtime for the guard's diagnostic message."""
    if mtime_ns is None:
        return "absent"
    stamp = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC).astimezone()
    return f"{stamp.isoformat()} (mtime_ns={mtime_ns})"


def _describe_changes(
    paths: tuple[Path, ...],
    before: tuple[int | None, ...],
    after: tuple[int | None, ...],
) -> str:
    """List every watched path whose mtime moved, with its before/after value."""
    return "\n".join(
        f"  {path}: {_format_mtime(was)} -> {_format_mtime(now)}"
        for path, was, now in zip(paths, before, after, strict=True)
        if was != now
    )


@contextmanager
def _guard_repo_directory(directory: Path, *watched: Path) -> Iterator[None]:
    """Fail the running test when a repository directory it does not own changes.

    Filesystem tests must stay in `tmp_path`. This catches the omission the
    socket blocker cannot: a default output path that resolves to real,
    operator-owned data rather than to an external boundary.

    A handful of stat calls, not a tree walk: the suite runs this around every
    test. Files are watched alongside their directory because rewriting a file
    in place moves its own mtime without moving its parent's -- true of
    `write_markdown_report`, which always rewrites `reports/latest.md` even
    when the dated subdirectory already exists, and of every DuckDB file under
    `data/`.

    An mtime fingerprint proves *that* the directory changed, never *who*
    changed it: this working copy is also the unattended execution environment,
    so the 18:30 routine writing `data/copilot.duckdb` and `reports/latest.md`
    trips the guard in whatever unrelated tests happen to be in flight
    (observed 2026-08, Issue #257). The message therefore names both causes and
    reports the evidence -- which watched paths moved, and their mtimes before
    and after -- instead of asserting that the test is at fault.
    """
    paths = (directory, *watched)
    before = _fingerprint(paths)
    yield
    after = _fingerprint(paths)
    if after != before:
        msg = (
            f"The repository's real {directory}/ directory changed while this "
            "test ran. An mtime fingerprint cannot tell who wrote, so weigh "
            "both causes:\n"
            "  (1) this test wrote there -- pass an isolated path (tmp_path), "
            "or monkeypatch.chdir(tmp_path) before calling a composition root "
            "that uses the repo-relative defaults;\n"
            "  (2) a concurrent external process wrote there -- the scheduled "
            "18:30 daily routine, a manual `copilot-daily` run, or anything "
            "else sharing this checkout. Then the test is innocent: check the "
            "timestamps below against that process and re-run once it is "
            "done.\n"
            "Changed watched paths (mtime before -> after):\n"
            + _describe_changes(paths, before, after)
        )
        raise AssertionError(msg)


@pytest.fixture(autouse=True)
def _block_repo_report_writes() -> Iterator[None]:
    """Fail when the repository's real `reports/` changes during this test.

    The mtime fingerprint cannot prove this test is the writer -- see
    `_guard_repo_directory`'s docstring.
    """
    with _guard_repo_directory(_REPO_REPORTS_DIR, _REPO_REPORTS_DIR / "latest.md"):
        yield


@pytest.fixture(autouse=True)
def _block_repo_data_writes() -> Iterator[None]:
    """Fail when the repository's real `data/` changes during this test.

    The mtime fingerprint cannot prove this test is the writer -- see
    `_guard_repo_directory`'s docstring.
    """
    with _guard_repo_directory(
        _REPO_DATA_DIR,
        _REPO_DATA_DIR / "copilot.duckdb",
        _REPO_DATA_DIR / "copilot_dry_run.duckdb",
        _REPO_DATA_DIR / "bars",
    ):
        yield


_REAL_DUCKDB_CONNECT = duckdb.connect


def _targets_repo_data(database: object) -> bool:
    """Whether a `duckdb.connect` target resolves inside the repo's `data/`."""
    if not isinstance(database, (str, Path)):
        return False
    resolved = Path(database).resolve()
    return resolved == _REPO_DATA_DIR or _REPO_DATA_DIR in resolved.parents


@pytest.fixture(autouse=True)
def _block_repo_data_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test opens a DuckDB file under the repository's `data/`.

    The mtime guard above structurally cannot see this one: `init_schema()`
    against an already initialized database writes nothing, so every watched
    mtime stays put -- yet the connection still takes DuckDB's file lock,
    which is exclusive between a read-write process and everything else and
    can therefore fail the unattended daily run outright. So intercept the
    boundary call itself, the way `_block_real_network` intercepts
    `socket.socket.connect`, rather than its after-effects.
    """

    def blocked_connect(*args: Any, **kwargs: Any) -> Any:
        database = args[0] if args else kwargs.get("database", ":memory:")
        if _targets_repo_data(database):
            msg = (
                f"Test opened a DuckDB file under the repository's real "
                f"{_REPO_DATA_DIR}/ directory ({database!r}). DuckDB's file "
                "lock is exclusive, so this can fail the operator's scheduled "
                "run. Pass an isolated path (tmp_path), or "
                "monkeypatch.chdir(tmp_path) before calling a composition "
                "root that uses the repo-relative defaults."
            )
            raise AssertionError(msg)
        return _REAL_DUCKDB_CONNECT(*args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", blocked_connect)


def plant_non_finite_bars(market_store: MarketStore, df: pd.DataFrame) -> None:
    """Write bars straight into Parquet, past `write_bars`' finite guard.

    `MarketStore.write_bars` rejects NaN/±inf OHLCV outright (Issue #227), so
    the only way a *stored* bar can still be non-finite is a partition written
    before that guard existed — which is exactly the state the reader-side
    defenses (`compute_forward_return`, `tracking.update`, `retro.evaluate`)
    exist for. Tests of those readers therefore have to plant the row the way
    that history did: bypassing validation, but through the real partition
    writer, so the on-disk layout stays identical.
    """
    working = df.copy()
    working["date"] = pd.to_datetime(working["date"]).dt.date
    years = working["date"].map(lambda bar_date: bar_date.year)
    for year in sorted(years.unique()):
        # Deliberately the unvalidated half of `write_bars`: re-implementing
        # partition merge/replace here would drift from the real writer.
        market_store._write_partition(int(year), working[years == year])  # noqa: SLF001


@pytest.fixture
def settings() -> Settings:
    return load_settings("config/settings.yaml")


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    """Initialized isolated state store for storage/paper contract tests."""
    store = StateStore(Database(tmp_path / "copilot.duckdb"))
    store.init_schema()
    return store
