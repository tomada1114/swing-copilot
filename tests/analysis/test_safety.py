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

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("ＡＡＰＬを購入してください。", id="full-width-japanese"),
            pytest.param("ＡＡＰＬを購入せよ。", id="japanese-command"),
            pytest.param("ポジションを閉じなさい。", id="japanese-close-command"),
            pytest.param(
                "\uff39\uff2f\uff35\u3000\uff2d\uff35\uff33\uff34\u3000\uff22\uff35\uff39\u3000"
                "\uff21\uff21\uff30\uff2c\u3002",
                id="full-width-english-obligation",
            ),
            pytest.param("Please sell AAPL immediately.", id="english-command"),
            pytest.param("「購入せよ」は禁止表現です。", id="quoted-command"),
            pytest.param("購入すべきではない。", id="negated-obligation"),
        ],
    )
    def test_normalized_commands_and_obligations_are_rejected(self, text):
        with pytest.raises(ForbiddenLanguageError, match="forbidden imperative"):
            check_no_imperative_language([text])


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

    @pytest.mark.parametrize(
        ("text", "missing"),
        [
            pytest.param(
                "売上が12%増えており、投資家心理が悪化した可能性がある。",
                "actual/plan markers",
                id="percentage-only",
            ),
            pytest.param(
                "実績が計画を下回り、投資家心理が悪化した可能性がある。",
                "percentage",
                id="markers-without-a-number",
            ),
            pytest.param(
                "実績は12%下回っており、投資家心理が悪化した可能性がある。",
                "plan marker",
                id="actual-and-percentage-without-plan",
            ),
            pytest.param(
                "計画を12%下回っており、投資家心理が悪化した可能性がある。",
                "actual marker",
                id="plan-and-percentage-without-actual",
            ),
        ],
    )
    def test_partial_evidence_is_still_rejected(self, text, missing):
        # The three evidence signals are conjunctive: any one of them missing
        # leaves the claim unfalsifiable. Weakening the `and` chain in
        # `safety.py` to an `or` must fail here.
        assert missing
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_no_unevidenced_behavioral_claims([text])

    def test_a_hedge_paired_with_actual_versus_planned_numbers_passes(self):
        check_no_unevidenced_behavioral_claims(
            ["実績が計画を12%下回っており、投資家心理が悪化した可能性がある。"]
        )

    def test_an_english_bare_behavioral_claim_is_rejected(self):
        with pytest.raises(ForbiddenLanguageError, match="behavioral/psychological"):
            check_no_unevidenced_behavioral_claims(
                ["Management is anxious about the outlook."]
            )

    def test_an_english_hedge_with_full_evidence_passes(self):
        check_no_unevidenced_behavioral_claims(
            [
                "Actual results missed the planned target by 12%, a possible "
                "shift in investor sentiment.",
            ]
        )

    @pytest.mark.parametrize(
        "hedged",
        [
            pytest.param("投資家心理が悪化した可能性がある。", id="可能性"),
            pytest.param("投資家心理が悪化したと考えられる。", id="考えられる"),
            pytest.param("投資家心理の悪化を示唆しうる。", id="示唆"),
            pytest.param("投資家心理は悪化したと読める。", id="と読める"),
            pytest.param(
                "入力の範囲では投資家心理は悪化している。", id="入力の範囲では"
            ),
        ],
    )
    def test_every_hedge_the_skill_docs_offer_satisfies_the_check(self, hedged):
        # AC12 presents these as interchangeable, so the machine check must
        # accept all of them; otherwise a conventions-following writer gets
        # their symbol withheld for an arbitrary word choice.
        check_no_unevidenced_behavioral_claims(
            [f"実績が計画を12%下回っており、{hedged}"]
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
