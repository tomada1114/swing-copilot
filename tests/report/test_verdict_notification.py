"""Tests for `report/verdict_notification.py` (Issue #383, FR-09).

Fully offline: every fixture is written to `tmp_path` as plain JSON via the
same production model classes/writers `copilot-daily` and
`copilot-ingest-analysis` use (`AnalysisInput`, `AnalysisResult`,
`write_report_context`), never a real network call, DuckDB connection, or
write to the repository's own `reports/`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    AnalysisContextBlocks,
    AnalysisInput,
    AnalysisResult,
    CandidateInput,
    ScreeningAssessment,
    SymbolAnalysis,
    Verdict,
    VerdictReason,
    canonical_json_digest,
)
from swing_copilot.analysis.snapshot import ReportContext, write_report_context
from swing_copilot.models import RunStatus
from swing_copilot.report.daily_brief import (
    BriefCandidate,
    BriefExposure,
    BriefFundamentals,
    BriefRegime,
    BriefRisk,
    DailyBrief,
    build_analysis_brief,
)
from swing_copilot.report.verdict_notification import (
    _MAX_BLOCK_BODY_CHARS,
    DISCORD_MESSAGE_CHAR_LIMIT,
    _pack_messages,
    _per_share_risk,
    _proceed_block,
    _safe_block,
    _shrink_block,
    build_daily_notification,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_DATE = date(2026, 8, 28)
STRATEGY_KEY = "default"


def _outcome_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": "success",
        "reason": None,
        "run_id": str(RUN_ID),
        "run_date": RUN_DATE.isoformat(),
        "candidates": 1,
        "started_at": "2026-08-28T21:17:00+00:00",
        "finished_at": "2026-08-28T21:20:00+00:00",
    }
    payload.update(overrides)
    return payload


def _write_outcome_file(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(_outcome_payload(**overrides)), encoding="utf-8")
    return path


def _candidate_input(symbol: str) -> CandidateInput:
    return CandidateInput(
        symbol=symbol,
        score_breakdown="",
        risk_constraints="",
        news=[],
        filings=[],
    )


def _analysis_input(
    reports_dir: Path,
    *,
    symbols: list[str],
    run_id: UUID = RUN_ID,
    as_of: date = RUN_DATE,
    strategy_key: str = STRATEGY_KEY,
) -> AnalysisInput:
    unsigned: dict[str, object] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "run_id": str(run_id),
        "as_of": as_of.isoformat(),
        "strategy_key": strategy_key,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "context": AnalysisContextBlocks(market_regime=None).model_dump(mode="json"),
        "candidates": [
            _candidate_input(symbol).model_dump(mode="json") for symbol in symbols
        ],
    }
    document = AnalysisInput.model_validate(
        {
            **unsigned,
            "input_digest": canonical_json_digest(
                unsigned, excluded_field="input_digest"
            ),
        }
    )
    run_dir = reports_dir / as_of.isoformat() / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ANALYSIS_INPUT_FILENAME).write_text(
        json.dumps(document.model_dump(mode="json")), encoding="utf-8"
    )
    return document


def _write_analysis_result(
    reports_dir: Path,
    analysis_input: AnalysisInput,
    *,
    symbols: list[SymbolAnalysis],
    no_trade_reason: str | None = None,
    run_id: UUID | None = None,
) -> None:
    """Write `analysis_result.json` matching `analysis_input`'s own identity.

    `no_trade_reason` given at all implies `no_trade=True` (the schema
    requires the two travel together). `run_id` may be overridden to
    deliberately break identity (used by the "documents disagree" test) --
    every other identity field always matches, since that is the only
    mismatch this test module needs to exercise.
    """
    result = AnalysisResult(
        schema_version=RESULT_SCHEMA_VERSION,
        run_id=run_id or analysis_input.run_id,
        as_of=analysis_input.as_of,
        strategy_key=analysis_input.strategy_key,
        input_digest=analysis_input.input_digest,
        generated_by="offline test fixture",
        symbols=symbols,
        no_trade=no_trade_reason is not None,
        no_trade_reason=no_trade_reason,
    )
    run_dir = (
        reports_dir / analysis_input.as_of.isoformat() / str(analysis_input.run_id)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ANALYSIS_RESULT_FILENAME).write_text(
        json.dumps(result.model_dump(mode="json")), encoding="utf-8"
    )


def _brief_risk(**overrides: object) -> BriefRisk:
    values: dict[str, object] = {
        "status": "approved",
        "entry_price": 150.0,
        "limit_price": 151.2,
        "stop_price": 145.3,
        "atr14": 3.1,
        "stop_distance_pct": 0.039,
        "reasons": (),
        "warnings": ("WIDE_STOP",),
        "binding_constraint": None,
    }
    values.update(overrides)
    return BriefRisk(**values)  # type: ignore[arg-type]


def _brief_candidate(
    symbol: str, *, rank: int = 1, risk: BriefRisk | None = None
) -> BriefCandidate:
    return BriefCandidate(
        rank=rank,
        symbol=symbol,
        company_name=f"{symbol} Inc.",
        close=150.0,
        pct_change=0.01,
        rsi14=55.0,
        atr14=3.1,
        score=0.842,
        score_rsi_pullback=0.1,
        score_trend_quality=0.2,
        score_liquidity=0.1,
        score_atr_pct=0.1,
        score_pivot_proximity=0.1,
        score_rs_percentile=0.1,
        score_criteria_met=0.1,
        signals=("trend_sma",),
        fundamentals=BriefFundamentals(
            per="N/A", fcf="N/A", equity_ratio="N/A", eps="N/A"
        ),
        risk=risk or _brief_risk(),
        analysis=build_analysis_brief(symbol, None),
    )


def _write_report_context(
    reports_dir: Path,
    analysis_input: AnalysisInput,
    *,
    candidates: list[BriefCandidate],
    include_exposure: bool = True,
) -> None:
    brief = DailyBrief(
        run_id=analysis_input.run_id,
        run_date=analysis_input.as_of,
        generated_at=datetime.now(tz=UTC),
        market=(),
        candidates=tuple(candidates),
        regime=BriefRegime(
            gate="BULL", dd_level="HIGH", spy_d25=1.0, qqq_d25=1.0, data_quality="OK"
        ),
        exposure=(
            BriefExposure(
                verdict="NEW_ENTRY_ALLOWED",
                gate="BULL",
                dd_level="HIGH",
                data_quality="OK",
                is_conservatively_downgraded=False,
            )
            if include_exposure
            else None
        ),
    )
    run_dir = (
        reports_dir / analysis_input.as_of.isoformat() / str(analysis_input.run_id)
    )
    write_report_context(
        ReportContext(
            brief=brief,
            status=RunStatus.SUCCESS,
            output_dir=reports_dir,
            strategy_key=analysis_input.strategy_key,
            input_digest=analysis_input.input_digest,
        ),
        run_dir,
    )


def _build_one_proceed_symbol_run(reports_dir: Path, tmp_path: Path) -> Path:
    """The common "one proceed symbol, fully wired" fixture most tests share."""
    analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
    _write_analysis_result(
        reports_dir,
        analysis_input,
        symbols=[
            SymbolAnalysis(
                symbol="AAPL",
                screening_assessment=ScreeningAssessment(summary="Strong trend."),
                verdict=Verdict(
                    recommendation="proceed",
                    reasons=[
                        VerdictReason(
                            text="Technical score is strong and RSI shows a pullback.",
                            basis="technical_score",
                        )
                    ],
                ),
            )
        ],
    )
    _write_report_context(
        reports_dir, analysis_input, candidates=[_brief_candidate("AAPL")]
    )
    return _write_outcome_file(tmp_path, candidates=1)


class TestOutcomeFileEdgeCases:
    def test_missing_outcome_file_is_abnormal(self, tmp_path: Path) -> None:
        messages = build_daily_notification(
            outcome_file=None, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "outcome ファイル欠落" in messages[0]

    def test_nonexistent_outcome_file_is_abnormal(self, tmp_path: Path) -> None:
        messages = build_daily_notification(
            outcome_file=tmp_path / "does-not-exist.json",
            reports_dir=tmp_path / "reports",
        )

        assert len(messages) == 1
        assert "outcome ファイル欠落" in messages[0]

    def test_outcome_file_not_json_object_is_abnormal(self, tmp_path: Path) -> None:
        path = tmp_path / "outcome.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "outcome ファイル欠落" in messages[0]

    def test_outcome_file_invalid_json_is_abnormal(self, tmp_path: Path) -> None:
        path = tmp_path / "outcome.json"
        path.write_text("not json", encoding="utf-8")

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "outcome ファイル欠落" in messages[0]

    def test_outcome_file_missing_outcome_field_is_abnormal(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "outcome.json"
        path.write_text(json.dumps({"reason": None}), encoding="utf-8")

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "outcome ファイル欠落" in messages[0]


class TestPreflightAbort:
    @pytest.mark.parametrize("reason", ["same_day_rerun", "no_trading_day"])
    def test_legitimate_reasons_are_a_normal_stop(
        self, tmp_path: Path, reason: str
    ) -> None:
        path = _write_outcome_file(
            tmp_path,
            outcome="preflight_abort",
            reason=reason,
            run_id=None,
            run_date=None,
            candidates=None,
        )

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "正常停止" in messages[0]
        assert reason in messages[0]

    def test_price_fetch_failed_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(
            tmp_path,
            outcome="preflight_abort",
            reason="price_fetch_failed",
            run_id=None,
            run_date=None,
            candidates=None,
        )

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "異常終了" in messages[0]
        assert "price_fetch_failed" in messages[0]

    def test_unrecognized_reason_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(
            tmp_path,
            outcome="preflight_abort",
            reason="some_future_reason",
            run_id=None,
            run_date=None,
            candidates=None,
        )

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert "異常終了" in messages[0]

    def test_missing_reason_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(
            tmp_path,
            outcome="preflight_abort",
            reason=None,
            run_id=None,
            run_date=None,
            candidates=None,
        )

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert "異常終了" in messages[0]


class TestJobFailure:
    def test_failed_outcome_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(tmp_path, outcome="failed")

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert len(messages) == 1
        assert "異常終了" in messages[0]
        assert "failed" in messages[0]

    def test_unknown_outcome_string_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(tmp_path, outcome="something_else")

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert "異常終了" in messages[0]

    def test_success_without_run_id_is_abnormal(self, tmp_path: Path) -> None:
        path = _write_outcome_file(tmp_path, run_id=None)

        messages = build_daily_notification(
            outcome_file=path, reports_dir=tmp_path / "reports"
        )

        assert "異常終了" in messages[0]


class TestNoCandidatesOrMissingAnalysis:
    def test_zero_candidates_is_a_benign_notice(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "reports"
        path = _write_outcome_file(tmp_path, candidates=0)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert len(messages) == 1
        assert "候補が無かった" in messages[0]

    def test_missing_analysis_result_with_candidates_is_flagged(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        path = _write_outcome_file(tmp_path, candidates=3)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert len(messages) == 1
        assert "見つかりません" in messages[0]
        assert "候補 3 件" in messages[0]

    def test_broken_analysis_documents_are_reported_as_unverifiable(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        # An analysis_result whose run_id does not match analysis_input's own:
        # validate_artifact_identity must hard-fail this, not silently ingest it.
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="proceed", reasons=[]),
                )
            ],
            run_id=uuid4(),
        )
        _write_report_context(
            reports_dir, analysis_input, candidates=[_brief_candidate("AAPL")]
        )
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert len(messages) == 1
        assert "検証できませんでした" in messages[0]

    def test_missing_report_context_is_reported_as_unverifiable(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="proceed", reasons=[]),
                )
            ],
        )
        # report_context.json deliberately never written.
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "検証できませんでした" in messages[0]


class TestProceedDay:
    def test_proceed_block_carries_the_full_trade_plan(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "reports"
        path = _build_one_proceed_symbol_run(reports_dir, tmp_path)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert len(messages) == 1
        body = messages[0]
        assert "AAPL" in body
        assert "AAPL Inc." in body
        assert "Exposure Ceiling: NEW_ENTRY_ALLOWED" in body
        assert "proceed 1 / skip 0 / withheld 0" in body
        assert "$151.20" in body  # limit_price
        assert "$145.30" in body  # stop_price
        assert "3.90%" in body  # stop_distance_pct (1R)
        assert "$5.90" in body  # per-share risk: 151.2 - 145.3
        assert "3.10" in body  # atr14
        assert "WIDE_STOP" in body
        assert "Technical score is strong" in body
        assert "[technical_score]" in body
        # Share count is never shown -- the account is unknown to this product.
        assert "株" not in body or "1株あたりリスク" in body

    def test_blocked_risk_shows_blocking_reasons_without_warnings(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="proceed", reasons=[]),
                )
            ],
        )
        blocked_risk = _brief_risk(
            status="rejected",
            reasons=("REGIME_CASH_PRIORITY_REASON",),
            warnings=(),
        )
        _write_report_context(
            reports_dir,
            analysis_input,
            candidates=[_brief_candidate("AAPL", risk=blocked_risk)],
        )
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "blocking_reasons: REGIME_CASH_PRIORITY_REASON" in messages[0]
        assert "warnings:" not in messages[0]

    def test_missing_exposure_omits_the_exposure_ceiling_line(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="proceed", reasons=[]),
                )
            ],
        )
        _write_report_context(
            reports_dir,
            analysis_input,
            candidates=[_brief_candidate("AAPL")],
            include_exposure=False,
        )
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "Exposure Ceiling" not in messages[0]

    def test_degraded_status_label_differs_from_success(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="skip", reasons=[]),
                )
            ],
            no_trade_reason="Market regime is bearish.",
        )
        _write_report_context(
            reports_dir, analysis_input, candidates=[_brief_candidate("AAPL")]
        )
        path = _write_outcome_file(tmp_path, outcome="degraded", candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "一部縮退" in messages[0]

    def test_candidate_missing_from_report_context_still_renders_a_block(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="proceed", reasons=[]),
                )
            ],
        )
        _write_report_context(reports_dir, analysis_input, candidates=[])
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "AAPL" in messages[0]
        assert "見つかりませんでした" in messages[0]


class TestAllSkipDay:
    def test_no_trade_reason_is_shown_when_present(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL", "MSFT"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol=symbol,
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="skip", reasons=[]),
                )
                for symbol in ("AAPL", "MSFT")
            ],
            no_trade_reason="All candidates failed the exposure ceiling screen today.",
        )
        _write_report_context(
            reports_dir,
            analysis_input,
            candidates=[_brief_candidate("AAPL"), _brief_candidate("MSFT", rank=2)],
        )
        path = _write_outcome_file(tmp_path, candidates=2)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert len(messages) == 1
        assert "proceed 0 / skip 2 / withheld 0" in messages[0]
        assert "本日 proceed 銘柄はありません" in messages[0]
        assert "All candidates failed the exposure ceiling screen today." in messages[0]

    def test_no_reason_present_falls_back_to_the_generic_note(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(recommendation="skip", reasons=[]),
                )
            ],
        )
        _write_report_context(
            reports_dir, analysis_input, candidates=[_brief_candidate("AAPL")]
        )
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "本日 proceed 銘柄はありません" in messages[0]


class TestWithheldSymbol:
    def test_provenance_failure_withholds_the_symbol_without_leaking_its_text(
        self, tmp_path: Path
    ) -> None:
        reports_dir = tmp_path / "reports"
        analysis_input = _analysis_input(reports_dir, symbols=["AAPL"])
        _write_analysis_result(
            reports_dir,
            analysis_input,
            symbols=[
                SymbolAnalysis(
                    symbol="AAPL",
                    screening_assessment=ScreeningAssessment(summary="OK"),
                    verdict=Verdict(
                        recommendation="proceed",
                        reasons=[
                            VerdictReason(
                                text="Sensitive unverifiable claim.",
                                source_ids=["source-not-in-input"],
                            )
                        ],
                    ),
                )
            ],
        )
        _write_report_context(
            reports_dir, analysis_input, candidates=[_brief_candidate("AAPL")]
        )
        path = _write_outcome_file(tmp_path, candidates=1)

        messages = build_daily_notification(outcome_file=path, reports_dir=reports_dir)

        assert "proceed 0 / skip 0 / withheld 1" in messages[0]
        assert "AAPL" in messages[0]
        assert "Sensitive unverifiable claim." not in messages[0]
        assert "検証不合格" in messages[0]


class TestPureHelpers:
    def test_per_share_risk_subtracts_limit_and_stop(self):
        assert _per_share_risk(151.2, 145.3) == pytest.approx(5.9)

    def test_per_share_risk_is_none_when_either_price_is_missing(self):
        assert _per_share_risk(None, 145.3) is None
        assert _per_share_risk(151.2, None) is None

    def test_shrink_block_leaves_short_blocks_untouched(self):
        block = "■ AAPL\nshort content"

        assert _shrink_block(block, "reports/2026-08-28/run/") == block

    def test_shrink_block_truncates_and_points_at_the_report(self):
        block = "■ AAPL\n" + ("x" * (_MAX_BLOCK_BODY_CHARS + 500))

        shrunk = _shrink_block(block, "reports/2026-08-28/run/")

        assert len(shrunk) <= _MAX_BLOCK_BODY_CHARS
        assert "reports/2026-08-28/run/" in shrunk
        assert "…" in shrunk

    def test_safe_block_passes_through_clean_text(self):
        block = "■ AAPL\nStrong technical setup."

        assert _safe_block(block) == block

    def test_safe_block_withholds_a_forbidden_phrase(self):
        block = "■ AAPL\n強く推奨します。"

        result = _safe_block(block)

        assert "強く推奨" not in result
        assert "■ AAPL" in result
        assert "CON-03" in result

    def test_pack_messages_never_exceeds_the_discord_limit(self):
        header = "[swing-copilot] header line\nsecond header line"
        blocks = [f"■ SYM{i}\n" + ("body " * 200) for i in range(10)]

        messages = _pack_messages(header, blocks, "reports/2026-08-28/run/")

        assert all(len(message) <= DISCORD_MESSAGE_CHAR_LIMIT for message in messages)
        assert len(messages) > 1
        assert messages[0].startswith(header)
        for index, message in enumerate(messages[1:], start=2):
            assert message.startswith(f"(続き {index}/{len(messages)})")

    def test_pack_messages_single_short_block_stays_one_message(self):
        header = "[swing-copilot] header"
        blocks = ["■ AAPL\nshort"]

        messages = _pack_messages(header, blocks, "reports/2026-08-28/run/")

        assert messages == ["[swing-copilot] header\n\n■ AAPL\nshort"]

    def test_pack_messages_never_splits_one_block_across_two_messages(self):
        header = "[swing-copilot] header"
        # Two blocks that together exceed the limit but neither alone does.
        blocks = ["■ AAA\n" + ("a" * 1000), "■ BBB\n" + ("b" * 1000)]

        messages = _pack_messages(header, blocks, "reports/2026-08-28/run/")

        assert len(messages) == 2
        assert "■ AAA" in messages[0]
        assert "■ BBB" not in messages[0]
        assert "■ BBB" in messages[1]

    def test_pack_messages_oversized_single_block_is_shrunk_to_fit(self):
        header = "[swing-copilot] header"
        blocks = ["■ AAPL\n" + ("x" * 5000)]

        messages = _pack_messages(header, blocks, "reports/2026-08-28/run/")

        assert len(messages) == 1
        assert len(messages[0]) <= DISCORD_MESSAGE_CHAR_LIMIT
        assert "…" in messages[0]

    def test_proceed_block_omits_share_counts(self):
        candidate = _brief_candidate("AAPL")
        verdict = Verdict(recommendation="proceed", reasons=[])

        block = _proceed_block("AAPL", verdict, candidate)

        assert "shares" not in block.lower()
        assert "株数" not in block
