"""Tests for `copilot-filter-matrix` (`screening/filter_matrix_cli.py`)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from swing_copilot.screening.filter_matrix_cli import main
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from tests.screening.conftest import (
    make_bars,
    pinned_settings_path,
    pinned_strategies_path,
)
from tests.screening.test_filter_matrix import (
    AS_OF,
    FUNDAMENTALS_CHECK,
    PULLBACK_CHECK,
    TREND_CHECK,
    VOLUME_CHECK,
    _pullback_closes,
    _uptrend_closes,
    healthy_fundamentals,
)

if TYPE_CHECKING:
    from pathlib import Path

_START = date(2026, 1, 1)
_FETCHED_AT = datetime(2026, 7, 29, tzinfo=UTC)
_SYMBOLS = ("PASSALL", "LOWVOL", "NOFUND", "UPTREND", "NOBARS")
#: In the `--as-of` snapshot but never fetched, so the local store holds
#: neither bars nor filings for it -- exactly what `--limit`ed daily runs
#: leave behind.
_UNSTORED_SYMBOL = "NEVERFETCHED"


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        company_name=symbol,
        gics_sector="Information Technology",
        source_symbol=symbol,
    )


def _stored_bars() -> pd.DataFrame:
    frames = [
        make_bars("PASSALL", _pullback_closes(), start=_START),
        make_bars("LOWVOL", _pullback_closes(), start=_START, volume=500_000),
        make_bars("NOFUND", _pullback_closes(), start=_START),
        make_bars("UPTREND", _uptrend_closes(), start=_START),
    ]
    bars = pd.concat(frames, ignore_index=True)
    bars["provider"] = "test"
    bars["fetched_at"] = pd.Timestamp(_FETCHED_AT)
    return bars


def _stored_fundamentals() -> list[FundamentalsRecord]:
    return [
        FundamentalsRecord(**row)  # type: ignore[arg-type]  # Any: conftest builds one row as loosely typed cells
        for symbol in ("PASSALL", "LOWVOL", "UPTREND", "NOBARS")
        for row in healthy_fundamentals(symbol)
    ]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A DuckDB/Parquet pair holding the same scenario the core test measures."""
    path = tmp_path / "copilot.duckdb"
    database = Database(path)
    state_store = StateStore(database)
    state_store.init_schema()
    market_store = MarketStore(database, parquet_root=tmp_path / "bars")
    market_store.write_bars(_stored_bars())
    market_store.upsert_fundamentals(_stored_fundamentals())
    # Snapshot dated exactly `AS_OF`, so the inclusive point-in-time boundary
    # is what `TestUniverseSnapshotBoundary` exercises.
    state_store.record_universe_membership(
        AS_OF, [_member(symbol) for symbol in (*_SYMBOLS, _UNSTORED_SYMBOL)]
    )
    return path


@pytest.fixture
def config(tmp_path: Path) -> list[str]:
    """`--settings`/`--strategies` pointing at pinned copies of the real config.

    The hand-calculated expectations below are tied to specific thresholds and
    to `default`'s check list, so they must not read `config/*.yaml` directly:
    tuning a threshold there is exactly what this CLI exists to support.
    """
    return [
        "--settings",
        str(pinned_settings_path(tmp_path)),
        "--strategies",
        str(pinned_strategies_path(tmp_path)),
    ]


def _run(db_path: Path, json_path: Path, config: list[str], *extra: str) -> None:
    main(
        [
            "--as-of",
            AS_OF.isoformat(),
            "--db",
            str(db_path),
            "--json",
            str(json_path),
            *config,
            *extra,
        ]
    )


class TestMain:
    def test_reports_the_hand_calculated_matrix(self, db_path, tmp_path, config):
        json_path = tmp_path / "out" / "filter_matrix.json"

        _run(db_path, json_path, config)

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["as_of"] == AS_OF.isoformat()
        assert payload["strategy"] == "default"
        assert payload["universe_size"] == 5
        assert [
            (
                check["name"],
                check["pass_count"],
                check["fail_count"],
                check["no_data_count"],
            )
            for check in payload["checks"]
        ] == [
            (FUNDAMENTALS_CHECK, 4, 0, 1),
            (VOLUME_CHECK, 3, 1, 1),
            (TREND_CHECK, 4, 0, 1),
            (PULLBACK_CHECK, 3, 1, 1),
        ]
        assert payload["blocked_count_distribution"] == [
            {"blocked_checks": 0, "symbol_count": 1},
            {"blocked_checks": 1, "symbol_count": 3},
            {"blocked_checks": 3, "symbol_count": 1},
        ]
        assert payload["co_blocked_counts"] == [
            {"checks": [VOLUME_CHECK, TREND_CHECK], "symbol_count": 1},
            {"checks": [VOLUME_CHECK, PULLBACK_CHECK], "symbol_count": 1},
            {"checks": [TREND_CHECK, PULLBACK_CHECK], "symbol_count": 1},
        ]
        assert payload["unblocked_symbols"] == ["PASSALL"]
        assert [check["sole_blocker_count"] for check in payload["checks"]] == [
            1,
            1,
            0,
            1,
        ]

    def test_prints_every_section_to_stdout(self, db_path, tmp_path, capsys, config):
        json_path = tmp_path / "filter_matrix.json"

        _run(db_path, json_path, config)

        stdout = capsys.readouterr().out
        assert "チェック別 独立通過率" in stdout
        assert "落選チェック数の分布" in stdout
        assert "同時落選マトリクス" in stdout
        assert "全チェック通過: PASSALL" in stdout
        assert f"JSON written to {json_path}" in stdout

    def test_stdout_only_when_json_is_omitted(self, db_path, capsys, config):
        main(["--as-of", AS_OF.isoformat(), "--db", str(db_path), *config])

        stdout = capsys.readouterr().out
        assert "チェック別 独立通過率" in stdout
        assert "JSON written to" not in stdout

    def test_rerun_replaces_the_json_and_leaves_no_temporary_file(
        self, db_path, tmp_path, config
    ):
        json_path = tmp_path / "filter_matrix.json"

        _run(db_path, json_path, config)
        _run(db_path, json_path, config, "--strategy", "minervini_stage2")

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["strategy"] == "minervini_stage2"
        assert [
            path.name for path in tmp_path.iterdir() if path.name.startswith(".")
        ] == []

    def test_run_writes_no_screening_rows(self, db_path, tmp_path, config):
        _run(db_path, tmp_path / "filter_matrix.json", config)

        with Database(db_path).connect() as conn:
            candidates = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()
            rejections = conn.execute(
                "SELECT COUNT(*) FROM screening_rejections"
            ).fetchone()
        assert (candidates, rejections) == ((0,), (0,))


class TestUniverseSnapshotBoundary:
    def test_snapshot_dated_exactly_as_of_is_visible(self, db_path, capsys, config):
        main(["--as-of", AS_OF.isoformat(), "--db", str(db_path), *config])

        assert "universe=5" in capsys.readouterr().out

    def test_run_before_the_first_snapshot_fails_instead_of_refetching(
        self, db_path, config
    ):
        earlier = (AS_OF - timedelta(days=1)).isoformat()

        with pytest.raises(SystemExit) as exc_info:
            main(["--as-of", earlier, "--db", str(db_path), *config])

        assert "ユニバーススナップショットがありません" in str(exc_info.value)


class TestArgumentErrors:
    def test_unknown_strategy_lists_the_configured_keys(self, db_path, config):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--as-of",
                    AS_OF.isoformat(),
                    "--db",
                    str(db_path),
                    "--strategy",
                    "nope",
                    *config,
                ]
            )

        message = str(exc_info.value)
        assert "戦略 'nope' は見つかりません" in message
        assert "default" in message

    def test_unreadable_settings_exit_with_code_1(self, db_path, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--as-of",
                    AS_OF.isoformat(),
                    "--db",
                    str(db_path),
                    "--settings",
                    str(tmp_path / "missing.yaml"),
                ]
            )

        assert exc_info.value.code == 1
        assert capsys.readouterr().err


class TestUniverseScoping:
    """Never-fetched snapshot members are not threshold rejections."""

    def test_symbols_with_no_stored_data_are_excluded_from_every_count(
        self, db_path, tmp_path, config
    ):
        json_path = tmp_path / "filter_matrix.json"

        _run(db_path, json_path, config)

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert (payload["snapshot_size"], payload["universe_size"]) == (6, 5)
        assert payload["unstored_symbol_count"] == 1
        # Counting it would report the bar-based checks as rejecting a sixth
        # of the universe on local coverage alone.
        assert (
            sum(row["symbol_count"] for row in payload["blocked_count_distribution"])
            == 5
        )

    def test_the_exclusion_is_reported_rather_than_silent(
        self, db_path, tmp_path, capsys, config
    ):
        _run(db_path, tmp_path / "filter_matrix.json", config)

        assert "1 銘柄はバーもファンダも未保存のため除外" in capsys.readouterr().out


class TestCandidateEquivalentOutput:
    def test_candidate_equivalent_symbols_are_reported_separately(
        self, db_path, tmp_path, capsys, config
    ):
        json_path = tmp_path / "filter_matrix.json"

        _run(db_path, json_path, config)

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["candidate_equivalent_symbols"] == ["PASSALL"]
        assert "候補相当（candidate_limit 適用前）: PASSALL" in capsys.readouterr().out


class TestReadOnlyGuarantee:
    def test_absent_database_is_an_error_and_is_not_created(self, tmp_path, config):
        absent = tmp_path / "absent.duckdb"

        with pytest.raises(SystemExit) as exc_info:
            main(["--as-of", AS_OF.isoformat(), "--db", str(absent), *config])

        assert "がありません" in str(exc_info.value)
        assert not absent.exists()

    def test_a_database_without_the_state_schema_is_not_migrated(
        self, tmp_path, config
    ):
        path = tmp_path / "bare.duckdb"
        with Database(path).connect() as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")

        with pytest.raises(SystemExit) as exc_info:
            main(["--as-of", AS_OF.isoformat(), "--db", str(path), *config])

        assert "を読めません" in str(exc_info.value)
        with Database(path).connect() as conn:
            tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        assert tables == {"unrelated"}


class TestUnmeasurableStrategy:
    def test_an_unregistered_check_key_fails_with_a_message_not_a_traceback(
        self, db_path, tmp_path
    ):
        strategies = pinned_strategies_path(tmp_path, filters_all=["volume_mn"])

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--as-of",
                    AS_OF.isoformat(),
                    "--db",
                    str(db_path),
                    "--settings",
                    str(pinned_settings_path(tmp_path)),
                    "--strategies",
                    str(strategies),
                ]
            )

        assert "チェック構成を測定できません" in str(exc_info.value)
