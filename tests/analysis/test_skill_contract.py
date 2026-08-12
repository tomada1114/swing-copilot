"""Machine-checked run-identity requirements for the swing-daily instructions."""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).parents[2] / ".claude" / "skills"
_SKILL_ROOT = _SKILLS / "swing-daily"
#: Every skill that writes a document `copilot-verify-analysis` can check.
_FRAGMENT_AUTHORS = ("analyze-news", "analyze-filings", "interpret-screening")


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
