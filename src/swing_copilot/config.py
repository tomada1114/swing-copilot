"""Load `config/settings.yaml`, `config/strategies.yaml`, and environment secrets.

Feature-gated secret validation (`require_secrets`) lets the configuration
load unconditionally offline while still failing fast when a feature that
needs a secret is actually enabled (`docs/04_detailed_design.md` 2.1 #6).
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
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

    @field_validator(
        "anthropic_api_key",
        "finnhub_api_key",
        "fred_api_key",
        "discord_webhook_url",
        "edgar_identity",
        "eodhd_api_key",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Treat a declared-but-empty `.env` entry (`KEY=`) as unset.

        `.env.example`-style files often ship with keys present but blank;
        python-dotenv reads that as `""`, not absent, so without this the
        value would be silently treated as "configured".
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    # Stop distance as a % of entry price above which a WIDE_STOP sizing
    # warning is raised (roadmap §5 P1-03, 要検証).
    wide_stop_threshold_pct: float = 10.0


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
    # Multiplier applied to slippage_pct on both entry and exit (incl. forced
    # liquidation), roadmap §5 P2-09. 1.0 == no change from the base
    # slippage_pct; --pessimistic overrides it with pessimistic_slippage_multiplier.
    slippage_multiplier: float = 1.0
    # Pessimistic-scenario preset (roadmap §5 P2-09, 要検証: median of
    # backtest-expert's cited 1.5-2.0x range).
    pessimistic_slippage_multiplier: float = 1.75
    # P2-10 sensitivity grid: best cell's expectancy_per_trade strictly above
    # this multiple of its (non-gray) neighbors' median triggers a "spike"
    # (overfitting suspicion) verdict (roadmap §5 P2-10, 要検証).
    sensitivity_spike_multiplier: float = 1.5
    # P2-10 sensitivity grid: fraction (e.g. 0.20 == 20%) around the best
    # cell's value within which every non-gray cell must fall for a
    # "plateau" (robust) verdict (roadmap §5 P2-10, 要検証; basis point is the
    # best cell's own value -- not specified in the seed, fixed here).
    sensitivity_plateau_tolerance_pct: float = 0.20
    # trade_count below this draws a "statistically insufficient" warning;
    # below preliminary_trade_count_threshold (but >= this) draws a
    # "preliminary" warning (roadmap §5 P2-07, out: backtest-expert).
    insufficient_trade_count_threshold: int = 30
    preliminary_trade_count_threshold: int = 100
    # win_rate (fraction, e.g. 0.90 == 90%) strictly above this, or
    # max_drawdown_pct strictly below lookahead_suspicion_max_drawdown, draws
    # a look-ahead-bias suspicion warning (roadmap §5 P2-07). The win_rate
    # bound is from the roadmap; the drawdown bound has no seed value and is
    # fixed here as 要検証 per Issue #16's boundary note.
    lookahead_suspicion_win_rate: float = 0.90
    lookahead_suspicion_max_drawdown: float = 0.01


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


_SCORE_WEIGHT_SUM_TOLERANCE = 1e-9


class ScoreWeights(_StrictModel):
    """Component weights for the composite ranking score (P1-01, roadmap §5).

    Defaults are unvalidated (要検証); P2-10's sensitivity grid is the
    intended follow-up to ground them empirically.
    """

    rsi_pullback: float = Field(default=0.5, ge=0.0)
    trend_quality: float = Field(default=0.3, ge=0.0)
    liquidity: float = Field(default=0.2, ge=0.0)


class RankingConfig(_StrictModel):
    """`ranking.*` in one `strategies.yaml` strategy entry."""

    score_weights: ScoreWeights = ScoreWeights()


class StrategySpec(_StrictModel):
    """Validated composition and ranking rules for one screening strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters_all: tuple[str, ...]
    signals_all: tuple[str, ...]
    candidate_limit: int = Field(gt=0, le=10)
    ranking: RankingConfig = RankingConfig()


class StrategiesConfig(_StrictModel):
    """Typed contents of `config/strategies.yaml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategies: dict[str, StrategySpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_score_weights_sum_to_one(self) -> StrategiesConfig:
        for key, spec in self.strategies.items():
            weights = spec.ranking.score_weights
            total = weights.rsi_pullback + weights.trend_quality + weights.liquidity
            if not math.isclose(total, 1.0, abs_tol=_SCORE_WEIGHT_SUM_TOLERANCE):
                msg = (
                    f"strategy '{key}': ranking.score_weights must sum to "
                    f"1.0, got {total}"
                )
                raise ValueError(msg)
        return self


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


def load_strategies(path: str = "config/strategies.yaml") -> StrategiesConfig:
    """Load and validate named screening strategies.

    Args:
        path: Path to the strategies YAML file.

    Returns:
        Typed strategy specifications with bounded candidate counts and
        composite-ranking score weights that sum to 1.0 (P1-01).

    Raises:
        ConfigError: The file is missing, malformed, or violates the schema.
    """
    strategies_path = Path(path)
    if not strategies_path.is_file():
        msg = f"Strategies file not found: {strategies_path}"
        raise ConfigError(msg)

    try:
        raw = yaml.safe_load(strategies_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"Strategies file is not valid YAML: {strategies_path}"
        raise ConfigError(msg) from exc

    try:
        return StrategiesConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"Strategies file failed validation: {strategies_path}\n{exc}"
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
