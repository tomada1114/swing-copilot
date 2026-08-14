"""Shared builders for the retrospective (`retro/`) tests.

Reuses `tests/analysis/conftest.py`'s payload builders so the fixtures stay
bound to the one strict schema pair `collect` actually parses, rather than
drifting into a second hand-maintained copy of it.

The `retro-input-v1` / `retro-result-v1` builders live here for the same
reason: the schema tests and the ingest tests must agree on one document pair,
or an ingest test could pass against a dossier the schema would reject.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.schemas import canonical_json_digest
from swing_copilot.storage.database import Database
from swing_copilot.storage.market_store import MarketStore
from tests.analysis.conftest import (
    AS_OF,
    CALENDAR_ID,
    FILING_ID,
    NEWS_ID,
    RUN_ID,
    input_payload,
    result_payload,
    symbol_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from pathlib import Path

__all__ = [
    "AS_OF",
    "CALENDAR_ID",
    "FILING_ID",
    "NEWS_ID",
    "RUN_ID",
    "input_payload",
    "result_payload",
    "symbol_payload",
]

#: Identities the dossier below supplies, and the only ones a result may cite.
RETRO_RUN_ID = "11111111-1111-1111-1111-111111111111"
RETRO_AS_OF = "2027-03-29"
SURPRISE_ID = f"surprise:{RETRO_RUN_ID}:AAPL"
SEPARATION_METRIC_ID = "metric:separation:5d"
CITED_SOURCE_ID = "finnhub:1"


def retro_input_unsigned_payload() -> dict[str, Any]:
    """A complete `retro-input-v1` body, digest excluded."""
    return {
        "schema_version": "retro-input-v1",
        "as_of": RETRO_AS_OF,
        "generated_at": "2027-03-29T00:00:00Z",
        "window_start": "2026-12-29",
        "evaluation": {
            "horizon_5d_weight": 0.6,
            "horizon_20d_weight": 0.4,
            "neutral_threshold_pct": 0.5,
            "severe_threshold_pct": 2.0,
            "preliminary_sample_threshold": 20,
            "lookback_window_days": 90,
            "proceed_severe_miss_watch_rate": 0.15,
        },
        "aggregates": {
            "separation": [
                {
                    "metric_id": SEPARATION_METRIC_ID,
                    "horizon_days": 5,
                    "value": -0.9,
                    "sample_size": 3,
                    "is_preliminary": True,
                }
            ],
            "proceed_severe_miss_rate": [
                {
                    "metric_id": "metric:proceed_severe_miss_rate:5d",
                    "horizon_days": 5,
                    "value": 0.5,
                    "baseline_value": 0.33,
                    "is_flagged": True,
                    "sample_size": 2,
                    "is_preliminary": True,
                }
            ],
            "skip_hit_rate": [
                {
                    "metric_id": "metric:skip_hit_rate:composed",
                    "horizon_days": None,
                    "value": None,
                    "baseline_value": None,
                    "is_flagged": False,
                    "sample_size": 0,
                    "is_preliminary": True,
                }
            ],
            "verdict_mix": {
                "metric_id": "verdict_mix",
                "run_count": 1,
                "verdict_count": 4,
                "proceed_count": 2,
                "skip_count": 2,
                "proceed_ratio": 0.5,
                "is_flagged": False,
            },
        },
        "signal_performance": [
            {
                "signal_name": "rsi_pullback",
                "true_positive_count": 2,
                "false_positive_count": 1,
                "neutral_count": 0,
                "hit_rate": 0.6,
                "n": 3,
                "is_preliminary": True,
            }
        ],
        "human_alignment": [
            {
                "cell_id": "metric:human_alignment:followed:proceed:5d",
                "decision": "followed",
                "recommendation": "proceed",
                "horizon_days": 5,
                "count": 2,
                "mean_forward_return_pct": 1.25,
                "hit_count": 1,
                "severe_miss_count": 1,
            }
        ],
        "source_contribution": [
            {
                "contribution_id": "metric:source_contribution:news:finnhub",
                "source_type": "news",
                "provider": "finnhub",
                "citation_count": 3,
                "hit_citation_count": 2,
                "miss_citation_count": 1,
                "neutral_citation_count": 0,
                "hit_citation_ratio": 0.6666666666666666,
            }
        ],
        "surprises": {
            "max_surprises": 5,
            "dropped_count": 1,
            "items": [
                {
                    "surprise_id": SURPRISE_ID,
                    "run_id": RETRO_RUN_ID,
                    "symbol": "AAPL",
                    "run_as_of": "2027-03-01",
                    "strategy_key": "default",
                    "recommendation": "proceed",
                    "no_trade": False,
                    "reasons": [
                        {"text": "受注は堅調に見える", "source_ids": [CITED_SOURCE_ID]}
                    ],
                    "cited_source_ids": [CITED_SOURCE_ID],
                    "outcomes": [
                        {
                            "horizon_days": 5,
                            "maturity_as_of": "2027-03-08",
                            "forward_return_pct": -8.0,
                            "classification": "MISS_SEVERE",
                        }
                    ],
                    "max_adverse_return_pct": -9.5,
                    "freshness": {
                        "news": [
                            {
                                "source_id": "finnhub:9",
                                "published_at": "2027-03-05T00:00:00Z",
                                "headline": "見出し",
                                "summary": "本文",
                                "url": "https://example.test/9",
                                "provider": "finnhub",
                            }
                        ],
                        "filings": [],
                        "fetch_failed": False,
                    },
                }
            ],
        },
        "config_snapshot": {
            "sections": {"retro": {"max_surprises": 5}},
            "config_hash": "0" * 64,
        },
        "proposals_ledger": {
            "path": "docs/retro/proposals.md",
            "exists": False,
            "rejected_proposal_ids": [],
        },
        "notes": ["AAPL: 鮮度開示を取得できなかったため空欄"],
    }


def retro_input_payload(**overrides: Any) -> dict[str, Any]:
    """A signed `retro-input-v1` document, digest recomputed after overrides."""
    unsigned = {**retro_input_unsigned_payload(), **overrides}
    return {
        **unsigned,
        "input_digest": canonical_json_digest(unsigned, excluded_field="input_digest"),
    }


def retro_input_digest() -> str:
    """The digest a matching `retro-result-v1` must copy verbatim."""
    return canonical_json_digest(
        retro_input_unsigned_payload(), excluded_field="input_digest"
    )


def proposal_payload(**overrides: Any) -> dict[str, Any]:
    """One `retro-result-v1` proposal, citing only supplied identifiers."""
    return {
        "proposal_key": "config:postmortem.severe_threshold_pct",
        "level": "L1",
        "target": "postmortem.severe_threshold_pct",
        "title": "重大境界の見直し",
        "claim": "separation が負のまま推移している可能性がある",
        "expected_effect": "重大逆行の分類が実態に近づくと考えられる",
        "evidence_refs": [SEPARATION_METRIC_ID],
        "evidence_basis": "quantitative",
        "verification_plan": "copilot-backtest で変更前後を比較し、最大DDが悪化しないこと",
        "risks": ["サンプルが小さく暫定域である"],
        "reopen_justification": None,
        **overrides,
    }


def narration_payload(**overrides: Any) -> dict[str, Any]:
    """One surprise narration, citing only supplied identifiers."""
    return {
        "surprise_id": SURPRISE_ID,
        "failure_class": "information_absent",
        "narrative": "当時の入力に材料が無く、後から出た開示に兆候が読める",
        "evidence_refs": [SURPRISE_ID, CITED_SOURCE_ID],
        **overrides,
    }


def retro_result_payload(**overrides: Any) -> dict[str, Any]:
    """A `retro-result-v1` document answering `retro_input_payload()`."""
    return {
        "schema_version": "retro-result-v1",
        "as_of": RETRO_AS_OF,
        "input_digest": retro_input_digest(),
        "structural_review_note": "再点検の上で L2/L3 相当の構造的観察はなし",
        "narrations": [narration_payload()],
        "proposals": [proposal_payload()],
        **overrides,
    }


@pytest.fixture
def market_store(tmp_path: Path) -> MarketStore:
    """Bars source sharing the `state_store` fixture's database path."""
    return MarketStore(
        Database(tmp_path / "copilot.duckdb"), parquet_root=tmp_path / "bars"
    )


def bars(symbol: str, prices: dict[date, float]) -> pd.DataFrame:
    """Tidy `BARS_COLUMNS` frame with `close` equal to each mapped price."""
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "date": bar_date,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1_000_000,
                "provider": "test",
                "fetched_at": datetime(2027, 3, 1, tzinfo=UTC),
            }
            for bar_date, price in prices.items()
        ]
    )


@pytest.fixture
def reports_root(tmp_path: Path) -> Path:
    """An empty `reports/` root, mirroring `pipeline/daily.py`'s output dir."""
    root = tmp_path / "reports"
    root.mkdir()
    return root


#: Distinguishes "caller said nothing" (write the default document) from an
#: explicit `None` (leave that document out of the run directory entirely).
_DEFAULT: Any = object()


@pytest.fixture
def write_run(reports_root: Path) -> Callable[..., Path]:
    """Write one `reports/<date>/<run_id>/` pair of analysis documents.

    Passing `None` for either document omits that file, which is how the
    fail-soft tests build an incomplete run archive.
    """

    def _write(
        analysis_input: dict[str, Any] | str | None = _DEFAULT,
        result: dict[str, Any] | str | None = _DEFAULT,
        *,
        run_id: str = RUN_ID,
        run_date: date = AS_OF,
    ) -> Path:
        directory = reports_root / run_date.isoformat() / run_id
        directory.mkdir(parents=True, exist_ok=True)
        if analysis_input is not None:
            _dump(
                directory / ANALYSIS_INPUT_FILENAME,
                input_payload() if analysis_input is _DEFAULT else analysis_input,
            )
        if result is not None:
            _dump(
                directory / ANALYSIS_RESULT_FILENAME,
                result_payload() if result is _DEFAULT else result,
            )
        return directory

    return _write


def _dump(path: Path, payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
