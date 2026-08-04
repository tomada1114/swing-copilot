"""Tests for settings/secrets loading and feature-gated secret validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from swing_copilot.config import (
    Secrets,
    Settings,
    StrategiesConfig,
    load_secrets,
    load_settings,
    load_strategies,
    require_secrets,
)
from swing_copilot.exceptions import ConfigError


class TestLoadSettings:
    def test_loads_default_settings_yaml(self):
        settings = load_settings("config/settings.yaml")
        assert isinstance(settings, Settings)
        assert settings.universe.index == "sp500"
        assert settings.risk.max_position_pct == pytest.approx(0.10)
        assert settings.notification.enabled is False

    def test_missing_file_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_settings(str(tmp_path / "does-not-exist.yaml"))

    def test_invalid_schema_raises_config_error(self, tmp_path):
        bad = tmp_path / "settings.yaml"
        bad.write_text('universe:\n  refresh_interval_days: "not-a-number"\n')
        with pytest.raises(ConfigError):
            load_settings(str(bad))

    def test_malformed_yaml_raises_config_error(self, tmp_path):
        bad = tmp_path / "settings.yaml"
        bad.write_text("universe: [unterminated\n")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load_settings(str(bad))

    def test_unknown_top_level_field_is_rejected(self, tmp_path):
        valid_yaml = Path("config/settings.yaml").read_text(encoding="utf-8")
        bad = tmp_path / "settings.yaml"
        bad.write_text(valid_yaml + "\nbogus_top_level_field: 1\n")
        with pytest.raises(ConfigError):
            load_settings(str(bad))


class TestLoadStrategies:
    def test_loads_typed_default_strategy(self):
        strategies = load_strategies("config/strategies.yaml")

        assert isinstance(strategies, StrategiesConfig)
        default = strategies.strategies["default"]
        assert default.filters_all == (
            "profitable_positive_fcf_equity",
            "volume_min",
        )
        assert default.candidate_limit == 10

    def test_rejects_invalid_candidate_limit(self, tmp_path):
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 0\n"
        )

        with pytest.raises(ConfigError, match="failed validation"):
            load_strategies(str(bad))

    def test_default_score_weights_sum_to_one(self):
        strategies = load_strategies("config/strategies.yaml")

        weights = strategies.strategies["default"].ranking.score_weights
        assert weights.rsi_pullback == pytest.approx(0.5)
        assert weights.trend_quality == pytest.approx(0.3)
        assert weights.liquidity == pytest.approx(0.2)

    def test_rejects_score_weights_not_summing_to_one(self, tmp_path):
        # REQ-020: sum != 1.0 must fail fast, naming the strategy and the sum.
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.5\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.3\n"
        )

        with pytest.raises(ConfigError, match="default") as exc_info:
            load_strategies(str(bad))
        assert "1.1" in str(exc_info.value)

    def test_rejects_negative_score_weight(self, tmp_path):
        # REQ-021
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: -0.1\n"
            "        trend_quality: 0.9\n"
            "        liquidity: 0.2\n"
        )

        with pytest.raises(ConfigError, match="failed validation"):
            load_strategies(str(bad))

    def test_rejects_unknown_score_weight_key(self, tmp_path):
        # REQ-022
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.5\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.1\n"
            "        foo: 0.1\n"
        )

        with pytest.raises(ConfigError, match="foo"):
            load_strategies(str(bad))

    def test_custom_score_weights_summing_to_one_are_accepted(self, tmp_path):
        # REQ-030
        good = tmp_path / "strategies.yaml"
        good.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.6\n"
            "        trend_quality: 0.2\n"
            "        liquidity: 0.2\n"
        )

        strategies = load_strategies(str(good))

        weights = strategies.strategies["default"].ranking.score_weights
        assert weights.rsi_pullback == pytest.approx(0.6)
        assert weights.trend_quality == pytest.approx(0.2)
        assert weights.liquidity == pytest.approx(0.2)

    def test_atr_pct_defaults_to_zero_so_shipped_rankings_are_unchanged(self):
        strategies = load_strategies("config/strategies.yaml")

        for spec in strategies.strategies.values():
            assert spec.ranking.score_weights.atr_pct == pytest.approx(0.0)

    def test_atr_pct_counts_toward_the_sum_to_one_requirement(self, tmp_path):
        # The other three weights already sum to 1.0, so adding atr_pct must
        # be rejected rather than silently accepted alongside them.
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.5\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            "        atr_pct: 0.2\n"
        )

        with pytest.raises(ConfigError, match="default") as exc_info:
            load_strategies(str(bad))
        assert "1.2" in str(exc_info.value)

    def test_score_weights_including_atr_pct_that_sum_to_one_are_accepted(
        self, tmp_path
    ):
        good = tmp_path / "strategies.yaml"
        good.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.3\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            "        atr_pct: 0.2\n"
        )

        weights = load_strategies(str(good)).strategies["default"].ranking.score_weights

        assert weights.atr_pct == pytest.approx(0.2)


class TestLoadSecrets:
    def test_loads_without_dotenv_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for key in (
            "FINNHUB_API_KEY",
            "FRED_API_KEY",
            "DISCORD_WEBHOOK_URL",
            "EDGAR_IDENTITY",
            "EODHD_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        secrets = load_secrets()

        assert isinstance(secrets, Secrets)
        assert secrets.finnhub_api_key is None

    def test_reads_environment_variables(self, monkeypatch):
        monkeypatch.setenv("FINNHUB_API_KEY", "finnhub-test-123")
        secrets = load_secrets()
        assert secrets.finnhub_api_key == "finnhub-test-123"


def _isolated_secrets(**overrides: str) -> Secrets:
    """Build Secrets isolated from any real `.env` a developer has locally.

    `env_file=".env"` reads whatever `.env` exists in the repo root a test
    happens to run from; passing `_env_file=None` overrides that for a
    single instantiation so these tests never depend on (or race with) a
    developer's real, in-progress `.env` file.
    """
    return Secrets(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestRequireSecrets:
    def test_no_features_never_raises(self):
        require_secrets(_isolated_secrets(), features=set())

    def test_missing_feature_secret_raises_config_error(self):
        with pytest.raises(ConfigError, match="finnhub_api_key"):
            require_secrets(_isolated_secrets(), features={"finnhub"})

    def test_present_feature_secret_passes(self):
        secrets = _isolated_secrets(finnhub_api_key="finnhub-test")
        require_secrets(secrets, features={"finnhub"})

    def test_multiple_missing_secrets_all_reported(self):
        with pytest.raises(ConfigError) as exc_info:
            require_secrets(_isolated_secrets(), features={"edgar", "finnhub", "fred"})
        message = str(exc_info.value)
        assert "edgar_identity" in message
        assert "finnhub_api_key" in message
        assert "fred_api_key" in message

    def test_unknown_feature_raises_config_error(self):
        with pytest.raises(ConfigError, match="unknown feature"):
            require_secrets(_isolated_secrets(), features={"not_a_real_feature"})


class TestSecretsModel:
    def test_extra_env_vars_are_ignored(self, monkeypatch):
        monkeypatch.setenv("SOME_UNRELATED_ENV_VAR", "value")
        _isolated_secrets()

    def test_all_fields_default_to_none(self):
        secrets = _isolated_secrets()
        assert secrets.finnhub_api_key is None
        assert secrets.fred_api_key is None
        assert secrets.discord_webhook_url is None
        assert secrets.edgar_identity is None
        assert secrets.eodhd_api_key is None

    def test_blank_env_value_is_treated_as_unset(self):
        secrets = _isolated_secrets(finnhub_api_key="   ")
        assert secrets.finnhub_api_key is None


def test_settings_rejects_wrong_type_for_nested_field():
    with pytest.raises(ValidationError):
        Settings.model_validate({"technical_signals": {"trend": {"sma_short": "fast"}}})


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"universe": {"refresh_interval_days": 0}}, id="refresh-zero"),
        pytest.param(
            {"universe": {"refresh_interval_days": -1}}, id="refresh-negative"
        ),
        pytest.param(
            {"fundamental_filters": {"min_profitable_quarters": 0}},
            id="profitable-quarters-zero",
        ),
        pytest.param(
            {"technical_signals": {"trend": {"sma_short": 0}}},
            id="sma-short-zero",
        ),
        pytest.param(
            {"technical_signals": {"trend": {"sma_long": -1}}},
            id="sma-long-negative",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"rsi_period": 0}}},
            id="rsi-period-zero",
        ),
        pytest.param(
            {"technical_signals": {"volume": {"avg_volume_days": 0}}},
            id="volume-window-zero",
        ),
        pytest.param(
            {"technical_signals": {"volume": {"min_avg_volume": -1}}},
            id="minimum-volume-negative",
        ),
        pytest.param({"schedule": {"timeout_minutes": 0}}, id="timeout-zero"),
        pytest.param({"schedule": {"timeout_minutes": -1}}, id="timeout-negative"),
    ],
)
def test_settings_rejects_non_positive_periods_counts_and_timeout(overrides):
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"fundamental_filters": {"min_equity_ratio": -0.01}},
            id="equity-ratio-negative",
        ),
        pytest.param(
            {"fundamental_filters": {"min_equity_ratio": 1.01}},
            id="equity-ratio-over-one",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"rsi_threshold": -0.01}}},
            id="rsi-threshold-negative",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"rsi_threshold": 100.01}}},
            id="rsi-threshold-over-one-hundred",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"sma_band_pct": -0.01}}},
            id="sma-band-negative",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"sma_band_pct": 1.01}}},
            id="sma-band-over-one",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"band_atr_multiple": 0.0}}},
            id="band-atr-multiple-zero",
        ),
        pytest.param(
            {"technical_signals": {"pullback": {"band_atr_multiple": -1.0}}},
            id="band-atr-multiple-negative",
        ),
    ],
)
def test_settings_rejects_out_of_range_screening_ratios_and_thresholds(overrides):
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


@pytest.mark.parametrize(
    ("short_window", "long_window"),
    [
        pytest.param(50, 50, id="equal"),
        pytest.param(200, 50, id="reversed"),
    ],
)
def test_settings_requires_short_sma_to_precede_long_sma(short_window, long_window):
    with pytest.raises(ValidationError, match="sma_short must be < sma_long"):
        Settings.model_validate(
            {
                "technical_signals": {
                    "trend": {
                        "sma_short": short_window,
                        "sma_long": long_window,
                    }
                }
            }
        )


def test_settings_rejects_coercible_values_and_unknown_nested_keys():
    with pytest.raises(ValidationError, match=r"technical_signals\.trend\.sma_short"):
        Settings.model_validate({"technical_signals": {"trend": {"sma_short": 50.0}}})
    with pytest.raises(ValidationError, match=r"risk\.max_position_pct"):
        Settings.model_validate({"risk": {"max_position_pct": "0.1"}})
    with pytest.raises(ValidationError, match=r"risk\.not_a_setting"):
        Settings.model_validate({"risk": {"not_a_setting": 1}})


def test_portfolio_heat_limit_defaults_to_six_percent():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.max_portfolio_heat_pct == 6.0


def test_earnings_guard_thresholds_default_to_two_and_five_business_days():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.earnings_block_business_days == 2
    assert settings.risk.earnings_warn_business_days == 5


def test_circuit_breaker_thresholds_have_documented_defaults():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.circuit_daily_loss_pct == 2.0
    assert settings.risk.circuit_weekly_loss_pct == 5.0
    assert settings.risk.circuit_monthly_loss_pct == 8.0
    assert settings.risk.circuit_consecutive_losses == 2
    assert settings.risk.circuit_cooldown_hours == 24


def test_dd_level_thresholds_default_to_previous_hardcoded_constants():
    # These defaults must reproduce the module constants that used to live in
    # `regime/distribution.py` (`_SEVERE_D25=6`, `_SEVERE_D15=4`, `_HIGH_D25=5`,
    # `_HIGH_D15=3`, `_HIGH_D5=2`, `_CAUTION_D25=3`) so behavior is unchanged.
    settings = load_settings("config/settings.yaml")
    assert settings.regime.dd_severe_d25 == 6
    assert settings.regime.dd_severe_d15 == 4
    assert settings.regime.dd_high_d25 == 5
    assert settings.regime.dd_high_d15 == 3
    assert settings.regime.dd_high_d5 == 2
    assert settings.regime.dd_caution_d25 == 3


def test_earnings_warn_threshold_cannot_be_below_block_threshold():
    with pytest.raises(ValidationError, match="earnings_warn_business_days"):
        Settings.model_validate(
            {
                "risk": {
                    "earnings_block_business_days": 5,
                    "earnings_warn_business_days": 2,
                }
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"risk": {"max_position_pct": 1.1}}, id="position-fraction"),
        pytest.param({"backtest": {"commission_pct": -0.01}}, id="commission"),
        pytest.param(
            {
                "backtest": {
                    "insufficient_trade_count_threshold": 100,
                    "preliminary_trade_count_threshold": 30,
                }
            },
            id="trade-count-order",
        ),
        pytest.param(
            {"postmortem": {"horizon_5d_weight": 0.8}},
            id="postmortem-weight-sum",
        ),
        pytest.param(
            {"regime": {"bull_vix_max": 35.0, "bear_vix_min": 30.0}},
            id="vix-order",
        ),
        pytest.param(
            {"regime": {"dd_severe_d25": 5, "dd_high_d25": 5}},
            id="dd-d25-severe-not-greater-than-high",
        ),
        pytest.param(
            {"regime": {"dd_high_d25": 3, "dd_caution_d25": 3}},
            id="dd-d25-high-not-greater-than-caution",
        ),
        pytest.param(
            {"regime": {"dd_severe_d15": 3, "dd_high_d15": 3}},
            id="dd-d15-severe-not-greater-than-high",
        ),
        pytest.param(
            {"regime": {"dd_severe_d25": 0}},
            id="dd-threshold-below-one",
        ),
        pytest.param(
            {"technical_signals": {"vcp": {"contraction_ratio_max": 1.1}}},
            id="vcp-ratio",
        ),
    ],
)
def test_settings_rejects_invalid_quantitative_thresholds(overrides):
    with pytest.raises(ValidationError):
        Settings.model_validate(overrides)


def test_analysis_config_has_documented_defaults():
    # roadmap §5 P6-26: `analysis.*` replaced the old `llm.*` cache/cost
    # section with the collection/export bounds handed to the qualitative
    # analysis skill via `analysis_input.json`.
    settings = load_settings("config/settings.yaml")
    assert settings.analysis.max_news_items_per_symbol == 20
    assert settings.analysis.max_news_chars_per_item == 4000
    assert settings.analysis.max_filing_chars == 120_000
    assert settings.analysis.max_filing_chars_per_symbol == 240_000
    assert settings.analysis.filing_lookback_days == 90
    assert settings.analysis.max_filings_per_symbol == 3
    assert settings.analysis.max_calendar_events == 20
    assert settings.analysis.max_calendar_chars_per_item == 2000


def test_analysis_config_max_filing_chars_is_directly_configurable():
    # Consolidated from the old `filing_chunk_chars * max_filing_chunks`
    # product into a single setting; the default (120_000) preserves the
    # previous 30_000 * 4 product unchanged.
    settings = Settings.model_validate(
        {
            "analysis": {
                "max_filing_chars": 3_000,
                "max_filing_chars_per_symbol": 4_000,
            }
        }
    )
    assert settings.analysis.max_filing_chars == 3_000


def test_analysis_config_rejects_symbol_budget_below_per_filing_budget():
    with pytest.raises(ValidationError, match="max_filing_chars_per_symbol"):
        Settings.model_validate(
            {
                "analysis": {
                    "max_filing_chars": 3_000,
                    "max_filing_chars_per_symbol": 2_999,
                }
            }
        )


def test_analysis_config_no_longer_has_the_old_chunk_keys():
    with pytest.raises(ValidationError):
        Settings.model_validate({"analysis": {"filing_chunk_chars": 1_000}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"analysis": {"max_filing_chunks": 3}})


def test_settings_no_longer_has_an_llm_or_budget_section():
    with pytest.raises(ValidationError):
        Settings.model_validate({"llm": {"cache_ttl_days": 5}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"budget": {"monthly_cost_cap_usd": 10.0}})


class TestRetroConfig:
    """P8-31 (E31.1): the retrospective's own two settings, and only those."""

    def test_has_documented_defaults(self):
        settings = load_settings("config/settings.yaml")
        assert settings.retro.max_surprises == 5
        assert settings.retro.approval_mode == "auto"

    def test_max_surprises_is_configurable(self):
        settings = Settings.model_validate({"retro": {"max_surprises": 3}})
        assert settings.retro.max_surprises == 3

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"max_surprises": 0}, id="max-surprises-below-one"),
            pytest.param({"approval_mode": "sometimes"}, id="unknown-approval-mode"),
            pytest.param({"unknown_field": 1}, id="unknown-field"),
        ],
    )
    def test_rejects_invalid_values(self, overrides):
        with pytest.raises(ValidationError):
            Settings.model_validate({"retro": overrides})

    def test_does_not_duplicate_the_postmortem_thresholds(self):
        # D6: verdict evaluation reuses `settings.postmortem`'s window and
        # thresholds rather than growing a second vocabulary for the same
        # quantities, so these must stay unknown fields here.
        for duplicated in ("neutral_threshold_pct", "preliminary_sample_threshold"):
            with pytest.raises(ValidationError):
                Settings.model_validate({"retro": {duplicated: 1.0}})


def test_band_atr_multiple_is_adopted_in_the_repo_settings():
    # 2026-08-04 adoption based on the R2 result in
    # reports/backtests/2026-07-30-strategy-comparison.md.
    settings = load_settings("config/settings.yaml")

    assert settings.technical_signals.pullback.band_atr_multiple == pytest.approx(2.0)


def test_band_atr_multiple_defaults_to_none_when_absent():
    # Absence keeps the legacy percentage band, so older/external settings
    # files keep their behavior.
    settings = Settings.model_validate({})

    assert settings.technical_signals.pullback.band_atr_multiple is None


def test_band_atr_multiple_accepts_a_positive_multiple():
    settings = Settings.model_validate(
        {"technical_signals": {"pullback": {"band_atr_multiple": 2.0}}}
    )

    assert settings.technical_signals.pullback.band_atr_multiple == pytest.approx(2.0)
