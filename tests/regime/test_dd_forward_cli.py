"""Tests for `copilot-dd-forward` (`regime/dd_forward_cli.py`).

`test_thresholds_match_the_daily_run` is the one that keeps the diagnostic
honest: it pins the CLI's `RegimeConfig` -> `RegimeThresholds` mapping against
`pipeline/daily.py`'s, so a new `regime.*` setting cannot reach the daily run
while this tool keeps measuring the old one.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import date
from typing import TYPE_CHECKING, cast

import duckdb
import pandas as pd
import pytest

from swing_copilot.config import load_settings
from swing_copilot.pipeline import daily as daily_module
from swing_copilot.regime.dd_forward import ForwardScanRequest, scan_forward
from swing_copilot.regime.dd_forward_cli import (
    REGIME_SYMBOLS,
    DdForwardCliError,
    _horizons,
    build_payload,
    main,
    render_terminal,
    thresholds_from,
)
from swing_copilot.regime.dd_forward_sweep import GRID_RANGES
from swing_copilot.regime.distribution import DistributionLevel
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.universe import UniverseMember
from tests.regime.conftest import bars_for, sawtooth

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.config import Settings
    from swing_copilot.pipeline.daily import DailyDependencies
    from swing_copilot.regime.gate import RegimeSnapshot, RegimeThresholds

_AS_OF = date(2027, 1, 1)
_LENGTH = 200


def _stored_db(tmp_path: Path, *, members: tuple[str, ...] = ()) -> tuple[Path, date]:
    """Write index bars (and optional universe members) into an isolated store."""
    db_path = tmp_path / "copilot.duckdb"
    database = Database(db_path)
    # `write_bars` only touches Parquet, so the DuckDB file itself has to be
    # created here -- the CLI treats an absent `--db` as an error, by design.
    state_store = StateStore(database)
    state_store.init_schema()
    store = MarketStore(database, parquet_root=db_path.parent / "bars")
    frames = [
        bars_for("SPY", sawtooth(_LENGTH)),
        bars_for("QQQ", sawtooth(_LENGTH, base=300.0)),
        bars_for("^VIX", [15.0 + (index % 7) for index in range(_LENGTH)]),
        *(bars_for(symbol, sawtooth(_LENGTH, base=50.0)) for symbol in members),
    ]
    bars = pd.concat(frames, ignore_index=True)
    store.write_bars(bars)
    if members:
        state_store.record_universe_membership(
            _AS_OF,
            [
                UniverseMember(
                    symbol=symbol,
                    company_name=symbol,
                    gics_sector="Information Technology",
                    source_symbol=symbol,
                )
                for symbol in members
            ],
        )
    return db_path, max(bars["date"])


def test_thresholds_match_the_daily_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI and `_calculate_regime_snapshot` build identical thresholds.

    Both restate the same `RegimeConfig` mapping. Intercepting the daily run's
    own call and comparing the object it was about to pass catches a `regime.*`
    field added to one and forgotten in the other, which no amount of prose in a
    docstring can.
    """
    settings = load_settings("config/settings.yaml")
    captured: list[RegimeThresholds] = []

    def capture(*_args: object, **kwargs: object) -> RegimeSnapshot:
        captured.append(cast("RegimeThresholds", kwargs["thresholds"]))
        msg = "stop after capture"
        raise RuntimeError(msg)

    monkeypatch.setattr(daily_module, "calculate_regime_snapshot", capture)
    db_path, as_of = _stored_db(tmp_path)
    deps = cast("DailyDependencies", _daily_deps(db_path, settings))
    with pytest.raises(RuntimeError, match="stop after capture"):
        daily_module._calculate_regime_snapshot(deps, as_of)  # noqa: SLF001

    assert captured == [thresholds_from(settings.regime)]


def _daily_deps(db_path: Path, settings: Settings) -> object:
    """The two `DailyDependencies` attributes `_calculate_regime_snapshot` reads.

    A real `DailyDependencies` needs the whole composition root; this stands in
    for the two fields the function under test actually touches.
    """

    class _Deps:
        def __init__(self) -> None:
            database = Database(db_path)
            self.market_store = MarketStore(
                database, parquet_root=db_path.parent / "bars"
            )
            self.settings = settings

    return _Deps()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5", (5,)), ("5,10,25", (5, 10, 25)), (" 3 , 7 ", (3, 7))],
)
def test_horizons_parses_valid_input(raw: str, expected: tuple[int, ...]) -> None:
    assert _horizons(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "-1,5", "abc", ","])
def test_horizons_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(DdForwardCliError, match="--horizons"):
        _horizons(raw)


def test_missing_database_is_an_error_not_a_fresh_one(tmp_path: Path) -> None:
    """A read-only diagnostic never creates the store it was asked to read."""
    missing = tmp_path / "absent.duckdb"
    with pytest.raises(SystemExit, match="ありません"):
        main(["--as-of", "2027-06-01", "--db", str(missing)])
    assert not missing.exists()


def test_runs_offline_and_reports_every_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default run prints the distribution and the per-level forward tables.

    The autouse socket blocker in `tests/conftest.py` makes this an offline
    assertion too: any network call would fail the test rather than pass it.
    """
    db_path, as_of = _stored_db(tmp_path)
    main(["--as-of", as_of.isoformat(), "--db", str(db_path), "--horizons", "5,10"])
    output = capsys.readouterr().out
    assert "copilot-dd-forward" in output
    assert "SEVERE" in output
    assert "CASH_PRIORITY" in output
    assert "SPY の先行き" in output
    assert "QQQ の先行き" in output


def test_universe_members_add_the_equal_weight_basket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A persisted snapshot at `--as-of` brings the basket target into the report."""
    db_path, as_of = _stored_db(tmp_path, members=("AAA", "BBB"))
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
        ]
    )
    output = capsys.readouterr().out
    assert "UNIVERSE_EW の先行き" in output
    assert "等加重バスケット構成銘柄 = 2" in output


def test_without_a_snapshot_the_basket_is_absent_not_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No universe snapshot means index-only measurement, stated in the header."""
    db_path, as_of = _stored_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
        ]
    )
    output = capsys.readouterr().out
    assert "等加重バスケット = なし" in output
    assert "UNIVERSE_EW" not in output


def test_json_is_written_atomically_into_tmp_path(tmp_path: Path) -> None:
    """`--json` is the only write, and it lands through the atomic helper."""
    db_path, as_of = _stored_db(tmp_path)
    json_path = tmp_path / "out" / "dd_forward.json"
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
            "--json",
            str(json_path),
        ]
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["as_of"] == as_of.isoformat()
    assert payload["horizons"] == [5]
    assert payload["thresholds"]["severe_d25"] == 7
    assert payload["observation_count"] == len(payload["observations"])
    assert not list(tmp_path.glob("**/*.tmp"))


def test_sweep_and_grid_sections_are_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither the sensitivity nor the grid renders unless asked for."""
    db_path, as_of = _stored_db(tmp_path)
    args = [
        "--as-of",
        as_of.isoformat(),
        "--db",
        str(db_path),
        "--horizons",
        "5",
        "--score-horizon",
        "5",
    ]
    main(args)
    assert "一変数感度" not in capsys.readouterr().out

    main([*args, "--sweep"])
    output = capsys.readouterr().out
    assert "一変数感度" in output
    assert "dd_caution_d25 は掃引しない" in output


def test_grid_reports_the_current_configuration_alongside_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped 7/6/5/3/2 is always shown, so candidates have a reference row.

    The production ranges score ~17k candidates twice and take about a minute;
    this asserts the rendering contract, which does not depend on their size.
    """
    for name, values in GRID_RANGES.items():
        monkeypatch.setitem(GRID_RANGES, name, values[:2])
    db_path, as_of = _stored_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--grid",
            "--score-horizon",
            "5",
            "--grid-top",
            "3",
            "--grid-min-episodes",
            "1",
            "--grid-max-cash-share",
            "1.0",
        ]
    )
    output = capsys.readouterr().out
    assert "7/6/5/3/2 (現行)" in output
    assert "CASH_PRIORITY 軸" in output
    assert "REDUCE_ONLY 軸" in output
    assert "out-of-sample の検証ではない" in output


def test_start_before_the_history_still_warms_up(tmp_path: Path) -> None:
    """`--start` earlier than the stored history is clamped, not an error."""
    db_path, as_of = _stored_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--start",
            "2000-01-01",
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
        ]
    )


def test_an_as_of_with_no_classifiable_dates_exits(tmp_path: Path) -> None:
    """Too little visible history is a message, not an empty table."""
    db_path, _ = _stored_db(tmp_path)
    with pytest.raises(SystemExit, match="観測日"):
        main(["--as-of", "2027-01-20", "--db", str(db_path), "--horizons", "5"])


def test_payload_levels_sum_to_the_observation_count(tmp_path: Path) -> None:
    """Every classified day lands in exactly one level bucket."""
    db_path, as_of = _stored_db(tmp_path)
    settings = load_settings("config/settings.yaml")
    thresholds = thresholds_from(settings.regime)
    database = Database(db_path)
    store = MarketStore(database, parquet_root=db_path.parent / "bars")
    bars = store.read_bars(list(REGIME_SYMBOLS), date.min, as_of, as_of)

    scan = scan_forward(
        ForwardScanRequest(
            bars=bars,
            start=date.min,
            as_of=as_of,
            thresholds=thresholds,
            horizons=(5,),
        )
    )
    payload = build_payload(scan, thresholds)
    assert sum(payload["level_days"].values()) == payload["observation_count"]


def test_a_score_horizon_outside_horizons_is_rejected(tmp_path: Path) -> None:
    """Scoring on an unmeasured horizon renders empty cells, so it fails first."""
    db_path, as_of = _stored_db(tmp_path)
    with pytest.raises(SystemExit, match="--score-horizon 10 は測定していません"):
        main(
            [
                "--as-of",
                as_of.isoformat(),
                "--db",
                str(db_path),
                "--horizons",
                "5",
                "--score-horizon",
                "10",
            ]
        )


def test_a_score_target_that_was_not_measured_is_rejected(tmp_path: Path) -> None:
    """Without a universe snapshot the basket is absent, so scoring on it fails."""
    db_path, as_of = _stored_db(tmp_path)
    with pytest.raises(SystemExit, match="利用可能: SPY, QQQ"):
        main(
            [
                "--as-of",
                as_of.isoformat(),
                "--db",
                str(db_path),
                "--horizons",
                "5",
                "--score-horizon",
                "5",
                "--score-target",
                "UNIVERSE_EW",
            ]
        )


def test_unreadable_database_reports_a_message_not_a_bare_duckdb_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `duckdb.Error` while reading bars becomes a `DdForwardCliError`, not a crash."""
    db_path, as_of = _stored_db(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> pd.DataFrame:
        msg = "boom"
        raise duckdb.Error(msg)

    monkeypatch.setattr(MarketStore, "read_bars", _boom)
    with pytest.raises(SystemExit, match="読めません"):
        main(["--as-of", as_of.isoformat(), "--db", str(db_path), "--horizons", "5"])


def test_as_of_before_any_stored_bar_reports_no_bars(tmp_path: Path) -> None:
    """A cutoff earlier than every stored row is an explicit message, not empty tables."""
    db_path, _ = _stored_db(tmp_path)
    with pytest.raises(SystemExit, match="バーが1本もありません"):
        main(["--as-of", "2020-01-01", "--db", str(db_path), "--horizons", "5"])


def test_a_horizon_longer_than_the_history_skips_its_forward_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A horizon with no complete window anywhere renders the other tables, not this one."""
    db_path, as_of = _stored_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            str(_LENGTH),
            "--score-horizon",
            str(_LENGTH),
        ]
    )
    output = capsys.readouterr().out
    assert f"保有 {_LENGTH} 営業日" not in output
    assert "copilot-dd-forward" in output


def _climb(length: int, base: float) -> list[float]:
    """A steady rise, avoiding both a decline day and a flat-stall day."""
    closes = [base]
    for _ in range(1, length):
        closes.append(closes[-1] * 1.006)
    return closes


def _mixed_level_db(tmp_path: Path) -> tuple[Path, date]:
    """A calm climb into the sawtooth tail, so NORMAL, HIGH, and SEVERE all occur.

    `_stored_db`'s pure sawtooth history is SEVERE from the very first
    observation under the shipped thresholds, so it cannot exercise the
    severe-trigger table's non-SEVERE skip or a `--grid` candidate that
    actually survives filtering.
    """
    climb_len = 150
    tail_len = 60
    db_path = tmp_path / "copilot.duckdb"
    database = Database(db_path)
    state_store = StateStore(database)
    state_store.init_schema()
    store = MarketStore(database, parquet_root=db_path.parent / "bars")
    spy_closes = _climb(climb_len, 100.0) + sawtooth(
        tail_len, base=_climb(climb_len, 100.0)[-1]
    )
    qqq_closes = _climb(climb_len, 300.0) + sawtooth(
        tail_len, base=_climb(climb_len, 300.0)[-1]
    )
    length = climb_len + tail_len
    frames = [
        bars_for("SPY", spy_closes),
        bars_for("QQQ", qqq_closes),
        bars_for("^VIX", [15.0 + (index % 7) for index in range(length)]),
    ]
    bars = pd.concat(frames, ignore_index=True)
    store.write_bars(bars)
    return db_path, max(bars["date"])


def test_severe_trigger_table_skips_non_severe_observations(tmp_path: Path) -> None:
    """A day that never reached SEVERE contributes nothing to the by-boundary table."""
    db_path, as_of = _mixed_level_db(tmp_path)
    settings = load_settings("config/settings.yaml")
    thresholds = thresholds_from(settings.regime)
    database = Database(db_path)
    store = MarketStore(database, parquet_root=db_path.parent / "bars")
    bars = store.read_bars(list(REGIME_SYMBOLS), date.min, as_of, as_of)
    scan = scan_forward(
        ForwardScanRequest(
            bars=bars, start=date.min, as_of=as_of, thresholds=thresholds, horizons=(5,)
        )
    )
    assert any(
        observation.level(thresholds.distribution) is not DistributionLevel.SEVERE
        for observation in scan.observations
    )
    assert any(
        observation.level(thresholds.distribution) is DistributionLevel.SEVERE
        for observation in scan.observations
    )
    args = Namespace(score_horizon=5, score_target="SPY", sweep=False, grid=False)
    output = render_terminal(scan, thresholds, args)
    # The console-width title wraps after "SEVERE "; this half is never split.
    assert "を出した条件の内訳" in output


def test_sweep_skips_a_boundary_whose_whole_range_is_unloadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary with no candidate loadable against the current config is skipped.

    It must be omitted from `--sweep`, not rendered as an empty table.
    """
    # Current dd_severe_d25 is 7 (config/settings.yaml); every candidate here
    # must be >= it so the whole range is unloadable (dd_severe_d25 >
    # dd_high_d25 is required).
    monkeypatch.setitem(GRID_RANGES, "high_d25", (7, 8, 9))
    db_path, as_of = _stored_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
            "--sweep",
        ]
    )
    output = capsys.readouterr().out
    # Only the header threshold line mentions "high_d25"; no table renders for it.
    assert output.count("high_d25") == 1
    assert "severe_d25" in output


def test_grid_renders_a_surviving_candidate_row_when_the_history_is_mixed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that clears the filters gets its own row, not just the shipped config."""
    for name, values in GRID_RANGES.items():
        monkeypatch.setitem(GRID_RANGES, name, values[:2])
    db_path, as_of = _mixed_level_db(tmp_path)
    main(
        [
            "--as-of",
            as_of.isoformat(),
            "--db",
            str(db_path),
            "--horizons",
            "5",
            "--score-horizon",
            "5",
            "--grid",
            "--grid-top",
            "3",
            "--grid-min-episodes",
            "1",
            "--grid-max-cash-share",
            "1.0",
        ]
    )
    output = capsys.readouterr().out
    assert "4/2/2/1/1" in output


def test_missing_settings_file_exits_with_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `ConfigError` while loading `--settings` is reported and exits 1, not a traceback."""
    missing_settings = tmp_path / "absent-settings.yaml"
    unused_db = tmp_path / "unused.duckdb"
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--as-of",
                "2027-01-01",
                "--db",
                str(unused_db),
                "--settings",
                str(missing_settings),
            ]
        )
    assert exc_info.value.code == 1
    assert "Settings file not found" in capsys.readouterr().err
