"""P8-31: `copilot-retro export` assembles and writes `retro_input.json`.

The export reads only what `collect`/`evaluate` already put in DuckDB, plus
the freshness fetch through injected fakes -- the suite stays offline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from swing_copilot.analysis.filing_selection import MIN_FILING_CHARS
from swing_copilot.analysis.news_supply import DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
from swing_copilot.config import Settings
from swing_copilot.retro.export import (
    RETRO_INPUT_FILENAME,
    RetroExportDependencies,
    RetroExportRequest,
    build_retro_input,
    export_retro_input,
    read_proposals_ledger,
    retro_output_dir,
)
from swing_copilot.retro.schemas import RETRO_INPUT_SCHEMA_VERSION, RetroInput
from swing_copilot.retro.surprises import FreshnessSources
from swing_copilot.retro.validate import evidence_id_space
from swing_copilot.storage.audit_records import SignalOutcomeRecord
from swing_copilot.storage.config_records import ConfigVersionRecord
from swing_copilot.storage.retro_records import (
    RetroNarrationRecord,
    RetroSessionRecord,
)
from swing_copilot.storage.tracking_records import VerdictPosition
from swing_copilot.storage.verdict_records import (
    AnalysisSourceCoverageRecord,
    NewsSupplyRecord,
    VerdictOutcomeRecord,
    VerdictReasonRecord,
    VerdictRecord,
    VerdictSourceRecord,
)
from swing_copilot.text.base import TextItem
from tests.retro.conftest import bars

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.storage.market_store import MarketStore
    from swing_copilot.storage.state_store import StateStore

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
#: A second run sharing `RUN_DATE`, the shape `collect` skips (P8-119/#124).
SUPERSEDED_RUN_ID = UUID("99999999-9999-9999-9999-999999999999")
RUN_DATE = date(2027, 3, 1)
MATURITY_5D = date(2027, 3, 8)
MATURITY_20D = date(2027, 3, 29)
AS_OF = date(2027, 4, 1)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2027, 4, 1, 12, tzinfo=UTC)

    def today(self) -> date:
        return AS_OF


class _FakeNewsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []

    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        self.calls.append((symbol, since, as_of))
        return [
            TextItem(
                source_id="finnhub:fresh",
                symbol=symbol,
                source_type="news",
                published_at=datetime(2027, 3, 10, tzinfo=UTC),
                title="後から出た材料",
                source_url="https://example.test/fresh",
                content_text="本文",
                fetched_at=datetime(2027, 4, 1, tzinfo=UTC),
            )
        ]


class _RaisingNewsClient:
    def fetch_company_news(
        self, symbol: str, since: date, *, as_of: date
    ) -> list[TextItem]:
        msg = f"provider down for {symbol} ({since}..{as_of})"
        raise RuntimeError(msg)


def _verdict(
    symbol: str, recommendation: str, *, run_id: UUID = RUN_ID
) -> VerdictRecord:
    return VerdictRecord(
        run_id=run_id,
        symbol=symbol,
        as_of=RUN_DATE,
        strategy_key="default",
        recommendation=recommendation,
        reasons=(
            VerdictReasonRecord(
                text="受注は堅調に見える",
                source_ids=("finnhub:1",),
                basis="news_catalyst",
            ),
        ),
        no_trade=False,
    )


def _measured_verdict(
    symbol: str, recommendation: str, *, mentions: int, level: str
) -> VerdictRecord:
    """A verdict carrying Issue #130's archived news-supply measurement."""
    return replace(
        _verdict(symbol, recommendation),
        news_supply=NewsSupplyRecord(
            collected_items=20,
            exported_items=15,
            symbol_mention_items=mentions,
            level=level,
        ),
    )


def _insert_run(
    state_store: StateStore, run_id: UUID, run_date: date, started_at: datetime
) -> None:
    """Insert a minimal `runs` row so `get_run_started_at` can resolve it."""
    with state_store._database.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (run_id, run_date, mode, config_hash, status, "
            "started_at) VALUES (?, ?, 'live', 'cfg', 'success', ?)",
            [str(run_id), run_date, started_at],
        )


def _outcome(
    symbol: str,
    recommendation: str,
    forward_return_pct: float,
    classification: str,
    *,
    horizon_days: int = 5,
) -> VerdictOutcomeRecord:
    return VerdictOutcomeRecord(
        run_id=RUN_ID,
        symbol=symbol,
        horizon_days=horizon_days,
        as_of=MATURITY_5D if horizon_days == 5 else MATURITY_20D,
        recommendation=recommendation,
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


@pytest.fixture
def populated_store(state_store: StateStore) -> StateStore:
    """One collected run: a severe proceed miss, a hit, and a human decision."""
    state_store.record_text_items(
        [
            TextItem(
                source_id="finnhub:1",
                symbol="AAPL",
                source_type="news",
                published_at=datetime(2027, 2, 28, tzinfo=UTC),
                title="当時の材料",
                source_url="https://example.test/1",
                content_text="本文",
                fetched_at=datetime(2027, 3, 1, tzinfo=UTC),
            )
        ]
    )
    state_store.replace_run_verdicts(
        RUN_ID,
        [_verdict("AAPL", "proceed"), _verdict("MSFT", "skip")],
        [
            VerdictSourceRecord(
                run_id=RUN_ID,
                symbol="AAPL",
                source_id="finnhub:1",
                source_type="news",
            )
        ],
        [
            AnalysisSourceCoverageRecord(
                run_id=RUN_ID,
                symbol="AAPL",
                source_id="edgar:quarterly",
                original_chars=180_000,
                exported_chars=120_000,
                is_truncated=True,
                selection_mode="section_priority_partial",
                sections=(
                    ("part_i_item_2", "full"),
                    ("part_ii_item_1a", "partial"),
                ),
            )
        ],
    )
    state_store.replace_verdict_outcomes(
        RUN_ID,
        5,
        [
            _outcome("AAPL", "proceed", -8.0, "MISS_SEVERE"),
            _outcome("MSFT", "skip", -1.0, "HIT"),
        ],
    )
    return state_store


def _deps(
    store: StateStore,
    market_store: MarketStore,
    *,
    freshness: FreshnessSources | None = None,
    settings: Settings | None = None,
) -> RetroExportDependencies:
    return RetroExportDependencies(
        market_store=market_store,
        state_store=store,
        settings=settings or Settings(),
        clock=_FixedClock(),
        freshness=freshness or FreshnessSources(),
    )


def _request(tmp_path: Path) -> RetroExportRequest:
    return RetroExportRequest(
        as_of=AS_OF,
        reports_root=tmp_path / "reports",
        ledger_path=tmp_path / "docs" / "retro" / "proposals.md",
    )


class TestBuildRetroInput:
    def test_packs_the_window_s_aggregates_and_identity(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.schema_version == RETRO_INPUT_SCHEMA_VERSION
        assert document.as_of == AS_OF
        assert document.window_start == AS_OF - timedelta(days=90)
        assert document.generated_at == datetime(2027, 4, 1, 12, tzinfo=UTC)
        # proceed mean -8.0 minus skip mean -1.0
        separation = {row.metric_id: row for row in document.aggregates.separation}
        assert separation["metric:separation:5d"].value == pytest.approx(-7.0)
        assert separation["metric:separation:5d"].is_preliminary is True

    def test_publishes_the_paired_and_excess_separation_beside_the_pooled_one(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # Issue #190: the pooled metric keeps its ID (proposals in the ledger
        # cite it) and the two new readings are published alongside it, so the
        # skill can see whether all three agree.
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        paired = document.aggregates.separation_paired
        excess = document.aggregates.separation_paired_excess
        assert paired is not None
        assert excess is not None
        assert {row.metric_id for row in paired} >= {"metric:separation_paired:5d"}
        assert {row.metric_id for row in excess} >= {
            "metric:separation_paired_excess:5d"
        }
        # One run day with both sides: the paired gap equals the pooled one.
        by_id = {row.metric_id: row for row in paired}
        assert by_id["metric:separation_paired:5d"].value == pytest.approx(-7.0)
        assert by_id["metric:separation_paired:5d"].excluded_day_count == 0

    def test_publishes_the_tracking_ledgers_record_per_verdict_side(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        populated_store.upsert_verdict_position(
            VerdictPosition(
                run_id=RUN_ID,
                symbol="AAPL",
                strategy_key="default",
                recommendation="proceed",
                no_trade=False,
                entry_date=RUN_DATE,
                entry_price=100.0,
                stop_price=95.0,
                days_held=5,
                status="closed",
                exit_date=MATURITY_5D,
                exit_price=110.0,
                exit_reason="stop",
                realized_return_pct=10.0,
                last_marked_date=MATURITY_5D,
            )
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        tracked = document.aggregates.tracked_performance
        assert tracked is not None
        rows = {row.recommendation: row for row in tracked}
        assert rows["proceed"].closed_count == 1
        assert rows["proceed"].win_rate == 1.0
        assert rows["proceed"].expectancy_pct == pytest.approx(10.0)
        assert rows["skip"].closed_count == 0

    def test_a_position_realized_outside_the_window_is_left_out(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # The window is matched on the exit date, the same "matured in this
        # period" rule `verdict_outcomes` uses, so both aggregate blocks
        # describe the same stretch of time.
        populated_store.upsert_verdict_position(
            VerdictPosition(
                run_id=RUN_ID,
                symbol="OLD",
                strategy_key="default",
                recommendation="proceed",
                no_trade=False,
                entry_date=date(2026, 1, 5),
                entry_price=100.0,
                stop_price=95.0,
                days_held=5,
                status="closed",
                exit_date=date(2026, 1, 12),
                exit_price=110.0,
                exit_reason="stop",
                realized_return_pct=10.0,
                last_marked_date=date(2026, 1, 12),
            )
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        tracked = document.aggregates.tracked_performance
        assert tracked is not None
        assert {row.recommendation: row.closed_count for row in tracked}["all"] == 0

    def test_verdict_mix_reflects_the_windows_verdicts(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # populated_store: one run, AAPL proceed / MSFT skip.
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        mix = document.aggregates.verdict_mix
        assert mix.metric_id == "verdict_mix"
        assert (mix.run_count, mix.verdict_count) == (1, 2)
        assert (mix.proceed_count, mix.skip_count) == (1, 1)
        assert mix.proceed_ratio == pytest.approx(0.5)
        assert mix.is_flagged is False

    def test_verdict_mix_counts_a_same_day_rerun_once(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # P8-124: `collect` skips a same-day loser but leaves its previously
        # written rows in place, so the window read has to drop them too.
        # Without that, this day is counted twice: 2 runs / 4 verdicts.
        _insert_run(
            populated_store, RUN_ID, RUN_DATE, datetime(2027, 3, 1, 18, tzinfo=UTC)
        )
        _insert_run(
            populated_store,
            SUPERSEDED_RUN_ID,
            RUN_DATE,
            datetime(2027, 3, 1, 9, tzinfo=UTC),
        )
        populated_store.replace_run_verdicts(
            SUPERSEDED_RUN_ID,
            [
                _verdict("AAPL", "proceed", run_id=SUPERSEDED_RUN_ID),
                _verdict("MSFT", "proceed", run_id=SUPERSEDED_RUN_ID),
            ],
            [],
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        mix = document.aggregates.verdict_mix
        assert (mix.run_count, mix.verdict_count) == (1, 2)
        assert (mix.proceed_count, mix.skip_count) == (1, 1)

    def test_verdict_mix_is_computed_even_when_nothing_has_matured(
        self, state_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # REQ-009: 25 skip verdicts, zero verdict_outcomes (no horizon has
        # matured yet) -- separation/proceed_severe_miss_rate/skip_hit_rate
        # all go silent (value=None), but verdict_mix still sees the window.
        state_store.replace_run_verdicts(
            RUN_ID,
            [_verdict(f"S{i}", "skip") for i in range(25)],
            [],
        )

        document = build_retro_input(
            _deps(state_store, market_store), _request(tmp_path)
        )

        mix = document.aggregates.verdict_mix
        assert (mix.verdict_count, mix.proceed_count) == (25, 0)
        assert mix.is_flagged is True
        composed_separation = next(
            row for row in document.aggregates.separation if row.horizon_days is None
        )
        assert composed_separation.value is None

    def test_crosses_the_news_supply_level_against_the_verdicts(
        self, state_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # Issue #154: the threshold is only reviewable if the dossier says how
        # often a `sparse` feed still produced `proceed`.
        state_store.replace_run_verdicts(
            RUN_ID,
            [
                _measured_verdict("AAPL", "proceed", mentions=3, level="sparse"),
                _measured_verdict("MSFT", "skip", mentions=9, level="sufficient"),
                _verdict("IBM", "proceed"),
            ],
            [],
        )

        document = build_retro_input(
            _deps(state_store, market_store), _request(tmp_path)
        )

        supply = document.aggregates.news_supply
        assert supply is not None
        assert supply.sufficient_threshold == DEFAULT_SUFFICIENT_SYMBOL_MENTION_ITEMS
        assert (supply.recorded_verdict_count, supply.unrecorded_verdict_count) == (
            2,
            1,
        )
        assert [
            (cell.level, cell.recommendation, cell.verdict_count)
            for cell in supply.cells
        ] == [
            ("sparse", "proceed", 1),
            ("sufficient", "skip", 1),
            ("unrecorded", "proceed", 1),
        ]

    def test_a_surprise_carries_the_news_supply_its_verdict_was_made_under(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # The counts the skill needs to tell a `sufficient` grade that was
        # still too thin from one that held up -- which no aggregate can say.
        populated_store.replace_run_verdicts(
            RUN_ID,
            [
                _measured_verdict("AAPL", "proceed", mentions=6, level="sufficient"),
                _verdict("MSFT", "skip"),
            ],
            [],
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        supply = document.surprises.items[0].news_supply
        assert supply is not None
        assert (supply.symbol_mention_items, supply.level) == (6, "sufficient")

    def test_a_surprise_from_an_unmeasured_archive_states_no_supply(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items[0].news_supply is None

    def test_records_the_evaluation_settings_the_numbers_came_from(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )
        assert document.evaluation.severe_threshold_pct == pytest.approx(2.0)
        assert document.evaluation.preliminary_sample_threshold == 20
        assert document.evaluation.proceed_severe_miss_watch_rate == pytest.approx(0.15)

    def test_counts_export_gaps_without_claiming_they_caused_a_miss(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.input_coverage is not None
        assert document.input_coverage.filing_count == 1
        assert document.input_coverage.truncated_filing_count == 1
        assert document.input_coverage.severe_miss_symbol_count_with_gap == 1
        dossier = document.surprises.items[0]
        assert dossier.input_filing_coverage[0].source_id == "edgar:quarterly"
        assert dossier.input_filing_coverage[0].coverage.sections[1].status == "partial"

    @pytest.mark.parametrize(
        ("exhibit_truncated", "severe_miss_counts"),
        [
            pytest.param(True, (1, 0, 0), id="collection-stage-cut-is-a-gap"),
            pytest.param(False, (0, 1, 0), id="recorded-as-uncut-is-no-gap"),
            pytest.param(None, (0, 0, 1), id="not-recorded-is-unknown-not-complete"),
        ],
    )
    def test_an_exhibit_cut_before_export_is_not_counted_as_a_complete_input(
        self,
        populated_store: StateStore,
        market_store: MarketStore,
        tmp_path: Path,
        exhibit_truncated: bool | None,
        severe_miss_counts: tuple[int, int, int],
    ) -> None:
        # Issue #157: the export kept every character it was given, so
        # `is_truncated` is honestly false. Counting only that column put the
        # symbol in `without_gap`, telling the retrospective the input had
        # been complete when the press release's tail was never collected.
        populated_store.replace_run_verdicts(
            RUN_ID,
            [_verdict("AAPL", "proceed"), _verdict("MSFT", "skip")],
            [],
            [
                AnalysisSourceCoverageRecord(
                    run_id=RUN_ID,
                    symbol="AAPL",
                    source_id="edgar:earnings-8k",
                    original_chars=64_841,
                    exported_chars=64_841,
                    is_truncated=False,
                    selection_mode="full",
                    sections=(),
                    exhibit_truncated=exhibit_truncated,
                )
            ],
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        coverage = document.input_coverage
        assert coverage is not None
        assert coverage.truncated_filing_count == 0
        assert coverage.exhibit_truncated_filing_count == (
            1 if exhibit_truncated else 0
        )
        assert (
            coverage.severe_miss_symbol_count_with_gap,
            coverage.severe_miss_symbol_count_without_gap,
            coverage.severe_miss_symbol_count_unknown,
        ) == severe_miss_counts
        dossier = document.surprises.items[0]
        assert dossier.input_filing_coverage[0].coverage.exhibit_truncated is (
            exhibit_truncated is True
        )

    @pytest.mark.parametrize(
        ("filing", "counts"),
        [
            pytest.param(
                (4_074, 0, "omitted_symbol_budget"), (0, 1, 1), id="nothing-exported"
            ),
            pytest.param(
                (6_670, 10, "head_fallback"),
                (1, 0, 1),
                id="ten-character-head-slice",
            ),
            pytest.param(
                (96_000, MIN_FILING_CHARS, "head_fallback"),
                (1, 0, 1),
                id="head-slice-cut-to-the-reserved-minimum",
            ),
            pytest.param(
                (180_000, MIN_FILING_CHARS, "section_priority_partial"),
                (0, 0, 1),
                id="sections-shaped-into-the-reserved-minimum",
            ),
            pytest.param(
                (180_000, 120_000, "section_priority_partial"),
                (0, 0, 0),
                id="a-normal-sized-excerpt",
            ),
            pytest.param(
                (600_000, MIN_FILING_CHARS + 1, "head_fallback"),
                (1, 0, 0),
                id="one-character-above-the-floor",
            ),
            pytest.param(
                (4_074, 4_074, "full"), (0, 0, 0), id="a-short-filing-exported-whole"
            ),
        ],
    )
    def test_counts_a_filing_too_small_to_analyze_whatever_mode_cut_it(
        self,
        populated_store: StateStore,
        market_store: MarketStore,
        tmp_path: Path,
        filing: tuple[int, int, str],
        counts: tuple[int, int, int],
    ) -> None:
        # Issue #267: `omitted_symbol_budget` stopped being the shape budget
        # starvation takes once Issue #255 reserved every filing a minimum, so
        # the two rows cut down to that floor read as an ordinary
        # `head_fallback` / `section_priority_partial` here and the mode
        # tallies see nothing. The size does. And a filing short enough to fit
        # whole is not starved, however few characters it carries.
        original_chars, exported_chars, selection_mode = filing
        populated_store.replace_run_verdicts(
            RUN_ID,
            [_verdict("AAPL", "proceed"), _verdict("MSFT", "skip")],
            [],
            [
                AnalysisSourceCoverageRecord(
                    run_id=RUN_ID,
                    symbol="AAPL",
                    source_id="edgar:third-8k",
                    original_chars=original_chars,
                    exported_chars=exported_chars,
                    is_truncated=exported_chars < original_chars,
                    selection_mode=selection_mode,
                    sections=(),
                    exhibit_truncated=False,
                )
            ],
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        coverage = document.input_coverage
        assert coverage is not None
        assert (
            coverage.fallback_filing_count,
            coverage.omitted_filing_count,
            coverage.starved_filing_count,
        ) == counts

    def test_tallies_the_cited_sources(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert [
            (row.source_type, row.provider, row.citation_count)
            for row in document.source_contribution
        ] == [("news", "finnhub", 1)]

    def test_tallies_the_hit_rate_of_each_evidence_kind(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        """Issue #191: a `basis` split the provider tally cannot produce."""
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert [
            (row.basis, row.verdict_count) for row in document.basis_contribution
        ] == [("news_catalyst", 2)]
        assert (
            document.basis_contribution[0].basis_id
            == "metric:basis_contribution:news_catalyst"
        )

    def test_the_basis_tally_is_citable_evidence_for_a_proposal(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        """An aggregate a proposal cannot cite is an aggregate nobody can act on."""
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.basis_contribution[0].basis_id in evidence_id_space(document)

    def test_builds_a_dossier_for_the_severe_miss_only(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert [item.symbol for item in document.surprises.items] == ["AAPL"]
        dossier = document.surprises.items[0]
        assert dossier.run_as_of == RUN_DATE
        assert dossier.strategy_key == "default"
        assert dossier.recommendation == "proceed"
        assert [reason.text for reason in dossier.reasons] == ["受注は堅調に見える"]
        assert dossier.cited_source_ids == ["finnhub:1"]
        assert [row.horizon_days for row in dossier.outcomes] == [5]

    def test_reports_the_realized_drawdown_from_the_run_s_close(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        market_store.write_bars(
            bars(
                "AAPL",
                {
                    RUN_DATE: 100.0,
                    date(2027, 3, 4): 88.0,
                    MATURITY_5D: 92.0,
                },
            )
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        # Worst close after the run: 88.0 against the run's 100.0.
        assert document.surprises.items[0].max_adverse_return_pct == pytest.approx(
            -12.0
        )

    def test_leaves_the_drawdown_unknown_when_no_bar_follows_the_run(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        market_store.write_bars(bars("AAPL", {RUN_DATE: 100.0}))

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items[0].max_adverse_return_pct is None

    def test_leaves_the_drawdown_unknown_when_the_run_close_is_zero(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # A zero close is bad data, not a 100% loss: dividing by it would
        # manufacture an infinite drawdown out of a storage defect.
        market_store.write_bars(bars("AAPL", {RUN_DATE: 0.0, date(2027, 3, 4): 88.0}))

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items[0].max_adverse_return_pct is None

    def test_leaves_the_drawdown_unknown_when_bars_are_missing(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items[0].max_adverse_return_pct is None

    def test_fetches_freshness_for_the_window_after_the_reviewed_run(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        news_client = _FakeNewsClient()

        document = build_retro_input(
            _deps(
                populated_store,
                market_store,
                freshness=FreshnessSources(news_client=news_client),
            ),
            _request(tmp_path),
        )

        assert news_client.calls == [("AAPL", RUN_DATE, AS_OF)]
        freshness = document.surprises.items[0].freshness
        assert [item.source_id for item in freshness.news] == ["finnhub:fresh"]
        assert freshness.fetch_failed is False

    def test_degrades_to_an_empty_freshness_block_when_the_fetch_fails(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(
                populated_store,
                market_store,
                freshness=FreshnessSources(news_client=_RaisingNewsClient()),
            ),
            _request(tmp_path),
        )

        freshness = document.surprises.items[0].freshness
        assert (freshness.news, freshness.fetch_failed) == ([], True)
        assert any("AAPL" in note for note in document.notes)

    def test_notes_that_no_text_adapter_was_available(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert any("アダプタ" in note for note in document.notes)

    def test_reports_what_the_surprise_cap_dropped(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        populated_store.replace_verdict_outcomes(
            RUN_ID,
            5,
            [
                _outcome("AAPL", "proceed", -8.0, "MISS_SEVERE"),
                _outcome("MSFT", "skip", 9.0, "MISS_SEVERE"),
            ],
        )
        settings = Settings.model_validate({"retro": {"max_surprises": 1}})

        document = build_retro_input(
            _deps(populated_store, market_store, settings=settings),
            _request(tmp_path),
        )

        assert [item.symbol for item in document.surprises.items] == ["MSFT"]
        assert document.surprises.dropped_count == 1
        assert document.surprises.max_surprises == 1

    def test_snapshots_the_settings_a_proposal_could_target(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        sections = document.config_snapshot.sections
        assert sections["retro"] == {"max_surprises": 5}
        assert sections["trade_plan"] == {
            "entry_limit_atr_multiple": 0.0,
            "exit_atr_multiple": 2.5,
            "exit_atr_period": 14,
            "max_hold_days": 25,
        }
        assert "postmortem" in sections
        # Delivery plumbing is not an analysis parameter, so it stays out.
        assert "notification" not in sections

    def test_changes_the_config_hash_when_a_snapshotted_value_changes(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        baseline = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )
        changed = build_retro_input(
            _deps(
                populated_store,
                market_store,
                settings=Settings.model_validate({"retro": {"max_surprises": 4}}),
            ),
            _request(tmp_path),
        )

        assert (
            baseline.config_snapshot.config_hash != changed.config_snapshot.config_hash
        )

    def test_changes_the_config_hash_when_trade_plan_changes(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        baseline = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )
        changed = build_retro_input(
            _deps(
                populated_store,
                market_store,
                settings=Settings.model_validate({"trade_plan": {"max_hold_days": 26}}),
            ),
            _request(tmp_path),
        )

        assert (
            baseline.config_snapshot.config_hash != changed.config_snapshot.config_hash
        )
        assert changed.config_snapshot.sections["trade_plan"] == {
            "entry_limit_atr_multiple": 0.0,
            "exit_atr_multiple": 2.5,
            "exit_atr_period": 14,
            "max_hold_days": 26,
        }

    def test_includes_the_signal_performance_overview(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        populated_store.record_signal_outcomes(
            [
                _signal_outcome("AAPL", ("rsi_pullback",), 3.0, "TRUE_POSITIVE"),
                _signal_outcome(
                    "MSFT", ("rsi_pullback",), -3.0, "FALSE_POSITIVE_SEVERE"
                ),
            ]
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert [
            (row.signal_name, row.true_positive_count, row.false_positive_count)
            for row in document.signal_performance
        ] == [("rsi_pullback", 1, 1)]

    def test_produces_an_empty_but_valid_document_for_an_empty_database(
        self, state_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(state_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items == []
        assert [row.value for row in document.aggregates.separation] == [
            None,
            None,
            None,
        ]

    def test_omits_runs_whose_horizon_matured_before_the_window(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        request = RetroExportRequest(
            as_of=MATURITY_5D + timedelta(days=91),
            reports_root=tmp_path / "reports",
            ledger_path=tmp_path / "proposals.md",
        )

        document = build_retro_input(_deps(populated_store, market_store), request)

        assert document.surprises.items == []
        assert document.aggregates.separation[0].sample_size == 0


def _signal_outcome(
    symbol: str,
    signal_names: tuple[str, ...],
    forward_return_pct: float,
    classification: str,
) -> SignalOutcomeRecord:
    return SignalOutcomeRecord(
        run_id=RUN_ID,
        symbol=symbol,
        horizon_days=5,
        as_of=MATURITY_5D,
        signal_names=signal_names,
        forward_return_pct=forward_return_pct,
        classification=classification,
    )


def _record_retro_session(
    store: StateStore, retro_as_of: date, classes: tuple[str, ...]
) -> None:
    """Ingest one past retrospective's narrations, as `copilot-retro ingest` does."""
    store.replace_retro_session(
        RetroSessionRecord(
            retro_as_of=retro_as_of,
            window_start=retro_as_of - timedelta(days=90),
            input_digest="a" * 64,
            generated_at=datetime(2027, 3, 1, tzinfo=UTC),
            outcome_count=4,
            proposal_count=0,
        ),
        [
            RetroNarrationRecord(
                retro_as_of=retro_as_of,
                surprise_id=f"{retro_as_of.isoformat()}-{index}",
                run_id=RUN_ID,
                symbol="AAPL",
                failure_class=failure_class,
                narrative="当時の入力に材料が無かった",
                evidence_refs=("finnhub:1",),
            )
            for index, failure_class in enumerate(classes)
        ],
    )


class TestFailureClassHistory:
    """Issue #189: the L2 qualitative gate is read, not counted by the skill."""

    def test_absent_before_any_retrospective_has_been_ingested(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.failure_class_history is None

    def test_carries_the_trailing_counts_and_the_gate_verdict(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        _record_retro_session(
            populated_store, date(2027, 1, 1), ("information_absent",) * 2
        )
        _record_retro_session(
            populated_store,
            date(2027, 2, 1),
            ("information_absent",) * 3 + ("exogenous",),
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        history = document.failure_class_history
        assert history is not None
        assert (history.gate_window_sessions, history.gate_min_count) == (3, 5)
        assert [
            (row.failure_class, row.count, row.meets_l2_gate) for row in history.counts
        ] == [("information_absent", 5, True), ("exogenous", 1, False)]

    def test_a_session_after_the_cutoff_is_not_counted(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        """Point-in-time: re-exporting an old retrospective reproduces its number."""
        _record_retro_session(
            populated_store, AS_OF + timedelta(days=1), ("exogenous",)
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.failure_class_history is None

    def test_the_gate_rows_are_citable(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        _record_retro_session(populated_store, date(2027, 2, 1), ("exogenous",))

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert "failure_class_exogenous" in evidence_id_space(document)


class TestAggregatesByConfig:
    """Issue #189: separation split by the configuration each run executed under."""

    def test_an_outcome_whose_run_is_unknown_is_dropped(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        """Pooling it would mix populations, which is what the split exists to stop."""
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert document.aggregates_by_config == []

    def test_splits_the_window_by_the_configuration_behind_each_outcome(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        _insert_run(
            populated_store, RUN_ID, RUN_DATE, datetime(2027, 3, 1, 18, tzinfo=UTC)
        )
        populated_store.upsert_config_version(
            ConfigVersionRecord(
                config_hash="cfg",
                first_seen_run_date=RUN_DATE,
                snapshot_hash="b" * 64,
                sections={"retro": {"max_surprises": 5}},
            )
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        (entry,) = document.aggregates_by_config
        assert (entry.config_hash, entry.snapshot_hash) == ("cfg", "b" * 64)
        assert (entry.first_seen_run_date, entry.run_count, entry.outcome_count) == (
            RUN_DATE,
            1,
            2,
        )

    def test_a_configuration_the_ledger_never_saw_reads_as_unrecorded(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        """A run from before the ledger existed: NULL, never a guessed value."""
        _insert_run(
            populated_store, RUN_ID, RUN_DATE, datetime(2027, 3, 1, 18, tzinfo=UTC)
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        (entry,) = document.aggregates_by_config
        assert (entry.snapshot_hash, entry.first_seen_run_date) == (None, None)

    def test_the_per_config_metrics_are_citable_and_distinct(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        _insert_run(
            populated_store, RUN_ID, RUN_DATE, datetime(2027, 3, 1, 18, tzinfo=UTC)
        )

        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        (entry,) = document.aggregates_by_config
        window_wide = {row.metric_id for row in document.aggregates.separation}
        per_config = {row.metric_id for row in entry.separation}
        assert per_config.isdisjoint(window_wide)
        assert per_config <= evidence_id_space(document)


class TestReadProposalsLedger:
    def test_reports_an_absent_ledger_without_failing(self, tmp_path: Path) -> None:
        ledger = read_proposals_ledger(tmp_path / "proposals.md")

        assert (ledger.exists, ledger.rejected_proposal_ids) == (False, [])

    def test_collects_the_ids_a_re_proposal_guard_must_block(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "# 提案台帳\n\n"
            "| RP-ID | 日付 | level | タイトル | status | メモ |\n"
            "|---|---|---|---|---|---|\n"
            "| RP-001 | 2027-03-01 | L1 | RSI 閾値 | applied | #12 |\n"
            "| RP-002 | 2027-03-05 | L2 | ニュース源追加 | rejected | 却下 |\n"
            "| RP-003 | 2027-03-09 | L1 | 重み調整 | verification_failed | 差戻 |\n",
            encoding="utf-8",
        )

        ledger = read_proposals_ledger(path)

        assert ledger.exists is True
        assert ledger.rejected_proposal_ids == ["RP-002", "RP-003"]

    def test_ignores_prose_and_header_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "proposals.md"
        path.write_text(
            "rejected と書かれた本文は行ではない\n"
            "| RP-ID | status |\n|---|---|\n| RP-001 | proposed |\n",
            encoding="utf-8",
        )

        assert read_proposals_ledger(path).rejected_proposal_ids == []


class TestExportRetroInput:
    def test_writes_the_document_where_the_skill_looks_for_it(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        summary = export_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        expected = retro_output_dir(tmp_path / "reports", AS_OF) / RETRO_INPUT_FILENAME
        assert summary.path == expected.resolve()
        assert expected.is_file()

    def test_round_trips_through_the_strict_schema(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        summary = export_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        reloaded = RetroInput.model_validate(
            json.loads(summary.path.read_text(encoding="utf-8"))
        )

        assert reloaded.surprises.items[0].symbol == "AAPL"
        assert reloaded.input_digest == summary.digest

    def test_replaces_a_previous_export_atomically(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        request = _request(tmp_path)
        first = export_retro_input(_deps(populated_store, market_store), request)
        populated_store.replace_verdict_outcomes(
            RUN_ID, 5, [_outcome("MSFT", "skip", 9.0, "MISS_SEVERE")]
        )

        second = export_retro_input(_deps(populated_store, market_store), request)

        assert second.path == first.path
        reloaded = RetroInput.model_validate(
            json.loads(second.path.read_text(encoding="utf-8"))
        )
        assert [item.symbol for item in reloaded.surprises.items] == ["MSFT"]
        assert list(second.path.parent.glob(".*tmp")) == []

    def test_summarizes_what_it_exported(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        summary = export_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert (summary.outcome_count, summary.surprise_count) == (2, 1)
        assert summary.dropped_surprise_count == 0

    def test_preserves_the_previous_export_when_the_write_fails(
        self,
        populated_store: StateStore,
        market_store: MarketStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request(tmp_path)
        first = export_retro_input(_deps(populated_store, market_store), request)
        original = first.path.read_text(encoding="utf-8")

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(
            "swing_copilot.retro.export.write_json_atomically", _explode
        )

        with pytest.raises(OSError, match="disk full"):
            export_retro_input(_deps(populated_store, market_store), request)

        assert first.path.read_text(encoding="utf-8") == original


class TestWriteReadRoundTrip:
    """Issue #292: what the export writes, the export can read back.

    Since #289 verifies the digest against the `exclude_unset` dump, a field
    the hand-built `unsigned` dict forgot is absent from the constructed
    document's `fields_set` too, so the export used to succeed while the file
    it wrote -- where every default is materialized -- could never be parsed
    again. Injecting a next-generation `RetroInput` reproduces exactly that
    and pins the failure to write time instead of read time.
    """

    @staticmethod
    def _schema_with_a_field_the_export_forgot() -> type[RetroInput]:
        """Add a top-level field `build_retro_input`'s `unsigned` never sets.

        Its default is non-droppable on purpose: `_drop_legacy_defaults`
        cannot recognize `0` as "absent", which is the same property Issue
        #276 hit with `exhibit_truncated_filing_count`.
        """

        class _RetroInputWithAForgottenField(RetroInput):
            forgotten_metric_count: int = 0

        return _RetroInputWithAForgottenField

    def test_refuses_to_write_a_document_it_could_not_read_back(
        self,
        populated_store: StateStore,
        market_store: MarketStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "swing_copilot.retro.export.RetroInput",
            self._schema_with_a_field_the_export_forgot(),
        )
        request = _request(tmp_path)

        with pytest.raises(
            ValidationError, match="input_digest does not match canonical retro input"
        ):
            export_retro_input(_deps(populated_store, market_store), request)

        assert not (
            retro_output_dir(request.reports_root, AS_OF) / RETRO_INPUT_FILENAME
        ).exists()


class TestUnknownRun:
    def test_skips_a_surprise_whose_verdict_row_is_gone(
        self, state_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        # `verdict_outcomes` outlives a re-collect that dropped the symbol:
        # fail-soft with a note rather than an export-wide crash.
        orphan_run = uuid4()
        state_store.replace_verdict_outcomes(
            orphan_run,
            5,
            [
                VerdictOutcomeRecord(
                    run_id=orphan_run,
                    symbol="AAPL",
                    horizon_days=5,
                    as_of=MATURITY_5D,
                    recommendation="proceed",
                    forward_return_pct=-8.0,
                    classification="MISS_SEVERE",
                )
            ],
        )

        document = build_retro_input(
            _deps(state_store, market_store), _request(tmp_path)
        )

        assert document.surprises.items == []
        assert any("AAPL" in note for note in document.notes)
