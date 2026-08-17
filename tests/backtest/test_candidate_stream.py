"""Tests for `backtest/candidate_stream.py` (Issue #185).

The contract under test is that screening runs once per *screening input*, not
once per engine run: a stream generated for a baseline is reusable across an
exit-parameter or cost sweep, survives a Parquet round-trip bit-exactly, and
refuses to be reused once anything screening reads has moved.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from swing_copilot.backtest.candidate_stream import (
    CandidateStream,
    CandidateStreamError,
    CandidateStreamMismatchError,
    compute_cache_key,
    generate_candidate_stream,
    load_candidate_stream,
    load_market_frame,
    save_candidate_stream,
)
from swing_copilot.backtest.runner import (
    BacktestCostOverrides,
    BacktestDependencies,
    BacktestRequest,
    run_backtest,
)
from swing_copilot.screening.base import Candidate, ScreeningInput
from swing_copilot.screening.pipeline import ScreeningPipeline
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from swing_copilot.universe import UniverseMember
from tests.backtest.conftest import bar_row, bars_frame, flat_bars

if TYPE_CHECKING:
    from swing_copilot.config import Settings

STRATEGIES_CONFIG = {
    "strategies": {
        "trend": {
            "filters_all": [],
            "signals_all": ["trend_sma"],
            "candidate_limit": 10,
        },
        "narrow": {
            "filters_all": [],
            "signals_all": ["trend_sma"],
            "candidate_limit": 1,
        },
    }
}

#: Enough history for the 200-day SMA `ranking_metrics` needs, plus the window.
_HISTORY_DAYS = 240
#: The last five sessions are the simulated window; everything before is warmup.
_WINDOW_DAYS = 5
_FIRST_DAY = date(2026, 6, 1)
_DAYS = [_FIRST_DAY + timedelta(days=index) for index in range(_HISTORY_DAYS)]
#: `BBB` collapses here, so it stops being a candidate mid-window and the
#: position opened from it exits on a real gap-down stop.
_CRASH_INDEX = _HISTORY_DAYS - 3


def _with_provider_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**row, "provider": "test", "fetched_at": pd.Timestamp("2027-01-20", tz="UTC")}
        for row in rows
    ]


def _trending_rows() -> list[dict[str, object]]:
    """SPY flat, AAA in a steady uptrend, BBB uptrending then crashing."""
    rows = list(flat_bars("SPY", _DAYS, 400.0))
    for index, day in enumerate(_DAYS):
        aaa_close = 100.0 + 0.5 * index
        rows.append(
            bar_row("AAA", day, (aaa_close, aaa_close + 1, aaa_close - 1, aaa_close))
        )
        bbb_close = 200.0 + 0.4 * index if index < _CRASH_INDEX else 120.0
        rows.append(
            bar_row("BBB", day, (bbb_close, bbb_close + 1, bbb_close - 1, bbb_close))
        )
    return rows


def _make_store(tmp_path: Path, rows: list[dict[str, object]]) -> MarketStore:
    store = MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )
    store.write_bars(bars_frame(_with_provider_columns(rows)))
    return store


def _universe(*symbols: str) -> tuple[UniverseMember, ...]:
    return tuple(
        UniverseMember(
            symbol=symbol,
            company_name=f"{symbol} Inc.",
            gics_sector="Information Technology",
            source_symbol=symbol,
        )
        for symbol in symbols
    )


@pytest.fixture
def trending_store(tmp_path: Path) -> MarketStore:
    return _make_store(tmp_path, _trending_rows())


@pytest.fixture
def deps(settings: Settings, trending_store: MarketStore) -> BacktestDependencies:
    return BacktestDependencies(
        market_store=trending_store,
        universe=_universe("AAA", "BBB"),
        settings=settings,
        strategies_config=STRATEGIES_CONFIG,
    )


@pytest.fixture
def request_(deps: BacktestDependencies) -> BacktestRequest:
    return BacktestRequest(
        symbols=["AAA", "BBB"],
        start=_DAYS[-_WINDOW_DAYS],
        end=_DAYS[-1],
        initial_cash=100_000.0,
        strategy_key="trend",
    )


class TestGenerate:
    def test_generate_candidate_stream_covers_every_day_with_candidates(
        self, request_, deps
    ):
        frame = load_market_frame(request_, deps)

        stream = generate_candidate_stream(request_, deps, frame)

        assert set(stream.candidates_by_day) <= set(frame.trading_days)
        assert stream.candidates_by_day
        for day, candidates in stream.candidates_by_day.items():
            assert [candidate.rank for candidate in candidates] == list(
                range(1, len(candidates) + 1)
            )
            assert all(candidate.as_of == day for candidate in candidates)

    def test_generate_candidate_stream_omits_days_without_candidates(
        self, settings, tmp_path
    ):
        # 20 sessions of history: `ranking_metrics` cannot compute an SMA200,
        # so no day produces a candidate and no day gets an entry either.
        days = [date(2027, 1, 1) + timedelta(days=index) for index in range(20)]
        store = _make_store(
            tmp_path, [*flat_bars("SPY", days, 400.0), *flat_bars("AAA", days, 100.0)]
        )
        deps = BacktestDependencies(
            market_store=store,
            universe=_universe("AAA"),
            settings=settings,
            strategies_config=STRATEGIES_CONFIG,
        )
        request = BacktestRequest(["AAA"], days[0], days[-1], 100_000.0, "trend")
        frame = load_market_frame(request, deps)

        stream = generate_candidate_stream(request, deps, frame)

        assert frame.trading_days
        assert stream.candidates_by_day == {}

    def test_generate_candidate_stream_tags_the_stream_with_its_cache_key(
        self, request_, deps
    ):
        frame = load_market_frame(request_, deps)

        stream = generate_candidate_stream(request_, deps, frame)

        assert stream.cache_key == compute_cache_key(request_, deps, frame)


class TestRoundTrip:
    def test_save_load_round_trip_preserves_a_generated_stream_exactly(
        self, request_, deps, tmp_path
    ):
        frame = load_market_frame(request_, deps)
        stream = generate_candidate_stream(request_, deps, frame)
        path = tmp_path / "cache" / "candidates.parquet"
        path.parent.mkdir(parents=True)

        save_candidate_stream(stream, path)
        loaded = load_candidate_stream(path)

        assert loaded == stream

    def test_save_load_round_trip_preserves_none_execution_distance(self, tmp_path):
        stream = CandidateStream(
            cache_key="deadbeef",
            candidates_by_day={
                date(2027, 1, 4): (
                    Candidate(
                        symbol="AAA",
                        as_of=date(2027, 1, 4),
                        signal_names=("trend_sma",),
                        metrics={"atr14": 1.25, "score": 0.375},
                        rank=1,
                        execution_state="UNKNOWN",
                        execution_distance=None,
                    ),
                    Candidate(
                        symbol="BBB",
                        as_of=date(2027, 1, 4),
                        signal_names=(),
                        metrics={},
                        rank=2,
                        execution_state="FAIR",
                        execution_distance=1.5,
                    ),
                )
            },
        )
        path = tmp_path / "candidates.parquet"

        save_candidate_stream(stream, path)

        assert load_candidate_stream(path) == stream

    def test_save_load_round_trip_of_an_empty_stream_succeeds(self, tmp_path):
        stream = CandidateStream(cache_key="empty", candidates_by_day={})
        path = tmp_path / "candidates.parquet"

        save_candidate_stream(stream, path)

        assert load_candidate_stream(path) == stream

    def test_save_rejects_a_non_finite_metric(self, tmp_path):
        stream = CandidateStream(
            cache_key="nan",
            candidates_by_day={
                date(2027, 1, 4): (
                    Candidate(
                        symbol="AAA",
                        as_of=date(2027, 1, 4),
                        signal_names=(),
                        metrics={"score": float("inf")},
                        rank=1,
                    ),
                )
            },
        )

        with pytest.raises(CandidateStreamError, match="JSON 化できない"):
            save_candidate_stream(stream, tmp_path / "candidates.parquet")

    def test_load_of_a_missing_file_raises(self, tmp_path):
        with pytest.raises(CandidateStreamError, match="読み込めません"):
            load_candidate_stream(tmp_path / "absent.parquet")

    def test_load_of_a_non_parquet_file_raises(self, tmp_path):
        path = tmp_path / "candidates.parquet"
        path.write_bytes(b"not a parquet file at all")

        with pytest.raises(CandidateStreamError, match="読み込めません"):
            load_candidate_stream(path)

    def test_load_of_a_parquet_without_cache_key_metadata_raises(self, tmp_path):
        path = tmp_path / "foreign.parquet"
        pd.DataFrame({"symbol": ["AAA"]}).to_parquet(path, index=False)

        with pytest.raises(CandidateStreamError, match="cache_key"):
            load_candidate_stream(path)

    def test_load_of_a_row_with_unparseable_json_raises(self, tmp_path):
        path = tmp_path / "candidates.parquet"
        save_candidate_stream(
            CandidateStream(cache_key="k", candidates_by_day={}), path
        )
        table = pq.read_table(path)
        corrupted = pa.Table.from_pydict(
            {
                "as_of": [date(2027, 1, 4)],
                "symbol": ["AAA"],
                "rank": [1],
                "signal_names_json": ["[]"],
                "metrics_json": ["{not json}"],
                "execution_state": ["FAIR"],
                "execution_distance": [1.0],
            },
            schema=table.schema,
        )
        pq.write_table(corrupted, path)

        with pytest.raises(CandidateStreamError, match="形式が不正"):
            load_candidate_stream(path)


class TestBitExactReuse:
    def test_injected_round_tripped_stream_reproduces_the_plain_run_exactly(
        self, request_, deps, tmp_path
    ):
        frame = load_market_frame(request_, deps)
        path = tmp_path / "candidates.parquet"
        save_candidate_stream(generate_candidate_stream(request_, deps, frame), path)
        reloaded = load_candidate_stream(path)

        baseline = run_backtest(request_, deps)
        reused = run_backtest(
            request_, deps, candidate_stream=reloaded, market_frame=frame
        )

        assert baseline.trade_count > 0
        assert reused == baseline

    def test_the_same_stream_serves_every_exit_parameter_cell(
        self, request_, deps, tmp_path
    ):
        frame = load_market_frame(request_, deps)
        path = tmp_path / "candidates.parquet"
        save_candidate_stream(generate_candidate_stream(request_, deps, frame), path)
        reloaded = load_candidate_stream(path)
        overrides = BacktestCostOverrides(exit_atr_multiple=1.0, max_hold_days=2)

        baseline = run_backtest(request_, deps, overrides)
        reused = run_backtest(
            request_, deps, overrides, candidate_stream=reloaded, market_frame=frame
        )

        assert reused == baseline


class TestCacheKeyContract:
    """`settings.backtest` and `initial_cash` are engine inputs, never screening ones."""

    @pytest.fixture
    def baseline(self, request_, deps):
        frame = load_market_frame(request_, deps)
        return compute_cache_key(request_, deps, frame), frame

    def _key(self, request_, deps):
        return compute_cache_key(request_, deps, load_market_frame(request_, deps))

    @pytest.mark.parametrize(
        "update",
        [
            pytest.param({"exit_atr_multiple": 1.0}, id="exit_atr_multiple"),
            pytest.param({"max_hold_days": 3}, id="max_hold_days"),
            pytest.param({"commission_pct": 0.05}, id="commission_pct"),
            pytest.param({"slippage_pct": 0.02}, id="slippage_pct"),
            pytest.param({"slippage_multiplier": 3.0}, id="slippage_multiplier"),
        ],
    )
    def test_engine_only_backtest_settings_leave_the_key_unchanged(
        self, request_, deps, baseline, update
    ):
        expected_key, _frame = baseline
        varied = deps.settings.model_copy(
            update={"backtest": deps.settings.backtest.model_copy(update=update)}
        )

        varied_deps = BacktestDependencies(
            deps.market_store, deps.universe, varied, deps.strategies_config
        )

        assert self._key(request_, varied_deps) == expected_key

    def test_initial_cash_leaves_the_key_unchanged(self, request_, deps, baseline):
        expected_key, _frame = baseline
        varied = BacktestRequest(
            symbols=request_.symbols,
            start=request_.start,
            end=request_.end,
            initial_cash=request_.initial_cash * 7,
            strategy_key=request_.strategy_key,
        )

        assert self._key(varied, deps) == expected_key

    def test_a_technical_signal_setting_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline
        trend = deps.settings.technical_signals.trend.model_copy(
            update={"sma_short": 20}
        )
        varied = deps.settings.model_copy(
            update={
                "technical_signals": deps.settings.technical_signals.model_copy(
                    update={"trend": trend}
                )
            }
        )

        varied_deps = BacktestDependencies(
            deps.market_store, deps.universe, varied, deps.strategies_config
        )

        assert self._key(request_, varied_deps) != expected_key

    def test_a_fundamental_filter_setting_changes_the_key(
        self, request_, deps, baseline
    ):
        expected_key, _frame = baseline
        varied = deps.settings.model_copy(
            update={
                "fundamental_filters": deps.settings.fundamental_filters.model_copy(
                    update={"min_equity_ratio": 0.5}
                )
            }
        )

        varied_deps = BacktestDependencies(
            deps.market_store, deps.universe, varied, deps.strategies_config
        )

        assert self._key(request_, varied_deps) != expected_key

    def test_a_strategy_spec_change_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline
        varied_config = {
            "strategies": {
                **STRATEGIES_CONFIG["strategies"],
                "trend": {
                    "filters_all": [],
                    "signals_all": ["trend_sma"],
                    "candidate_limit": 2,
                },
            }
        }

        varied_deps = BacktestDependencies(
            deps.market_store, deps.universe, deps.settings, varied_config
        )

        assert self._key(request_, varied_deps) != expected_key

    def test_a_different_strategy_key_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline
        varied = BacktestRequest(
            symbols=request_.symbols,
            start=request_.start,
            end=request_.end,
            initial_cash=request_.initial_cash,
            strategy_key="narrow",
        )

        assert self._key(varied, deps) != expected_key

    def test_a_different_symbol_set_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline
        varied = BacktestRequest(
            symbols=["AAA"],
            start=request_.start,
            end=request_.end,
            initial_cash=request_.initial_cash,
            strategy_key=request_.strategy_key,
        )

        assert self._key(varied, deps) != expected_key

    def test_a_different_window_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline
        earlier_start = BacktestRequest(
            symbols=request_.symbols,
            start=_DAYS[-_WINDOW_DAYS - 1],
            end=request_.end,
            initial_cash=request_.initial_cash,
            strategy_key=request_.strategy_key,
        )
        earlier_end = BacktestRequest(
            symbols=request_.symbols,
            start=request_.start,
            end=_DAYS[-2],
            initial_cash=request_.initial_cash,
            strategy_key=request_.strategy_key,
        )

        assert self._key(earlier_start, deps) != expected_key
        assert self._key(earlier_end, deps) != expected_key

    def test_a_different_universe_changes_the_key(self, request_, deps, baseline):
        expected_key, _frame = baseline

        varied_deps = BacktestDependencies(
            deps.market_store,
            _universe("AAA"),
            deps.settings,
            deps.strategies_config,
        )

        assert self._key(request_, varied_deps) != expected_key

    def test_a_changed_bar_price_changes_the_key(
        self, request_, deps, baseline, monkeypatch
    ):
        expected_key, _frame = baseline
        original_read_bars = deps.market_store.read_bars

        def edited_read_bars(symbols, start, end, as_of):
            bars = original_read_bars(symbols, start, end, as_of=as_of).copy()
            bars.loc[bars.index[0], "close"] = 1.0
            return bars

        monkeypatch.setattr(deps.market_store, "read_bars", edited_read_bars)

        assert self._key(request_, deps) != expected_key

    def test_a_changed_fundamentals_value_changes_the_key(
        self, request_, deps, baseline, monkeypatch
    ):
        expected_key, frame = baseline
        edited = pd.DataFrame(
            {
                "accession_no": ["0000-1"],
                "symbol": ["AAA"],
                "filed_at": [pd.Timestamp("2026-01-05", tz="UTC")],
                "net_income": [1.0],
            }
        )
        monkeypatch.setattr(
            deps.market_store, "read_fundamentals", lambda _as_of: edited
        )

        varied_frame = load_market_frame(request_, deps)

        assert varied_frame.fundamentals_digest != frame.fundamentals_digest
        assert compute_cache_key(request_, deps, varied_frame) != expected_key

    def test_bar_row_order_does_not_change_the_key(
        self, request_, deps, baseline, monkeypatch
    ):
        expected_key, _frame = baseline
        original_read_bars = deps.market_store.read_bars

        def shuffled_read_bars(symbols, start, end, as_of):
            return original_read_bars(symbols, start, end, as_of=as_of).sample(
                frac=1.0, random_state=7
            )

        monkeypatch.setattr(deps.market_store, "read_bars", shuffled_read_bars)

        assert self._key(request_, deps) == expected_key


def test_screening_ignores_backtest_settings(request_, deps):
    """The exclusion in `compute_cache_key` rests on this equivalence."""
    frame = load_market_frame(request_, deps)
    swept = deps.settings.model_copy(
        update={
            "backtest": deps.settings.backtest.model_copy(
                update={
                    "exit_atr_multiple": 1.0,
                    "max_hold_days": 3,
                    "commission_pct": 0.05,
                    "slippage_pct": 0.02,
                    "slippage_multiplier": 3.0,
                }
            )
        }
    )
    data = ScreeningInput(
        as_of=frame.trading_days[-1],
        universe=deps.universe,
        fundamentals=frame.fundamentals,
        bars=frame.bars,
    )

    baseline_candidates = ScreeningPipeline(
        deps.strategies_config, deps.market_store, deps.settings, "trend"
    ).run(data)
    swept_candidates = ScreeningPipeline(
        deps.strategies_config, deps.market_store, swept, "trend"
    ).run(data)

    assert baseline_candidates
    assert swept_candidates == baseline_candidates


class TestMismatchFailFast:
    def test_a_stream_from_different_inputs_is_rejected(self, request_, deps):
        frame = load_market_frame(request_, deps)
        other_deps = BacktestDependencies(
            deps.market_store, _universe("AAA"), deps.settings, deps.strategies_config
        )
        foreign_stream = generate_candidate_stream(request_, other_deps, frame)

        with pytest.raises(CandidateStreamMismatchError, match="cache_key 不一致"):
            run_backtest(
                request_, deps, candidate_stream=foreign_stream, market_frame=frame
            )

    def test_a_frame_built_for_another_benchmark_is_rejected(self, request_, deps):
        frame = load_market_frame(request_, deps, benchmark_symbol="AAA")

        with pytest.raises(CandidateStreamMismatchError, match="ベンチマーク"):
            run_backtest(request_, deps, market_frame=frame)


class TestAtomicSave:
    def test_a_failed_replacement_preserves_the_previous_cache_and_leaves_no_tmp(
        self, request_, deps, tmp_path, monkeypatch
    ):
        path = tmp_path / "candidates.parquet"
        frame = load_market_frame(request_, deps)
        original = generate_candidate_stream(request_, deps, frame)
        save_candidate_stream(original, path)
        before = path.read_bytes()

        def _boom(self, _target):
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(Path, "replace", _boom)
        replacement = CandidateStream(cache_key="other", candidates_by_day={})

        with pytest.raises(OSError, match="disk full"):
            save_candidate_stream(replacement, path)

        assert path.read_bytes() == before
        assert list(tmp_path.glob(".candidates.parquet.*.tmp")) == []
        assert load_candidate_stream(path) == original
