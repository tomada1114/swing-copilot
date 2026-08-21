"""Tests for settings/secrets loading and feature-gated secret validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from swing_copilot import config as config_module
from swing_copilot.analysis import filing_selection
from swing_copilot.analysis.filing_selection import MIN_FILING_CHARS
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
        assert settings.notification.enabled is True

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

    def test_non_utf8_file_raises_config_error(self, tmp_path):
        # A settings file saved in another encoding must fail as a config
        # problem, not as a bare `UnicodeDecodeError` from deep inside the
        # loader -- `copilot-daily` maps `ConfigError` to a fatal message.
        bad = tmp_path / "settings.yaml"
        bad.write_bytes("universe:\n  index: 日本\n".encode("shift_jis"))
        with pytest.raises(ConfigError, match="Settings file could not be read"):
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

    @pytest.mark.parametrize(
        "component", ["pivot_proximity", "rs_percentile", "criteria_met"]
    )
    def test_strategy_specific_components_default_to_zero_in_every_shipped_strategy(
        self, component
    ):
        # Issue #251 stage 1: the mechanism ships disabled, so no shipped
        # strategy's ranking output moves.
        strategies = load_strategies("config/strategies.yaml")

        for spec in strategies.strategies.values():
            assert getattr(spec.ranking.score_weights, component) == pytest.approx(0.0)

    def test_a_strategy_specific_component_counts_toward_the_sum_to_one_requirement(
        self, tmp_path
    ):
        # Issue #251: the sum check enumerates `ScoreWeights.model_fields`, so
        # a component added later cannot escape it by omission.
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [vcp_breakout]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.5\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            "        pivot_proximity: 0.1\n"
        )

        with pytest.raises(ConfigError, match="default") as exc_info:
            load_strategies(str(bad))
        assert "1.1" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("component", "required_signal"),
        [
            ("pivot_proximity", "vcp_breakout"),
            ("rs_percentile", "minervini_stage2"),
            ("criteria_met", "minervini_stage2"),
        ],
    )
    def test_a_weighted_component_without_its_signal_is_rejected(
        self, tmp_path, component, required_signal
    ):
        # Issue #251: without the signal the metric never exists, so every
        # candidate would score a constant 0.0 there and the components that
        # do work would silently lose that share of the weight.
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.4\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            f"        {component}: 0.1\n"
        )

        with pytest.raises(ConfigError, match=component) as exc_info:
            load_strategies(str(bad))
        assert required_signal in str(exc_info.value)

    @pytest.mark.parametrize(
        ("component", "required_signal"),
        [
            ("pivot_proximity", "vcp_breakout"),
            ("rs_percentile", "minervini_stage2"),
            ("criteria_met", "minervini_stage2"),
        ],
    )
    def test_a_weighted_component_with_its_signal_is_accepted(
        self, tmp_path, component, required_signal
    ):
        good = tmp_path / "strategies.yaml"
        good.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            f"    signals_all: [{required_signal}]\n"
            "    candidate_limit: 10\n"
            "    ranking:\n"
            "      score_weights:\n"
            "        rsi_pullback: 0.4\n"
            "        trend_quality: 0.3\n"
            "        liquidity: 0.2\n"
            f"        {component}: 0.1\n"
        )

        weights = load_strategies(str(good)).strategies["default"].ranking.score_weights

        assert getattr(weights, component) == pytest.approx(0.1)

    def test_a_zero_weighted_component_needs_no_signal(self, tmp_path):
        # The guard keys off a weight greater than zero: an explicit 0.0 is
        # exactly the shipped default and must stay loadable everywhere.
        good = tmp_path / "strategies.yaml"
        good.write_text(
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
            "        pivot_proximity: 0.0\n"
        )

        weights = load_strategies(str(good)).strategies["default"].ranking.score_weights

        assert weights.pivot_proximity == pytest.approx(0.0)

    def test_non_utf8_file_raises_config_error(self, tmp_path):
        bad = tmp_path / "strategies.yaml"
        bad.write_bytes("strategies:\n  default: 日本\n".encode("shift_jis"))
        with pytest.raises(ConfigError, match="Strategies file could not be read"):
            load_strategies(str(bad))


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
        # Issue #297: `chase_pivot_pct` is now also `pivot_proximity`'s decay
        # width, which `screening/pipeline.py` divides by. `0.0` used to be
        # accepted (`ge=0.0`); it must be rejected here, because that is the
        # only thing standing between a settings edit and a ZeroDivisionError
        # in the ranking loop.
        pytest.param(
            {"technical_signals": {"vcp": {"chase_pivot_pct": 0.0}}},
            id="chase-pivot-pct-zero",
        ),
        pytest.param(
            {"technical_signals": {"vcp": {"chase_pivot_pct": -0.01}}},
            id="chase-pivot-pct-negative",
        ),
        pytest.param(
            {"technical_signals": {"vcp": {"chase_pivot_pct": 1.01}}},
            id="chase-pivot-pct-over-one",
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


def test_entry_limit_atr_multiple_defaults_to_zero():
    settings = load_settings("config/settings.yaml")
    assert settings.backtest.entry_limit_atr_multiple == 0.0


def test_entry_limit_atr_multiple_rejects_negative_values():
    with pytest.raises(ValidationError, match="entry_limit_atr_multiple"):
        Settings.model_validate({"backtest": {"entry_limit_atr_multiple": -0.1}})


def test_earnings_guard_thresholds_default_to_two_and_five_business_days():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.earnings_block_business_days == 2
    assert settings.risk.earnings_warn_business_days == 5


def test_earnings_lookahead_days_defaults_to_forty_five_calendar_days():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.earnings_lookahead_days == 45


def test_earnings_lookahead_days_rejects_zero_or_negative():
    with pytest.raises(ValidationError, match="earnings_lookahead_days"):
        Settings.model_validate({"risk": {"earnings_lookahead_days": 0}})


def test_circuit_breaker_thresholds_have_documented_defaults():
    settings = load_settings("config/settings.yaml")
    assert settings.risk.circuit_daily_loss_pct == 2.0
    assert settings.risk.circuit_weekly_loss_pct == 5.0
    assert settings.risk.circuit_monthly_loss_pct == 8.0
    assert settings.risk.circuit_consecutive_losses == 2
    assert settings.risk.circuit_cooldown_hours == 24


def test_dd_level_thresholds_default_to_previous_hardcoded_constants():
    # `dd_high_d25/d15/d5` and `dd_caution_d25` still reproduce the module
    # constants that used to live in `regime/distribution.py` (`_HIGH_D25=5`,
    # `_HIGH_D15=3`, `_HIGH_D5=2`, `_CAUTION_D25=3`). `dd_severe_d25`/
    # `dd_severe_d15` were changed from the original `_SEVERE_D25=6`/
    # `_SEVERE_D15=4` on 2026-08-07 (Issue #111; see
    # `reports/regime/2026-08-06-dd-threshold-review.md` §10).
    settings = load_settings("config/settings.yaml")
    assert settings.regime.dd_severe_d25 == 7
    assert settings.regime.dd_severe_d15 == 6
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
        pytest.param(
            {
                "technical_signals": {
                    "vcp": {"min_contractions": 3, "max_contractions": 2}
                }
            },
            id="vcp-max-contractions-below-min",
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
    assert settings.analysis.sufficient_news_mention_items == 5


def test_analysis_config_max_filing_chars_is_directly_configurable():
    # Consolidated from the old `filing_chunk_chars * max_filing_chunks`
    # product into a single setting; the default (120_000) preserves the
    # previous 30_000 * 4 product unchanged.
    settings = Settings.model_validate(
        {
            "analysis": {
                "max_filing_chars": 3_000,
                "max_filing_chars_per_symbol": 9_000,
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


def test_config_validates_against_the_selectors_own_minimum():
    # Issue #268: the bound `config.py` enforces and the reservation
    # `filing_selection.py` actually hands out have to be the *same* number, so
    # the validation is the one imported constant rather than a copy that can
    # drift from it. Asserting *identity* -- not equality of two literals --
    # is what makes a second definition impossible to add unnoticed: a copy
    # declared in `config.py` would be an equal but distinct int object.
    # Read out of the module namespace rather than as an attribute because
    # mypy's `no_implicit_reexport` rejects reading an imported name off
    # another module, which is the same rule that keeps this one-way.
    assert vars(config_module)["MIN_FILING_CHARS"] is filing_selection.MIN_FILING_CHARS


def test_shipped_settings_cover_every_filing_minimum():
    # Issue #268's "existing valid configuration still loads" guard, stated as
    # the invariant rather than as three literals: the shipped per-symbol
    # ceiling has to cover one reservation per collected filing.
    analysis = load_settings("config/settings.yaml").analysis
    assert (
        analysis.max_filing_chars_per_symbol
        >= analysis.max_filings_per_symbol
        * min(analysis.max_filing_chars, MIN_FILING_CHARS)
    )


def test_analysis_config_rejects_symbol_budget_below_the_filing_minimums():
    # Issue #268: `max_filing_chars_per_symbol >= max_filing_chars` alone let a
    # per-symbol ceiling through that cannot seat one `MIN_FILING_CHARS`
    # reservation per collected filing, so every filing past the first exported
    # as `omitted_symbol_budget` on every run. Rejected at load time, before any
    # EDGAR call.
    with pytest.raises(ValidationError, match="max_filings_per_symbol"):
        Settings.model_validate(
            {
                "analysis": {
                    "max_filing_chars": 10_000,
                    "max_filing_chars_per_symbol": 3 * MIN_FILING_CHARS - 1,
                    "max_filings_per_symbol": 3,
                }
            }
        )


def test_analysis_config_accepts_exactly_the_filing_minimums():
    # The boundary itself is valid: every filing seats its reservation, with
    # nothing left over for any of them to grow into.
    settings = Settings.model_validate(
        {
            "analysis": {
                "max_filing_chars": 10_000,
                "max_filing_chars_per_symbol": 3 * MIN_FILING_CHARS,
                "max_filings_per_symbol": 3,
            }
        }
    )
    assert settings.analysis.max_filing_chars_per_symbol == 3 * MIN_FILING_CHARS


@pytest.mark.parametrize(
    ("per_symbol_chars", "is_valid"),
    [(5_999, False), (6_000, True)],
)
def test_analysis_config_floors_the_minimum_at_the_per_filing_ceiling(
    per_symbol_chars, is_valid
):
    # A per-filing ceiling below `MIN_FILING_CHARS` caps what a filing can
    # reserve, exactly as `_reserve_minimum_chars` does, so the required total
    # is 3 * 2_000 and not 3 * MIN_FILING_CHARS -- otherwise a small but
    # perfectly coherent budget would be rejected.
    overrides = {
        "analysis": {
            "max_filing_chars": 2_000,
            "max_filing_chars_per_symbol": per_symbol_chars,
            "max_filings_per_symbol": 3,
        }
    }
    if not is_valid:
        with pytest.raises(ValidationError, match="max_filings_per_symbol"):
            Settings.model_validate(overrides)
        return
    settings = Settings.model_validate(overrides)
    assert settings.analysis.max_filing_chars_per_symbol == per_symbol_chars


def test_analysis_config_error_names_the_required_minimum_and_the_actual_value():
    # A rejection has to say what to change it to, not just that it is wrong.
    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate(
            {
                "analysis": {
                    "max_filing_chars": 10_000,
                    "max_filing_chars_per_symbol": 20_000,
                    "max_filings_per_symbol": 3,
                }
            }
        )
    message = str(excinfo.value)
    assert "20000" in message
    assert str(3 * MIN_FILING_CHARS) in message


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
    """P8-31 (E31.1): the retrospective's own single setting, and only that."""

    def test_has_documented_defaults(self):
        settings = load_settings("config/settings.yaml")
        assert settings.retro.max_surprises == 5

    def test_max_surprises_is_configurable(self):
        settings = Settings.model_validate({"retro": {"max_surprises": 3}})
        assert settings.retro.max_surprises == 3

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"max_surprises": 0}, id="max-surprises-below-one"),
            pytest.param(
                {"max_surprises": "sometimes"}, id="max-surprises-out-of-domain"
            ),
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

    def test_rejects_the_removed_approval_mode_reservation(self):
        # Issue #178: `approval_mode` was accepted but never read, so `manual`
        # silently kept the auto-apply behaviour. It is gone, and a settings
        # file that still carries it must fail loudly instead of being ignored.
        for value in ("auto", "manual"):
            with pytest.raises(ValidationError):
                Settings.model_validate({"retro": {"approval_mode": value}})


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


def test_analysis_config_rejects_a_non_positive_news_mention_floor():
    # Issue #191: a floor of 0 would grade every feed `sufficient`, silently
    # undoing Issue #130's declaration rather than loosening it visibly.
    with pytest.raises(ValidationError):
        Settings.model_validate({"analysis": {"sufficient_news_mention_items": 0}})
