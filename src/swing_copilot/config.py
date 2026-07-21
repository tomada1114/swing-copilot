"""Load `config/settings.yaml`, `config/strategies.yaml`, and environment secrets.

Feature-gated secret validation (`require_secrets`) lets the configuration
load unconditionally offline while still failing fast when a feature that
needs a secret is actually enabled (`docs/04_detailed_design.md` 2.1 #6).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from swing_copilot.exceptions import ConfigError

FEATURE_SECRET_ATTRS: dict[str, str] = {
    "llm": "anthropic_api_key",
    "finnhub": "finnhub_api_key",
    "fred": "fred_api_key",
    "discord": "discord_webhook_url",
    "edgar": "edgar_identity",
    "eodhd": "eodhd_api_key",
}


class Secrets(BaseSettings):
    """Secrets loaded from environment variables (and a local `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    finnhub_api_key: str | None = None
    fred_api_key: str | None = None
    discord_webhook_url: str | None = None
    edgar_identity: str | None = None
    eodhd_api_key: str | None = None  # unused until the P4 EODHD provider


class _StrictModel(BaseModel):
    """Base for settings.yaml sections: reject unknown keys (fail fast)."""

    model_config = ConfigDict(extra="forbid")


class UniverseConfig(_StrictModel):
    """`universe.*` in `settings.yaml`."""

    index: str = "sp500"
    refresh_interval_days: int = 7
    snapshot_path: str = "config/universe_snapshot.csv"
    manual_include: list[str] = []
    manual_exclude: list[str] = []


class RiskConfig(_StrictModel):
    """`risk.*` in `settings.yaml`."""

    account_equity_usd: float | None = None
    max_position_pct: float = 0.10
    max_trade_risk_pct: float = 0.01
    max_sector_pct: float = 0.30
    max_correlation: float = 0.7
    correlation_lookback_days: int = 60


class FundamentalFilterConfig(_StrictModel):
    """`fundamental_filters.*` in `settings.yaml`."""

    min_profitable_quarters: int = 4
    require_positive_fcf: bool = True
    min_equity_ratio: float = 0.30


class TrendSignalConfig(_StrictModel):
    """`technical_signals.trend.*` in `settings.yaml`."""

    sma_short: int = 50
    sma_long: int = 200


class PullbackSignalConfig(_StrictModel):
    """`technical_signals.pullback.*` in `settings.yaml`."""

    rsi_period: int = 14
    rsi_threshold: float = 45
    sma_band_pct: float = 0.03


class VolumeFilterConfig(_StrictModel):
    """`technical_signals.volume.*` in `settings.yaml`."""

    avg_volume_days: int = 20
    min_avg_volume: int = 1_000_000


class TechnicalSignalConfig(_StrictModel):
    """`technical_signals.*` in `settings.yaml`."""

    trend: TrendSignalConfig = TrendSignalConfig()
    pullback: PullbackSignalConfig = PullbackSignalConfig()
    volume: VolumeFilterConfig = VolumeFilterConfig()


class BacktestConfig(_StrictModel):
    """`backtest.*` in `settings.yaml`."""

    initial_cash_usd: float = 100_000
    entry: str = "next_open"
    exit_atr_multiple: float = 2.5
    exit_atr_period: int = 14
    max_hold_days: int = 60
    commission_pct: float = 0.001
    slippage_pct: float = 0.001
    benchmark: str = "SPY"


class LLMModelSelection(_StrictModel):
    """`llm.models.*` in `settings.yaml`. Model IDs are never hardcoded elsewhere."""

    news_summary: str = "claude-haiku-4-5-20251001"
    filing_analysis: str = "claude-haiku-4-5-20251001"


class LLMConfig(_StrictModel):
    """`llm.*` in `settings.yaml`."""

    models: LLMModelSelection = LLMModelSelection()
    max_tokens: int = 2048
    schema_version: int = 1
    max_news_items_per_symbol: int = 20
    max_news_chars_per_item: int = 4000
    filing_chunk_chars: int = 30_000
    max_filing_chunks: int = 4


class BudgetConfig(_StrictModel):
    """`budget.*` in `settings.yaml` (NFR-01)."""

    monthly_cap_usd_prototype: float = 5
    monthly_cap_usd_production: float = 25


class ScheduleConfig(_StrictModel):
    """`schedule.*` in `settings.yaml` (NFR-03)."""

    timeout_minutes: int = 35


class NotificationConfig(_StrictModel):
    """`notification.*` in `settings.yaml`. Discord notification is opt-in."""

    enabled: bool = False


class ReportConfig(_StrictModel):
    """`report.*` in `settings.yaml`."""

    auto_open: bool = True


class Settings(_StrictModel):
    """Parsed, validated `config/settings.yaml`."""

    universe: UniverseConfig = UniverseConfig()
    risk: RiskConfig = RiskConfig()
    fundamental_filters: FundamentalFilterConfig = FundamentalFilterConfig()
    technical_signals: TechnicalSignalConfig = TechnicalSignalConfig()
    backtest: BacktestConfig = BacktestConfig()
    llm: LLMConfig = LLMConfig()
    budget: BudgetConfig = BudgetConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    notification: NotificationConfig = NotificationConfig()
    report: ReportConfig = ReportConfig()


def load_settings(path: str = "config/settings.yaml") -> Settings:
    """Load and validate `settings.yaml`.

    Args:
        path: Path to the settings YAML file.

    Returns:
        The validated settings object.

    Raises:
        ConfigError: The file is missing, not valid YAML, or fails schema
            validation (unknown keys, wrong types, missing required values).
    """
    settings_path = Path(path)
    if not settings_path.is_file():
        msg = f"Settings file not found: {settings_path}"
        raise ConfigError(msg)

    try:
        raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"Settings file is not valid YAML: {settings_path}"
        raise ConfigError(msg) from exc

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        msg = f"Settings file failed validation: {settings_path}\n{exc}"
        raise ConfigError(msg) from exc


def load_secrets() -> Secrets:
    """Load secrets from environment variables and a local `.env` if present.

    Returns:
        The secrets object. Missing values are `None`; validate only the
        secrets a given feature actually needs with `require_secrets`.
    """
    return Secrets()


def require_secrets(secrets: Secrets, features: set[str]) -> None:
    """Validate that every secret needed by `features` is present.

    Args:
        secrets: The loaded secrets to check.
        features: Feature keys to validate (see `FEATURE_SECRET_ATTRS`), e.g.
            `{"llm", "finnhub"}`.

    Raises:
        ConfigError: `features` contains an unknown key, or one or more
            required secrets are missing. All missing secrets are reported
            together.
    """
    unknown = features - FEATURE_SECRET_ATTRS.keys()
    if unknown:
        msg = f"require_secrets got unknown feature(s): {sorted(unknown)}"
        raise ConfigError(msg)

    missing = [
        FEATURE_SECRET_ATTRS[feature]
        for feature in sorted(features)
        if getattr(secrets, FEATURE_SECRET_ATTRS[feature]) is None
    ]
    if missing:
        msg = f"Missing required secret(s) for enabled feature(s): {missing}"
        raise ConfigError(msg)
