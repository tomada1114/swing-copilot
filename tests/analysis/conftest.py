"""Shared builders for the analysis-boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.export import (
    ANALYSIS_INPUT_FILENAME,
    ANALYSIS_RESULT_FILENAME,
)
from swing_copilot.analysis.fragment import PAYLOAD_FIELD_BY_KIND
from swing_copilot.analysis.schemas import (
    INPUT_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    canonical_json_digest,
    filing_body_digest,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from swing_copilot.analysis.fragment import FragmentKind


AS_OF = date(2027, 3, 1)
NEWS_ID = "finnhub:1"
FILING_ID = "edgar:0000320193-27-000001"
CALENDAR_ID = "fred:1:2027-03-05"
RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
STRATEGY_KEY = "default"
#: Verbatim excerpts of the bodies `input_payload()` exports, long enough to
#: clear `MIN_EVIDENCE_QUOTE_CHARS`.
NEWS_QUOTE = "announced a new product line"
FILING_QUOTE = "Quarterly report"


def input_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, valid `analysis_input.json` payload for one candidate."""
    payload: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "as_of": AS_OF.isoformat(),
        "strategy_key": STRATEGY_KEY,
        "generated_at": datetime(2027, 3, 1, 12, tzinfo=UTC).isoformat(),
        "context": {
            "market_regime": "<market_regime>\n</market_regime>\n",
            "calendar_events": [
                {
                    "source_id": CALENDAR_ID,
                    "published_at": datetime(2027, 3, 5, tzinfo=UTC).isoformat(),
                    "title": "Employment Situation",
                    "summary": "Employment Situation",
                    "url": "https://fred.stlouisfed.org/release?rid=1",
                    "provider": "fred",
                }
            ],
        },
        "candidates": [
            {
                "symbol": "AAPL",
                "score_breakdown": "<score_breakdown>\n</score_breakdown>\n",
                "risk_constraints": "<risk_constraints>\n</risk_constraints>\n",
                "prior_verdicts": None,
                "news": [
                    {
                        "source_id": NEWS_ID,
                        "published_at": datetime(2027, 2, 28, tzinfo=UTC).isoformat(),
                        "headline": "AAPL news",
                        "summary": "AAPL announced a new product line.",
                        "url": "https://example.com/news",
                        "provider": "finnhub",
                    }
                ],
                "news_supply": {
                    "collected_items": 1,
                    "exported_items": 1,
                    "symbol_mention_items": 1,
                    "level": "sparse",
                },
                "filings": [
                    {
                        "source_id": FILING_ID,
                        "form_type": "10-Q",
                        "filed_at": datetime(2027, 2, 20, tzinfo=UTC).isoformat(),
                        "text": "Quarterly report body.",
                        "url": "https://example.com/filing",
                        "coverage": {
                            "original_chars": 22,
                            "exported_chars": 22,
                            "is_truncated": False,
                            "selection_mode": "full",
                            "exhibit_truncated": False,
                            "sections": [],
                        },
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    payload["input_digest"] = canonical_json_digest(
        payload, excluded_field="input_digest"
    )
    return payload


def result_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, valid `analysis_result.json` payload answering `input_payload()`."""
    payload: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "as_of": AS_OF.isoformat(),
        "strategy_key": STRATEGY_KEY,
        "input_digest": input_payload()["input_digest"],
        "generated_by": "swing-daily skill",
        "symbols": [symbol_payload()],
        "no_trade": False,
        "no_trade_reason": None,
    }
    payload.update(overrides)
    return payload


def symbol_payload(**overrides: Any) -> dict[str, Any]:
    """One symbol's analysis, valid against `input_payload()`'s source IDs."""
    payload: dict[str, Any] = {
        "symbol": "AAPL",
        "news_summary": {
            "facts": [
                {
                    "text": "A new product line was announced.",
                    "source_ids": [NEWS_ID],
                    "evidence_quote": NEWS_QUOTE,
                }
            ],
            "interpretation": ["May support near-term revenue."],
            "risk_flags": ["Execution risk remains."],
        },
        "filing_analyses": [
            {
                "source_id": FILING_ID,
                "facts": [
                    {
                        "text": "Revenue rose year over year.",
                        "source_ids": [FILING_ID],
                        "evidence_quote": FILING_QUOTE,
                    }
                ],
                "interpretation": ["May indicate steady demand."],
                "red_flags": [],
                "yoy_changes": ["Revenue +8%"],
            }
        ],
        "screening_assessment": {
            "summary": "Survived on trend quality with adequate liquidity.",
            "strengths": ["Trend intact"],
            "concerns": ["Extended from the 50-day"],
        },
        "verdict": {
            "recommendation": "proceed",
            "reasons": [
                {"text": "No contradicting disclosure.", "source_ids": [FILING_ID]}
            ],
        },
    }
    payload.update(overrides)
    return payload


def fragment_payload(kind: FragmentKind = "news", **overrides: Any) -> dict[str, Any]:
    """One `analysis_work/<kind>-AAPL.json` fragment answering `input_payload()`.

    The payload key defaults to the one `kind` owns; pass it explicitly in
    `overrides` to exercise a fragment that carries the wrong number of keys.
    A filings fragment also gets the `filing_body_digests` its contract
    requires, digesting the bodies `input_payload()` exports (Issue #261).
    """
    symbol = symbol_payload()
    payload: dict[str, Any] = {
        "run_id": RUN_ID,
        "as_of": AS_OF.isoformat(),
        "input_digest": input_payload()["input_digest"],
        "symbol": "AAPL",
        "ac_check": "AC1-AC16 違反なし",
    }
    payload[PAYLOAD_FIELD_BY_KIND[kind]] = symbol[PAYLOAD_FIELD_BY_KIND[kind]]
    if kind == "filings":
        payload["filing_body_digests"] = exported_filing_digests()
    payload.update(overrides)
    return payload


def exported_filing_digests(
    payload: dict[str, Any] | None = None, symbol: str = "AAPL"
) -> dict[str, str]:
    """The `source_id` -> body digest map one candidate's filings hash to."""
    document = input_payload() if payload is None else payload
    candidate = next(
        item for item in document["candidates"] if item["symbol"] == symbol
    )
    return {
        filing["source_id"]: filing_body_digest(filing["text"])
        for filing in candidate["filings"]
    }


@pytest.fixture
def write_documents(tmp_path: Path) -> Callable[..., tuple[Path, Path]]:
    """Write input/result JSON into a shared directory and return their paths."""

    def _write(
        analysis_input: dict[str, Any] | str | None = None,
        result: dict[str, Any] | str | None = None,
    ) -> tuple[Path, Path]:
        directory = tmp_path / "reports" / AS_OF.isoformat() / RUN_ID
        directory.mkdir(parents=True, exist_ok=True)
        input_path = directory / ANALYSIS_INPUT_FILENAME
        result_path = directory / ANALYSIS_RESULT_FILENAME
        _dump(input_path, input_payload() if analysis_input is None else analysis_input)
        _dump(result_path, result_payload() if result is None else result)
        return input_path, result_path

    return _write


def _dump(path: Path, payload: dict[str, Any] | str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
