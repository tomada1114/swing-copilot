"""Contracts for the deterministic per-expert input slices (Issue #260)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.fragment import AnalysisFragment
from swing_copilot.analysis.slices import (
    InputSlice,
    SliceExportError,
    build_slices,
    write_slices,
)
from tests.analysis.conftest import FILING_ID, NEWS_ID, input_payload

if TYPE_CHECKING:
    from pathlib import Path

    from swing_copilot.analysis.slices import SliceDocument


def candidate_payload(
    symbol: str, *, news: bool = True, filings: bool = True, **overrides: Any
) -> dict[str, Any]:
    """One candidate shaped like `input_payload()`'s, with sources optional."""
    template = input_payload()["candidates"][0]
    payload: dict[str, Any] = {
        **template,
        "symbol": symbol,
        "news": (
            [
                {**item, "source_id": f"{item['source_id']}:{symbol}"}
                for item in template["news"]
            ]
            if news
            else []
        ),
        "filings": (
            [
                {**item, "source_id": f"{item['source_id']}:{symbol}"}
                for item in template["filings"]
            ]
            if filings
            else []
        ),
    }
    if not news:
        payload["news_supply"] = {
            "collected_items": 0,
            "exported_items": 0,
            "symbol_mention_items": 0,
            "level": "none",
        }
    payload.update(overrides)
    return payload


def mixed_payload() -> dict[str, Any]:
    """An input where each expert has a different set of symbols to cover."""
    return input_payload(
        candidates=[
            candidate_payload("AAPL"),
            candidate_payload("MSFT", filings=False),
            candidate_payload("NVDA", news=False, filings=False),
        ]
    )


def slice_by(documents: tuple[SliceDocument, ...], kind: str, symbol: str) -> Any:
    """Return the payload of one produced slice."""
    return next(
        document.payload
        for document in documents
        if document.kind == kind and document.symbol == symbol
    )


def test_build_slices_covers_each_expert_only_where_it_has_sources() -> None:
    payload = mixed_payload()

    documents = build_slices(payload)

    assert [(document.kind, document.symbol) for document in documents] == [
        ("news", "AAPL"),
        ("news", "MSFT"),
        ("filings", "AAPL"),
        ("screening", "AAPL"),
        ("screening", "MSFT"),
        ("screening", "NVDA"),
    ]


def test_every_candidate_gets_a_screening_slice_even_without_any_text() -> None:
    """`screening_assessment` is required for every symbol at ingest."""
    payload = input_payload(
        candidates=[candidate_payload("NVDA", news=False, filings=False)]
    )

    documents = build_slices(payload)

    assert [(document.kind, document.symbol) for document in documents] == [
        ("screening", "NVDA")
    ]


def test_news_slice_carries_the_news_expert_fields_verbatim() -> None:
    payload = mixed_payload()

    news_slice = slice_by(build_slices(payload), "news", "AAPL")

    source = payload["candidates"][0]
    assert news_slice["candidate"] == {
        "symbol": "AAPL",
        "news": source["news"],
        "news_supply": source["news_supply"],
    }
    assert news_slice["candidate"]["news"][0]["source_id"] == f"{NEWS_ID}:AAPL"
    assert news_slice["context"] == {}


def test_filings_slice_carries_only_the_filing_bodies() -> None:
    payload = mixed_payload()

    filings_slice = slice_by(build_slices(payload), "filings", "AAPL")

    source = payload["candidates"][0]
    assert filings_slice["candidate"] == {
        "symbol": "AAPL",
        "filings": source["filings"],
    }
    assert filings_slice["candidate"]["filings"][0]["source_id"] == f"{FILING_ID}:AAPL"
    assert filings_slice["context"] == {}


def test_screening_slice_carries_the_deterministic_blocks_and_run_wide_context() -> (
    None
):
    payload = mixed_payload()

    screening_slice = slice_by(build_slices(payload), "screening", "AAPL")

    source = payload["candidates"][0]
    assert screening_slice["candidate"] == {
        "symbol": "AAPL",
        "score_breakdown": source["score_breakdown"],
        "risk_constraints": source["risk_constraints"],
        "decision_history": source["decision_history"],
        "prior_verdicts": source["prior_verdicts"],
    }
    assert screening_slice["context"] == payload["context"]


def test_an_optional_candidate_key_absent_from_the_input_stays_absent() -> None:
    """An archived input must not grow a key it never had."""
    candidate = candidate_payload("AAPL")
    del candidate["news_supply"]
    del candidate["prior_verdicts"]
    payload = input_payload(candidates=[candidate])

    documents = build_slices(payload)

    assert "news_supply" not in slice_by(documents, "news", "AAPL")["candidate"]
    assert "prior_verdicts" not in slice_by(documents, "screening", "AAPL")["candidate"]


def test_every_slice_repeats_the_run_identity_verbatim() -> None:
    payload = mixed_payload()

    documents = build_slices(payload)

    for document in documents:
        assert list(document.payload) == [
            "run_id",
            "as_of",
            "input_digest",
            "kind",
            "context",
            "candidate",
        ]
        assert document.payload["run_id"] == payload["run_id"]
        assert document.payload["as_of"] == payload["as_of"]
        assert document.payload["input_digest"] == payload["input_digest"]


def test_source_chars_counts_only_the_bodies_that_expert_reads() -> None:
    payload = mixed_payload()
    candidate = payload["candidates"][0]

    documents = {
        (document.kind, document.symbol): document.source_chars
        for document in build_slices(payload)
    }

    assert documents[("filings", "AAPL")] == len(candidate["filings"][0]["text"])
    assert documents[("news", "AAPL")] == len(candidate["news"][0]["headline"]) + len(
        candidate["news"][0]["summary"]
    )
    assert documents[("screening", "AAPL")] > len(candidate["score_breakdown"])


def test_slice_filenames_cannot_be_mistaken_for_analysis_work_fragments() -> None:
    documents = build_slices(mixed_payload())

    assert [document.filename for document in documents] == [
        "slice-news-AAPL.json",
        "slice-news-MSFT.json",
        "slice-filings-AAPL.json",
        "slice-screening-AAPL.json",
        "slice-screening-MSFT.json",
        "slice-screening-NVDA.json",
    ]
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        AnalysisFragment.model_validate(documents[0].payload)


def test_write_slices_produces_utf8_lf_bytes_with_one_trailing_newline(
    tmp_path: Path,
) -> None:
    documents = build_slices(mixed_payload())

    written = write_slices(documents, tmp_path / "slices")

    raw = written[0].read_bytes()
    assert (
        raw
        == json.dumps(
            documents[0].payload, ensure_ascii=False, indent=2, sort_keys=False
        ).encode("utf-8")
        + b"\n"
    )
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")


def test_the_same_input_produces_byte_identical_slices(tmp_path: Path) -> None:
    """Issue #261 reuses a slice by its body hash, which needs stable bytes."""
    payload = mixed_payload()

    first = write_slices(build_slices(payload), tmp_path / "first")
    second = write_slices(build_slices(json.loads(json.dumps(payload))), tmp_path / "b")

    assert [path.name for path in first] == [path.name for path in second]
    assert [path.read_bytes() for path in first] == [
        path.read_bytes() for path in second
    ]


def test_slices_carry_nothing_environment_dependent(tmp_path: Path) -> None:
    """No wall clock, no paths, no host: only what the input already said."""
    written = write_slices(build_slices(mixed_payload()), tmp_path / "slices")

    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "generated_at" not in text
        assert str(tmp_path) not in text


def test_write_slices_replaces_a_stale_slice_of_the_same_name(tmp_path: Path) -> None:
    out_dir = tmp_path / "slices"
    out_dir.mkdir()
    (out_dir / "slice-news-AAPL.json").write_text("stale", encoding="utf-8")

    written = write_slices(build_slices(mixed_payload()), out_dir)

    assert json.loads(written[0].read_text(encoding="utf-8"))["candidate"][
        "symbol"
    ] == ("AAPL")


def test_build_slices_rejects_a_document_that_is_not_an_analysis_input() -> None:
    with pytest.raises(SliceExportError, match="failed schema validation"):
        build_slices({"schema_version": "analysis-input-v3"})


def test_build_slices_rejects_two_symbols_that_would_claim_one_file() -> None:
    """A case-insensitive filesystem lets the second write hide the first."""
    payload = input_payload(
        candidates=[candidate_payload("AAPL"), candidate_payload("aapl")]
    )

    with pytest.raises(SliceExportError, match="would both write"):
        build_slices(payload)


def test_build_slices_rejects_a_symbol_that_cannot_be_a_filename() -> None:
    payload = input_payload(candidates=[candidate_payload("../etc/passwd")])

    with pytest.raises(SliceExportError, match="cannot be used in a slice filename"):
        build_slices(payload)


@pytest.mark.parametrize(
    ("kind", "candidate", "expected"),
    [
        pytest.param(
            "news",
            {"symbol": "AAPL", "news": [], "filings": []},
            "must not carry candidate fields filings",
            id="news-carrying-filings",
        ),
        pytest.param(
            "filings",
            {"symbol": "AAPL"},
            "must carry candidate fields filings",
            id="filings-without-filings",
        ),
        pytest.param(
            "screening",
            {"symbol": "AAPL", "score_breakdown": "s"},
            "must carry candidate fields risk_constraints",
            id="screening-missing-block",
        ),
    ],
)
def test_input_slice_rejects_a_payload_outside_its_experts_grouping(
    kind: str, candidate: dict[str, Any], expected: str
) -> None:
    payload = input_payload()

    with pytest.raises(ValueError, match=expected):
        InputSlice.model_validate(
            {
                "run_id": payload["run_id"],
                "as_of": payload["as_of"],
                "input_digest": payload["input_digest"],
                "kind": kind,
                "context": {},
                "candidate": candidate,
            }
        )


def test_input_slice_rejects_run_wide_context_for_a_text_expert() -> None:
    payload = input_payload()

    with pytest.raises(ValueError, match="must not carry run-wide context"):
        InputSlice.model_validate(
            {
                "run_id": payload["run_id"],
                "as_of": payload["as_of"],
                "input_digest": payload["input_digest"],
                "kind": "news",
                "context": {"market_regime": "risk-on"},
                "candidate": {"symbol": "AAPL", "news": []},
            }
        )


def test_input_slice_rejects_an_unknown_field() -> None:
    payload = input_payload()

    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        InputSlice.model_validate(
            {
                "run_id": payload["run_id"],
                "as_of": payload["as_of"],
                "input_digest": payload["input_digest"],
                "kind": "news",
                "context": {},
                "candidate": {"symbol": "AAPL", "news": []},
                "generated_at": "2027-03-01T12:00:00Z",
            }
        )


def test_every_produced_slice_passes_the_strict_slice_schema() -> None:
    documents = build_slices(mixed_payload())

    for document in documents:
        parsed = InputSlice.model_validate(document.payload)
        assert parsed.kind == document.kind
        assert parsed.candidate.symbol == document.symbol
