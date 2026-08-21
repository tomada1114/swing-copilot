"""Contracts for the deterministic per-expert input slices (Issue #260)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from swing_copilot.analysis.fragment import AnalysisFragment
from swing_copilot.analysis.schemas import filing_body_digest
from swing_copilot.analysis.slices import (
    MAX_FILING_SLICE_TEXT_LINE_CHARS,
    InputSlice,
    SliceExportError,
    build_slices,
    write_slices,
)
from tests.analysis.conftest import FILING_ID, NEWS_ID, input_payload
from tests.test_io_atomic import partial_write_then_fail

if TYPE_CHECKING:
    from swing_copilot.analysis.slices import SliceDocument


def candidate_payload(
    symbol: str,
    *,
    news: bool = True,
    filings: bool = True,
    supply: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """One candidate shaped like `input_payload()`'s, with sources optional.

    `supply=False` drops `news_supply` entirely, which is how an input
    archived before Issue #130 -- and only that -- looks.
    """
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
            "collected_items": 12,
            "exported_items": 0,
            "symbol_mention_items": 0,
            "level": "none",
        }
    if not supply:
        payload.pop("news_supply", None)
    payload.update(overrides)
    return payload


def mixed_payload() -> dict[str, Any]:
    """An input where each expert has a different set of symbols to cover."""
    return input_payload(
        candidates=[
            candidate_payload("AAPL"),
            candidate_payload("MSFT", filings=False),
            candidate_payload("NVDA", news=False, filings=False),
            candidate_payload("TSLA", news=False, filings=False, supply=False),
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
        ("screening", "TSLA"),
    ]


@pytest.mark.parametrize(
    "supply",
    [pytest.param(True, id="supply-measured"), pytest.param(False, id="no-supply")],
)
def test_a_candidate_without_news_gets_no_news_slice(supply: bool) -> None:
    """An empty `news[]` decides it, with or without a `news_supply` record.

    A supply record says why the news is thin (Issue #130), but the expert it
    would be handed to is required by `analyze-news/SKILL.md` and AC14 to
    write `news_summary: null` for an empty `news[]`. Slicing for those
    symbols would therefore buy one subagent per newsless symbol and declare
    nothing; carrying the record through to the report is a change to the
    expert's contract, tracked separately.
    """
    payload = input_payload(
        candidates=[candidate_payload("NVDA", news=False, filings=False, supply=supply)]
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
    source_filing = source["filings"][0]
    assert filings_slice["candidate"] == {
        "symbol": "AAPL",
        "filings": [
            {
                **{key: value for key, value in source_filing.items() if key != "text"},
                "text_chunks": [source_filing["text"]],
            }
        ],
    }
    assert filings_slice["candidate"]["filings"][0]["source_id"] == f"{FILING_ID}:AAPL"
    assert filings_slice["context"] == {}


def test_a_large_filing_is_chunked_without_changing_its_body_or_digest(
    tmp_path: Path,
) -> None:
    """Keep every Read-visible line short while preserving provenance input."""
    candidate = candidate_payload("AAPL")
    original = ('Quoted "text" \\ path\n本文の行。' * 5_000) + "終端"
    filing = candidate["filings"][0]
    filing["text"] = original
    filing["coverage"] = {
        **filing["coverage"],
        "original_chars": len(original),
        "exported_chars": len(original),
    }
    payload = input_payload(candidates=[candidate])

    document = slice_by(build_slices(payload), "filings", "AAPL")
    chunked = document["candidate"]["filings"][0]["text_chunks"]

    assert len(chunked) > 1
    assert "".join(chunked) == original
    assert document["filing_body_digests"][filing["source_id"]] == filing_body_digest(
        original
    )

    written = write_slices(build_slices(payload), tmp_path / "slices")
    filings_path = next(
        path for path in written if path.name == "slice-filings-AAPL.json"
    )
    raw_text = filings_path.read_text(encoding="utf-8")
    raw_lines = raw_text.splitlines()
    assert max(map(len, raw_lines)) <= MAX_FILING_SLICE_TEXT_LINE_CHARS
    parsed = InputSlice.model_validate(json.loads(raw_text))
    assert parsed.candidate.filings is not None
    assert "".join(parsed.candidate.filings[0].text_chunks) == original


def test_only_the_filings_slice_carries_the_reuse_digests() -> None:
    """Issue #261's key exists exactly where a filing body does.

    A news or screening reading is `as_of`-dependent and can never be reused
    across runs, so a digest on its slice would only invite an expert to copy
    a key its fragment schema forbids.
    """
    payload = mixed_payload()

    documents = build_slices(payload)

    source = payload["candidates"][0]
    assert slice_by(documents, "filings", "AAPL")["filing_body_digests"] == {
        filing["source_id"]: filing_body_digest(filing["text"])
        for filing in source["filings"]
    }
    for kind, symbol in (("news", "AAPL"), ("screening", "AAPL")):
        assert "filing_body_digests" not in slice_by(documents, kind, symbol)


def test_the_reuse_digest_follows_the_exported_body_not_the_original() -> None:
    """Truncating a filing further must produce a different key.

    `coverage` records how much of the collected filing reached the export, so
    two runs can offer the same accession with different amounts of its text.
    The reading was written from what was exported, and only that.
    """
    candidate = candidate_payload("AAPL")
    filing = candidate["filings"][0]
    truncated = input_payload(
        candidates=[
            {
                **candidate,
                "filings": [
                    {
                        **filing,
                        "text": filing["text"][:12],
                        "coverage": {
                            **filing["coverage"],
                            "exported_chars": 12,
                            "is_truncated": True,
                        },
                    }
                ],
            }
        ]
    )

    before = slice_by(
        build_slices(input_payload(candidates=[candidate])), "filings", "AAPL"
    )
    after = slice_by(build_slices(truncated), "filings", "AAPL")

    assert set(before["filing_body_digests"]) == set(after["filing_body_digests"])
    assert before["filing_body_digests"] != after["filing_body_digests"]


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
        expected_keys = [
            "run_id",
            "as_of",
            "input_digest",
            "kind",
            "context",
            "candidate",
        ]
        if document.kind == "filings":
            # Issue #261's reuse key, present only where a body exists to hash.
            expected_keys.append("filing_body_digests")
        assert list(document.payload) == expected_keys
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
        "slice-screening-TSLA.json",
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


def test_a_failed_write_leaves_no_slice_and_no_temporary_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set is one logical write: seven files must not survive the eighth.

    A command that exits non-zero tells the orchestrator nothing was produced,
    and this workflow never deletes anything from the CI scratch directory, so a partial
    set would sit there unnoticed. The fake fills the file before failing, as
    ENOSPC does, so the temporary of the *failing* write counts too.
    """
    out_dir = tmp_path / "slices"
    monkeypatch.setattr(Path, "write_text", partial_write_then_fail(on_call=3))

    with pytest.raises(OSError, match="disk full"):
        write_slices(build_slices(mixed_payload()), out_dir)

    assert list(out_dir.iterdir()) == []


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
            id="screening-missing-blocks",
        ),
        pytest.param(
            "screening",
            {
                "symbol": "AAPL",
                "score_breakdown": "s",
                "risk_constraints": "r",
                "news": [],
            },
            "must not carry candidate fields news",
            id="screening-carrying-news",
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


@pytest.mark.parametrize(
    ("kind", "candidate", "digests", "expected"),
    [
        pytest.param(
            "news",
            {"symbol": "AAPL", "news": []},
            {"edgar:1": "a" * 64},
            "must not carry filing_body_digests",
            id="news-claiming-a-filing-digest",
        ),
        pytest.param(
            "filings",
            {"symbol": "AAPL", "filings": []},
            {"edgar:1": "a" * 64},
            "digest of every filing body",
            id="digest-for-a-body-the-slice-does-not-carry",
        ),
    ],
)
def test_input_slice_rejects_reuse_digests_that_do_not_match_its_bodies(
    kind: str, candidate: dict[str, Any], digests: dict[str, str], expected: str
) -> None:
    """The expert copies this map without checking it, so the slice must.

    A digest naming a body the slice never handed over would let the reading
    claim coverage it does not have, and the claim is later compared against
    the *input* -- which would agree with the forged map (Issue #261).
    """
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
                "filing_body_digests": digests,
            }
        )


def test_every_produced_slice_passes_the_strict_slice_schema() -> None:
    documents = build_slices(mixed_payload())

    for document in documents:
        parsed = InputSlice.model_validate(document.payload)
        assert parsed.kind == document.kind
        assert parsed.candidate.symbol == document.symbol
