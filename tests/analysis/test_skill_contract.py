"""Machine-checked run-identity requirements for the swing-daily instructions."""

from __future__ import annotations

from pathlib import Path

_SKILLS = Path(__file__).parents[2] / ".claude" / "skills"
_SKILL_ROOT = _SKILLS / "swing-daily"


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
