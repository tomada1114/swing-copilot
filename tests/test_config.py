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
        assert settings.llm.models.news_summary == "claude-haiku-4-5-20251001"
        assert settings.llm.models.filing_analysis == "claude-haiku-4-5-20251001"
        assert settings.notification.enabled is False
        assert settings.report.auto_open is True

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
            "    ranking: [rsi14_asc, avg_volume_desc, symbol_asc]\n"
        )

        with pytest.raises(ConfigError, match="failed validation"):
            load_strategies(str(bad))

    def test_rejects_nondeterministic_ranking(self, tmp_path):
        bad = tmp_path / "strategies.yaml"
        bad.write_text(
            "strategies:\n"
            "  default:\n"
            "    filters_all: []\n"
            "    signals_all: [trend_sma]\n"
            "    candidate_limit: 10\n"
            "    ranking: [avg_volume_desc]\n"
        )

        with pytest.raises(ConfigError, match="failed validation"):
            load_strategies(str(bad))


class TestLoadSecrets:
    def test_loads_without_dotenv_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for key in (
            "ANTHROPIC_API_KEY",
            "FINNHUB_API_KEY",
            "FRED_API_KEY",
            "DISCORD_WEBHOOK_URL",
            "EDGAR_IDENTITY",
            "EODHD_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        secrets = load_secrets()

        assert isinstance(secrets, Secrets)
        assert secrets.anthropic_api_key is None
        assert secrets.finnhub_api_key is None

    def test_reads_environment_variables(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        secrets = load_secrets()
        assert secrets.anthropic_api_key == "sk-test-123"


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
        with pytest.raises(ConfigError, match="anthropic_api_key"):
            require_secrets(_isolated_secrets(), features={"llm"})

    def test_present_feature_secret_passes(self):
        secrets = _isolated_secrets(anthropic_api_key="sk-test")
        require_secrets(secrets, features={"llm"})

    def test_multiple_missing_secrets_all_reported(self):
        with pytest.raises(ConfigError) as exc_info:
            require_secrets(_isolated_secrets(), features={"llm", "finnhub", "fred"})
        message = str(exc_info.value)
        assert "anthropic_api_key" in message
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
        assert secrets.anthropic_api_key is None
        assert secrets.finnhub_api_key is None
        assert secrets.fred_api_key is None
        assert secrets.discord_webhook_url is None
        assert secrets.edgar_identity is None
        assert secrets.eodhd_api_key is None

    def test_blank_env_value_is_treated_as_unset(self):
        secrets = _isolated_secrets(anthropic_api_key="   ")
        assert secrets.anthropic_api_key is None


def test_settings_rejects_wrong_type_for_nested_field():
    with pytest.raises(ValidationError):
        Settings.model_validate({"technical_signals": {"trend": {"sma_short": "fast"}}})
