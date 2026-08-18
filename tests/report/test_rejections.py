"""Tests for the `rejections.json` run artifact (`report/rejections.py`)."""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import pytest

from swing_copilot.report.rejections import (
    REJECTIONS_FILENAME,
    REJECTIONS_SCHEMA_VERSION,
    RejectionsArtifact,
    write_rejections,
)
from swing_copilot.screening.base import (
    RejectionReasonCode,
    RejectionRecord,
    RejectionStage,
    TruncatedCandidate,
)

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")
AS_OF = date(2027, 3, 1)


def _rejection(symbol: str) -> RejectionRecord:
    return RejectionRecord(
        symbol,
        RejectionStage.TECHNICAL_SIGNAL,
        RejectionReasonCode.SIGNAL_TREND_NOT_MET,
        {"signal": "trend_sma", "close": 10.0, "sma_long": 12.0},
    )


def _truncated(symbol: str, rank: int) -> TruncatedCandidate:
    return TruncatedCandidate(
        symbol=symbol,
        rank=rank,
        score=0.5,
        score_breakdown={
            "score_rsi_pullback": 0.2,
            "score_trend_quality": 0.1,
            "score_liquidity": 0.2,
            "score_atr_pct": 0.0,
        },
        execution_state="FAIR",
        execution_distance=1.25,
    )


def _artifact(
    *,
    rejections: list[RejectionRecord] | None = None,
    truncated: list[TruncatedCandidate] | None = None,
) -> RejectionsArtifact:
    return RejectionsArtifact(
        run_id=RUN_ID,
        as_of=AS_OF,
        strategy_key="minervini_stage2",
        rejections=rejections if rejections is not None else [_rejection("LOSER")],
        truncated=truncated if truncated is not None else [],
    )


def _written(tmp_path, artifact):
    path = write_rejections(artifact, tmp_path / "2027-03-01" / str(RUN_ID))
    return json.loads(path.read_text(encoding="utf-8"))


class TestRunIdentity:
    def test_the_file_lands_in_the_runs_own_directory(self, tmp_path):
        destination_dir = tmp_path / "2027-03-01" / str(RUN_ID)

        path = write_rejections(_artifact(), destination_dir)

        assert path == destination_dir / REJECTIONS_FILENAME
        assert path.is_file()

    def test_the_payload_identifies_the_run_and_strategy(self, tmp_path):
        payload = _written(tmp_path, _artifact())

        assert payload["schema_version"] == REJECTIONS_SCHEMA_VERSION
        assert payload["run_id"] == str(RUN_ID)
        assert payload["as_of"] == "2027-03-01"
        assert payload["strategy_key"] == "minervini_stage2"


class TestRejectionDetail:
    def test_every_rejection_keeps_its_stage_reason_and_detail(self, tmp_path):
        payload = _written(tmp_path, _artifact())

        assert payload["rejections"] == [
            {
                "symbol": "LOSER",
                "stage": "technical_signal",
                "reason_code": "SIGNAL_TREND_NOT_MET",
                "detail": {"signal": "trend_sma", "close": 10.0, "sma_long": 12.0},
            }
        ]

    def test_rejections_are_sorted_by_symbol_regardless_of_input_order(self, tmp_path):
        payload = _written(
            tmp_path,
            _artifact(rejections=[_rejection("ZZZ"), _rejection("AAA")]),
        )

        assert [item["symbol"] for item in payload["rejections"]] == ["AAA", "ZZZ"]

    def test_a_run_with_nothing_rejected_still_writes_both_empty_sections(
        self, tmp_path
    ):
        payload = _written(tmp_path, _artifact(rejections=[], truncated=[]))

        assert payload["rejections"] == []
        assert payload["truncated_by_candidate_limit"] == []


class TestTruncationSection:
    def test_candidate_limit_losers_are_recorded_with_rank_and_score_breakdown(
        self, tmp_path
    ):
        payload = _written(tmp_path, _artifact(truncated=[_truncated("NEARMISS", 11)]))

        assert payload["truncated_by_candidate_limit"] == [
            {
                "symbol": "NEARMISS",
                "rank": 11,
                "score": 0.5,
                "score_breakdown": {
                    "score_rsi_pullback": 0.2,
                    "score_trend_quality": 0.1,
                    "score_liquidity": 0.2,
                    "score_atr_pct": 0.0,
                },
                "execution_state": "FAIR",
                "execution_distance": 1.25,
            }
        ]

    def test_truncations_are_sorted_by_rank_regardless_of_input_order(self, tmp_path):
        payload = _written(
            tmp_path,
            _artifact(truncated=[_truncated("B", 13), _truncated("A", 11)]),
        )

        assert [item["rank"] for item in payload["truncated_by_candidate_limit"]] == [
            11,
            13,
        ]

    def test_an_undefined_execution_distance_is_written_as_null(self, tmp_path):
        item = TruncatedCandidate(
            symbol="NODATA",
            rank=11,
            score=0.0,
            score_breakdown={},
            execution_state="UNKNOWN",
            execution_distance=None,
        )

        payload = _written(tmp_path, _artifact(truncated=[item]))

        assert payload["truncated_by_candidate_limit"][0]["execution_distance"] is None


class TestAtomicReplacement:
    def test_a_rerun_replaces_the_previous_file(self, tmp_path):
        destination_dir = tmp_path / "run"
        write_rejections(_artifact(rejections=[_rejection("FIRST")]), destination_dir)

        path = write_rejections(
            _artifact(rejections=[_rejection("SECOND")]), destination_dir
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert [item["symbol"] for item in payload["rejections"]] == ["SECOND"]

    def test_a_failed_write_preserves_the_previous_file_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        destination_dir = tmp_path / "run"
        write_rejections(_artifact(rejections=[_rejection("KEPT")]), destination_dir)

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("swing_copilot.io_atomic.os.replace", _explode)

        with pytest.raises(OSError, match="disk full"):
            write_rejections(
                _artifact(rejections=[_rejection("LOST")]), destination_dir
            )

        payload = json.loads(
            (destination_dir / REJECTIONS_FILENAME).read_text(encoding="utf-8")
        )
        assert [item["symbol"] for item in payload["rejections"]] == ["KEPT"]
        assert list(destination_dir.glob(".*.tmp")) == []
