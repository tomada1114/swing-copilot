"""CON-03 machine checks over skill output (`analysis/safety.py`)."""

from __future__ import annotations

import pytest

from swing_copilot.analysis.safety import (
    FORBIDDEN_PHRASES,
    ForbiddenLanguageError,
    check_display_texts,
    check_no_imperative_language,
    check_no_unevidenced_behavioral_claims,
)


class TestImperativeLanguage:
    @pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
    def test_every_listed_phrase_is_rejected(self, phrase):
        with pytest.raises(ForbiddenLanguageError, match="forbidden imperative"):
            check_no_imperative_language([f"分析結果: {phrase} と判断します。"])

    def test_case_is_ignored(self):
        with pytest.raises(ForbiddenLanguageError, match="forbidden imperative"):
            check_no_imperative_language(["This is a STRONG BUY setup."])

    def test_neutral_text_passes(self):
        check_no_imperative_language(
            ["決算は市場予想を上回った。", "Revenue grew year over year."]
        )

    def test_an_empty_collection_passes(self):
        check_no_imperative_language([])


class TestUnevidencedBehavioralClaims:
    def test_a_bare_psychological_claim_is_rejected(self):
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_no_unevidenced_behavioral_claims(["投資家心理が悪化している。"])

    def test_a_hedged_claim_without_numbers_is_still_rejected(self):
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_no_unevidenced_behavioral_claims(
                ["投資家心理が悪化した可能性がある。"]
            )

    def test_numbers_without_a_hedge_are_still_rejected(self):
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_no_unevidenced_behavioral_claims(
                ["実績は計画を12%下回り、投資家心理は悪化している。"]
            )

    def test_a_hedge_paired_with_actual_versus_planned_numbers_passes(self):
        check_no_unevidenced_behavioral_claims(
            ["実績が計画を12%下回っており、投資家心理が悪化した可能性がある。"]
        )

    def test_text_without_any_behavioral_keyword_passes(self):
        check_no_unevidenced_behavioral_claims(["売上高は前年同期比で8%増加した。"])


class TestCombinedDisplayCheck:
    def test_it_applies_both_checks(self):
        with pytest.raises(ForbiddenLanguageError, match="forbidden imperative"):
            check_display_texts(["安定推移。", "今すぐ買うべき局面。"])
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_display_texts(["安定推移。", "パニック売りが出ている。"])

    def test_a_generator_argument_is_consumed_only_once(self):
        # Both checks must see every text even though the caller passed a
        # one-shot iterator (the ingest path builds texts lazily).
        texts = (text for text in ["安定推移。", "投資家心理は不明。"])
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_display_texts(texts)
