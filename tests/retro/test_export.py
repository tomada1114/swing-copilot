"""P8-31: `copilot-retro export` assembles and writes `retro_input.json`.

The export reads only what `collect`/`evaluate` already put in DuckDB, plus
the freshness fetch through injected fakes -- the suite stays offline.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

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
from swing_copilot.storage.audit_records import SignalOutcomeRecord
from swing_copilot.storage.paper_records import TradeDecisionRecord
from swing_copilot.storage.verdict_records import (
    AnalysisSourceCoverageRecord,
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
            VerdictReasonRecord(text="受注は堅調に見える", source_ids=("finnhub:1",)),
        ),
        no_trade=False,
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
    state_store.record_trade_decision(
        TradeDecisionRecord(
            run_id=RUN_ID,
            symbol="AAPL",
            strategy_key="default",
            position_id=None,
            decision="followed",
            reason_memo=None,
            virtual_fill_price=None,
        )
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

    def test_cross_tabs_the_human_journal_and_the_cited_sources(
        self, populated_store: StateStore, market_store: MarketStore, tmp_path: Path
    ) -> None:
        document = build_retro_input(
            _deps(populated_store, market_store), _request(tmp_path)
        )

        assert [
            (cell.decision, cell.recommendation, cell.count)
            for cell in document.human_alignment
        ] == [("followed", "proceed", 1)]
        assert [
            (row.source_type, row.provider, row.citation_count)
            for row in document.source_contribution
        ] == [("news", "finnhub", 1)]

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
        assert sections["retro"] == {"max_surprises": 5, "approval_mode": "auto"}
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
        assert document.human_alignment == []
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
