"""Machine-checked run-identity requirements for the swing-daily instructions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from pydantic import BaseModel

from swing_copilot.analysis.schemas import VERDICT_BASES, AnalysisResult

if TYPE_CHECKING:
    from collections.abc import Iterator

_SKILLS = Path(__file__).parents[2] / ".claude" / "skills"
_SKILL_ROOT = _SKILLS / "swing-daily"
#: Every skill that writes a document `copilot-verify-analysis` can check.
_FRAGMENT_AUTHORS = ("analyze-news", "analyze-filings", "interpret-screening")


def _read_output_schema() -> str:
    """Return the instruction text the skill actually reads before writing."""
    return (_SKILL_ROOT / "references" / "output-schema.md").read_text(encoding="utf-8")


def _nested_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Yield every `BaseModel` reachable from one field annotation.

    Field types are wrapped (`list[X]`, `X | None`, `Optional[X]`), so the
    model has to be dug out of the type arguments rather than read off the
    annotation directly.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def _contract_field_names(root: type[BaseModel]) -> set[str]:
    """Collect every field name in the model graph rooted at `root`."""
    names: set[str] = set()
    visited: set[type[BaseModel]] = set()
    pending = [root]
    while pending:
        model = pending.pop()
        if model in visited:
            continue
        visited.add(model)
        for field_name, field in model.model_fields.items():
            names.add(field_name)
            pending.extend(_nested_models(field.annotation))
    return names


def test_skill_reuses_only_fragments_bound_to_the_same_input() -> None:
    """Prevent a future instruction edit from reintroducing as-of-only reuse."""
    skill = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    schema = (_SKILL_ROOT / "references" / "output-schema.md").read_text(
        encoding="utf-8"
    )

    assert "run_id`、`as_of`、`input_digest`" in skill
    assert "analysis-result-v3" in skill
    assert "evidence_quote" in schema
    assert '"input_digest"' in schema
    assert "reports/<run_date>/<run_id>/" in schema
    assert "input にある symbol を落とさない" in skill
    assert "入力と完全一致" in schema
    assert "no_trade=true" in schema
    assert "NFKC" in schema


def test_news_skill_must_declare_a_thin_symbol_specific_supply() -> None:
    """Keep Issue #130's declaration route in the instructions, not just in code.

    `news_supply` is only observable downstream if the news expert is told to
    read it and to say so; nothing at ingest can reconstruct the declaration
    from an omission.
    """
    news_skill = (_SKILLS / "analyze-news" / "SKILL.md").read_text(encoding="utf-8")
    schema = (_SKILL_ROOT / "references" / "output-schema.md").read_text(
        encoding="utf-8"
    )

    assert "news_supply" in news_skill
    assert "symbol_mention_items" in news_skill
    assert "材料供給不足:" in news_skill
    assert "悪材料の不在を好材料として書かない" in news_skill
    assert "news_supply" in schema
    assert "「悪材料が見当たらない」を根拠に使わない" in schema


@pytest.mark.parametrize(
    "skill_name",
    [pytest.param(name, id=name) for name in (*_FRAGMENT_AUTHORS, "swing-daily")],
)
def test_every_fragment_author_is_pointed_at_the_shared_checker(
    skill_name: str,
) -> None:
    """Keep Issue #132's shared command in the instructions, not just in code.

    A checker nobody is told to run leaves each expert writing its own, and a
    hand-rolled one cannot reproduce the NFKC normalization `safety.py` and
    `evidence.py` apply -- so it reports a pass on text ingest will withhold.
    """
    skill = (_SKILLS / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert "copilot-verify-analysis" in skill
    assert "検証スクリプト" in skill


def test_the_schema_reference_binds_the_checker_to_the_ingest_function() -> None:
    """Name the shared function, so a rename cannot quietly weaken the claim."""
    schema = (_SKILL_ROOT / "references" / "output-schema.md").read_text(
        encoding="utf-8"
    )

    assert "copilot-verify-analysis" in schema
    assert "verify_symbol_analysis" in schema
    assert "自前の検証スクリプトを書かずに" in schema
    # AC16 exists (Issue #131), so the documented self-check range must cover it.
    assert '"ac_check": "AC1-AC16 違反なし"' in schema


def test_every_result_schema_field_is_named_in_the_output_schema_reference() -> None:
    """Fail the day `schemas.py` and the skill's instructions drift apart.

    `AnalysisResult` is the skill-authored direction, and `output-schema.md` is
    the only place the skill is told what to write. A field added or renamed on
    one side alone is invisible until ingest, where `extra="forbid"` hard-fails
    every symbol -- a whole day's analysis lost to a documentation gap.

    The bar is deliberately a plain substring search over the whole document,
    not a JSON-block parse: generic names (`text`, `symbol`, `summary`) also
    occur in the surrounding Japanese prose, and a stricter check would trade
    this test's job -- noticing an *undocumented* field -- for brittleness
    about where the field happens to be documented.
    """
    documented = _read_output_schema()
    fields = _contract_field_names(AnalysisResult)

    missing = sorted(name for name in fields if name not in documented)
    assert not missing, f"output-schema.md does not mention: {missing}"


def test_every_verdict_basis_value_is_named_in_the_output_schema_reference() -> None:
    """Keep the closed `basis` vocabulary and its instructions in step.

    `basis` is a closed set (Issue #191), so the two directions fail
    differently and both are silent until it is too late: a value the schema
    accepts but the instructions never name can never be written by the skill,
    and a value the instructions still name after it was removed from the
    schema hard-fails the whole run at ingest.
    """
    documented = _read_output_schema()

    missing = sorted(basis for basis in VERDICT_BASES if basis not in documented)
    assert not missing, f"output-schema.md does not mention basis values: {missing}"
