"""Acceptance tests for `report/html_report.py` (FR-09, `docs/05_ui_design.md`)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from swing_copilot.llm.schemas import FilingAnalysis, NewsSummary, SourcedFact
from swing_copilot.report.html_report import (
    ReportContext,
    classify_change,
    render_report,
)
from swing_copilot.risk.checks import CorrelationWarning, RiskAssessment
from swing_copilot.screening.base import Candidate
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import FundamentalsRecord, MarketStore
from swing_copilot.storage.state_store import StateStore
from swing_copilot.text.base import TextItem
from swing_copilot.universe import UniverseMember

_RUN_DATE = date(2026, 7, 20)
_MARKET_SYMBOLS = ("SPY", "QQQ", "^VIX", "^TNX")


@pytest.fixture
def market_store(tmp_path):
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


@pytest.fixture
def state_store(market_store):
    store = StateStore(market_store._database)  # noqa: SLF001 - share one DuckDB file
    store.init_schema()
    return store


def _write_bars(
    market_store: MarketStore, symbol: str, days: int = 40, base: float = 100.0
) -> None:
    dates = [_RUN_DATE - timedelta(days=days - 1 - i) for i in range(days)]
    rows = [
        {
            "symbol": symbol,
            "date": day,
            "open": base + i,
            "high": base + i + 1,
            "low": base + i - 1,
            "close": base + i + 0.5,
            "volume": 2_000_000 + i * 1000,
            "provider": "yfinance",
            "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
        for i, day in enumerate(dates)
    ]
    market_store.write_bars(pd.DataFrame(rows))


def _seed_market_and_symbols(market_store: MarketStore, symbols: list[str]) -> None:
    for symbol in _MARKET_SYMBOLS:
        _write_bars(market_store, symbol, base=50.0)
    for i, symbol in enumerate(symbols):
        _write_bars(market_store, symbol, base=100.0 + i * 10)


def _candidate(
    symbol: str, rank: int, signal_names: tuple[str, ...] = ("trend_sma",)
) -> Candidate:
    return Candidate(
        symbol=symbol,
        as_of=_RUN_DATE,
        signal_names=signal_names,
        metrics={
            "close": 150.5,
            "rsi14": 55.0,
            "atr14": 4.2,
            "avg_volume": 3_000_000.0,
        },
        rank=rank,
    )


def _universe(symbols: list[str]) -> tuple[UniverseMember, ...]:
    return tuple(
        UniverseMember(
            symbol=symbol,
            company_name=f"{symbol} Inc.",
            gics_sector="Information Technology",
            source_symbol=symbol,
        )
        for symbol in symbols
    )


def _context(
    symbols: list[str],
    *,
    risk_assessments: list[RiskAssessment] | None = None,
    news_summaries: list[NewsSummary] | None = None,
    filing_analyses: list[FilingAnalysis] | None = None,
) -> ReportContext:
    return ReportContext(
        run_id=uuid4(),
        run_date=_RUN_DATE,
        generated_at=datetime(2026, 7, 20, 5, 16, tzinfo=UTC),
        universe=_universe(symbols),
        candidates=[_candidate(symbol, rank=i + 1) for i, symbol in enumerate(symbols)],
        risk_assessments=risk_assessments or [],
        news_summaries=news_summaries,
        filing_analyses=filing_analyses,
    )


class TestClassifyChange:
    @pytest.mark.parametrize(
        ("pct", "expected"),
        [
            (0.002, "up"),
            (0.001, "up"),
            (-0.002, "down"),
            (-0.001, "down"),
            (0.0005, "neutral"),
            (0.0, "neutral"),
        ],
    )
    def test_thresholds(self, pct, expected):
        assert classify_change(pct) == expected


class TestRenderReportCandidateCounts:
    def test_zero_candidates_renders_empty_summary_table(
        self, market_store, state_store, tmp_path
    ):
        _seed_market_and_symbols(market_store, [])
        context = _context([])

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "<tbody>" in html
        assert 'id="card-' not in html

    def test_ten_candidates_renders_all_detail_cards(
        self, market_store, state_store, tmp_path
    ):
        symbols = [f"SYM{i}" for i in range(10)]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        for symbol in symbols:
            assert f'id="card-{symbol}"' in html
            assert f'href="#card-{symbol}"' in html


class TestRenderReportLLMFailSoft:
    def test_llm_present_shows_conclusion_and_resolves_sources(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        state_store.record_text_items(
            [
                TextItem(
                    source_id="news-1",
                    symbol="AAPL",
                    source_type="news",
                    published_at=datetime(2026, 7, 19, tzinfo=UTC),
                    title="Example",
                    source_url="https://example.com/news-1",
                    content_text="body",
                    fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
                )
            ]
        )
        news = NewsSummary(
            symbol="AAPL",
            period="2026-07-13..2026-07-20",
            facts=[SourcedFact(statement="Revenue grew.", source_ids=["news-1"])],
            interpretation=["Uptrend continues."],
            sentiment=1,
            risk_flags=[],
            sources=["https://example.com/news-1"],
        )
        context = _context(symbols, news_summaries=[news], filing_analyses=None)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "Uptrend continues." in html
        assert "https://example.com/news-1" in html

    def test_both_llm_lists_none_shows_degraded_message(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols, news_summaries=None, filing_analyses=None)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "本日はニュース・開示分析を取得できませんでした" in html
        # Other card blocks still render normally (fail-soft, not hidden).
        assert "テクニカル" in html
        assert "ファンダメンタル" in html
        assert "リスク計算" in html


class TestRenderReportSecurity:
    def test_xss_payload_in_company_name_is_escaped(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["EVIL"]
        _seed_market_and_symbols(market_store, symbols)
        context = ReportContext(
            run_id=uuid4(),
            run_date=_RUN_DATE,
            generated_at=datetime(2026, 7, 20, 5, 16, tzinfo=UTC),
            universe=(
                UniverseMember(
                    symbol="EVIL",
                    company_name="<script>alert(1)</script>",
                    gics_sector="Industrials",
                    source_symbol="EVIL",
                ),
            ),
            candidates=[_candidate("EVIL", rank=1)],
            risk_assessments=[],
            news_summaries=None,
            filing_analyses=None,
        )

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_xss_payload_in_llm_fact_is_escaped(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        news = NewsSummary(
            symbol="AAPL",
            period="p",
            facts=[
                SourcedFact(
                    statement="<img src=x onerror=alert(1)>", source_ids=["news-1"]
                )
            ],
            interpretation=["<b>bold</b> interpretation"],
            sentiment=0,
            risk_flags=[],
            sources=[],
        )
        context = _context(symbols, news_summaries=[news], filing_analyses=None)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "<img src=x onerror=alert(1)>" not in html
        assert "<b>bold</b> interpretation" not in html


class TestRenderReportOfflineAssets:
    def test_no_external_http_script_or_link_references(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        for line in html.splitlines():
            if "<script src=" in line or "<link" in line:
                assert "http://" not in line
                assert "https://" not in line
        assert 'src="assets/lightweight-charts.standalone.production.js"' in html
        assert 'href="assets/style.css"' in html


class TestRenderReportChartJS:
    def test_v5_add_series_calls_present(self, market_store, state_store, tmp_path):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "LightweightCharts.createChart" in html
        assert "LightweightCharts.CandlestickSeries" in html
        assert "LightweightCharts.LineSeries" in html
        assert "LightweightCharts.HistogramSeries" in html
        assert 'id="chart-data-AAPL"' in html


class TestRenderReportFooter:
    def test_attribution_and_disclaimer_present(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "TradingView" in html
        assert 'href="https://www.tradingview.com/"' in html
        assert "本レポートは情報提供のみを目的とし、投資助言ではありません" in html


class TestRenderReportRiskWarnings:
    def test_correlation_warning_renders_risk_notice_section(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AMD"]
        _seed_market_and_symbols(market_store, symbols)
        assessment = RiskAssessment(
            symbol="AMD",
            status="approved",
            max_shares=10,
            entry_price=100.0,
            stop_price=95.0,
            reasons=(),
            warnings=(CorrelationWarning("NVDA", 0.82, "high_correlation"),),
        )
        context = _context(symbols, risk_assessments=[assessment])

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "risk-warning" in html
        assert "NVDA" in html

    def test_no_warnings_omits_risk_notice_section_entirely(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AMD"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert '<section class="risk-warning">' not in html


class TestRenderReportAtomicWrites:
    def test_dated_and_latest_have_identical_content(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)
        output_dir = tmp_path / "reports"

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(output_dir),
        )

        assert path.name == "2026-07-20.html"
        assert (output_dir / "latest.html").read_text() == path.read_text()

    def test_no_temp_files_left_behind(self, market_store, state_store, tmp_path):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        context = _context(symbols)
        output_dir = tmp_path / "reports"

        render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(output_dir),
        )

        assert list(output_dir.glob("*.tmp")) == []

    def test_rerun_replaces_both_files_for_same_run_date(
        self, market_store, state_store, tmp_path
    ):
        output_dir = tmp_path / "reports"
        _seed_market_and_symbols(market_store, ["AAPL"])
        render_report(
            _context(["AAPL"]),
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(output_dir),
        )

        _seed_market_and_symbols(market_store, ["MSFT"])
        render_report(
            _context(["MSFT"]),
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(output_dir),
        )

        latest_html = (output_dir / "latest.html").read_text()
        assert 'id="card-MSFT"' in latest_html
        assert 'id="card-AAPL"' not in latest_html

    def test_replace_failure_preserves_previous_file_and_cleans_tmp(
        self, market_store, state_store, tmp_path, monkeypatch
    ):
        output_dir = tmp_path / "reports"
        _seed_market_and_symbols(market_store, ["AAPL"])
        render_report(
            _context(["AAPL"]),
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(output_dir),
        )
        previous_latest = (output_dir / "latest.html").read_text(encoding="utf-8")

        _seed_market_and_symbols(market_store, ["MSFT"])

        original_replace = Path.replace
        replace_error_message = "simulated disk failure"

        def failing_replace(self: Path, target: object) -> Path:
            if self.name == ".latest.html.tmp":
                raise OSError(replace_error_message)
            return original_replace(self, target)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "replace", failing_replace)

        with pytest.raises(OSError, match=replace_error_message):
            render_report(
                _context(["MSFT"]),
                market_store,
                state_store,
                templates_dir="templates",
                output_dir=str(output_dir),
            )

        assert (output_dir / "latest.html").read_text(
            encoding="utf-8"
        ) == previous_latest
        assert list(output_dir.glob("*.tmp")) == []


class TestRenderReportMarketStripUnavailable:
    def test_missing_index_bars_render_unavailable_label(
        self, market_store, state_store, tmp_path
    ):
        # No SPY/QQQ/^VIX/^TNX bars seeded at all.
        _write_bars(market_store, "AAPL")
        context = _context(["AAPL"])

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "取得不可" in html


class TestRenderReportSparklineAndPctChangeEdgeCases:
    def test_single_bar_of_history_omits_sparkline_and_pct_change(
        self, market_store, state_store, tmp_path
    ):
        for symbol in _MARKET_SYMBOLS:
            _write_bars(market_store, symbol, base=50.0)
        # Only one bar of history for the candidate itself.
        market_store.write_bars(
            pd.DataFrame(
                [
                    {
                        "symbol": "NEWCO",
                        "date": _RUN_DATE,
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.2,
                        "volume": 500_000,
                        "provider": "yfinance",
                        "fetched_at": datetime(2026, 7, 20, tzinfo=UTC),
                    }
                ]
            )
        )
        context = _context(["NEWCO"])

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert 'polyline points=""' in html


class TestRenderReportFundamentalsBlock:
    def test_full_record_computes_per_fcf_equity_ratio_and_eps(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        market_store.upsert_fundamentals(
            [
                FundamentalsRecord(
                    accession_no="acc-1",
                    symbol="AAPL",
                    form="10-Q",
                    fiscal_period_end=date(2026, 6, 30),
                    filed_at=datetime(2026, 7, 10, tzinfo=UTC),
                    revenue=1_000_000.0,
                    net_income=200_000.0,
                    fcf=150_000.0,
                    equity=5_000_000.0,
                    assets=10_000_000.0,
                    shares=1_000_000.0,
                    source_url="https://www.sec.gov/example",
                    fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
                )
            ]
        )
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "$150,000" in html
        assert "50%" in html  # equity ratio: 5M / 10M
        assert "$0.20" in html  # eps: 200,000 / 1,000,000

    def test_missing_net_income_or_shares_shows_na_for_per_and_eps(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        market_store.upsert_fundamentals(
            [
                FundamentalsRecord(
                    accession_no="acc-1",
                    symbol="AAPL",
                    form="10-Q",
                    fiscal_period_end=date(2026, 6, 30),
                    filed_at=datetime(2026, 7, 10, tzinfo=UTC),
                    revenue=1_000_000.0,
                    net_income=None,
                    fcf=None,
                    equity=None,
                    assets=0.0,
                    shares=None,
                    source_url="https://www.sec.gov/example",
                    fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
                )
            ]
        )
        context = _context(symbols)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "<dt>PER</dt><dd>N/A</dd>" in html
        assert "<dt>FCF (直近開示)</dt><dd>N/A</dd>" in html
        assert "<dt>自己資本比率</dt><dd>N/A</dd>" in html
        assert "<dt>直近EPS</dt><dd>N/A</dd>" in html

    def test_missing_close_still_shows_eps_but_per_is_na(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        market_store.upsert_fundamentals(
            [
                FundamentalsRecord(
                    accession_no="acc-1",
                    symbol="AAPL",
                    form="10-Q",
                    fiscal_period_end=date(2026, 6, 30),
                    filed_at=datetime(2026, 7, 10, tzinfo=UTC),
                    revenue=1_000_000.0,
                    net_income=200_000.0,
                    fcf=150_000.0,
                    equity=5_000_000.0,
                    assets=10_000_000.0,
                    shares=1_000_000.0,
                    source_url="https://www.sec.gov/example",
                    fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
                )
            ]
        )
        # No "close" key at all in metrics, matching how a candidate with no
        # available close price is represented (design.md 2.1: EPS must not
        # depend on close; only PER does).
        context = ReportContext(
            run_id=uuid4(),
            run_date=_RUN_DATE,
            generated_at=datetime(2026, 7, 20, 5, 16, tzinfo=UTC),
            universe=_universe(symbols),
            candidates=[
                Candidate(
                    symbol="AAPL",
                    as_of=_RUN_DATE,
                    signal_names=("trend_sma",),
                    metrics={"rsi14": 55.0, "atr14": 4.2, "avg_volume": 3_000_000.0},
                    rank=1,
                )
            ],
            risk_assessments=[],
            news_summaries=None,
            filing_analyses=None,
        )

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "<dt>PER</dt><dd>N/A</dd>" in html
        assert "<dt>直近EPS</dt><dd>$0.20</dd>" in html  # 200,000 / 1,000,000


class TestRenderReportLLMPartialMatch:
    def test_symbol_absent_from_non_none_llm_lists_shows_neutral_message(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        other_symbol_news = NewsSummary(
            symbol="MSFT",
            period="p",
            facts=[SourcedFact(statement="Unrelated.", source_ids=["x"])],
            interpretation=["Unrelated interpretation."],
            sentiment=0,
            risk_flags=[],
            sources=[],
        )
        context = _context(
            symbols, news_summaries=[other_symbol_news], filing_analyses=[]
        )

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "ニュース・開示分析からの追加情報は今回ありません" in html

    def test_filing_only_uses_filing_interpretation_as_conclusion(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        filing = FilingAnalysis(
            symbol="AAPL",
            filing_type="10-Q",
            facts=[SourcedFact(statement="Filed on time.", source_ids=["f-1"])],
            interpretation=["Filing-derived conclusion."],
            red_flags=["Regulatory scrutiny risk."],
            yoy_changes=[],
            guidance_direction="positive",
        )
        context = _context(symbols, news_summaries=[], filing_analyses=[filing])

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert "Filing-derived conclusion." in html
        assert "Regulatory scrutiny risk." in html


class TestRenderReportLLMSingleItemInterpretation:
    def test_single_item_interpretation_is_conclusion_only_not_duplicated(
        self, market_store, state_store, tmp_path
    ):
        symbols = ["AAPL"]
        _seed_market_and_symbols(market_store, symbols)
        news = NewsSummary(
            symbol="AAPL",
            period="p",
            facts=[SourcedFact(statement="Revenue grew.", source_ids=["news-1"])],
            interpretation=["Only one interpretation sentence."],
            sentiment=1,
            risk_flags=[],
            sources=[],
        )
        context = _context(symbols, news_summaries=[news], filing_analyses=None)

        path = render_report(
            context,
            market_store,
            state_store,
            templates_dir="templates",
            output_dir=str(tmp_path / "reports"),
        )

        html = path.read_text(encoding="utf-8")
        assert html.count("Only one interpretation sentence.") == 1
        assert '<ul class="reasons">' not in html
