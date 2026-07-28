"""Machine-checked run-identity requirements for the swing-daily instructions."""

from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = Path(__file__).parents[2] / ".claude" / "skills" / "swing-daily"


def test_skill_reuses_only_fragments_bound_to_the_same_input() -> None:
    """Prevent a future instruction edit from reintroducing as-of-only reuse."""
    skill = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    schema = (_SKILL_ROOT / "references" / "output-schema.md").read_text(
        encoding="utf-8"
    )

    assert "run_id`、`as_of`、`input_digest`" in skill
    assert "analysis-result-v2" in skill
    assert '"input_digest"' in schema
    assert "reports/<run_date>/<run_id>/" in schema
    assert "input にある symbol を落とさない" in skill
    assert "入力と完全一致" in schema
    assert "no_trade=true" in schema
    assert "NFKC" in schema
