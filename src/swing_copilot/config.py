"""Load `config/settings.yaml`, `config/strategies.yaml`, and environment secrets.

Feature-gated secret validation (`require_secrets`) lets the configuration
load unconditionally offline while still failing fast when a feature that
needs a secret is actually enabled (`docs/04_detailed_design.md` 2.1 #6).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

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
    "finnhub": "finnhub_api_key",
    "fred": "fred_api_key",
    "discord": "discord_webhook_url",
    "edgar": "edgar_identity",
    "eodhd": "eodhd_api_key",
}


class Secrets(BaseSettings):
    """Secrets loaded from environment variables (and a local `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    finnhub_api_key: str | None = None
    fred_api_key: str | None = None
    discord_webhook_url: str | None = None
    edgar_identity: str | None = None
    eodhd_api_key: str | None = None  # unused until the P4 EODHD provider

    @field_validator(
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

    model_config = ConfigDict(extra="forbid", strict=True)


class UniverseConfig(_StrictModel):
    """`universe.*` in `settings.yaml`."""

    index: str = "sp500"
    refresh_interval_days: int = Field(default=7, ge=1)
    snapshot_path: str = "config/universe_snapshot.csv"
    manual_include: list[str] = []
    manual_exclude: list[str] = []


class RiskConfig(_StrictModel):
    """`risk.*` in `settings.yaml`."""

    account_equity_usd: float | None = Field(default=None, gt=0.0)
    max_position_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    max_trade_risk_pct: float = Field(default=0.01, gt=0.0, le=1.0)
    max_sector_pct: float = Field(default=0.30, gt=0.0, le=1.0)
    max_correlation: float = Field(default=0.7, ge=-1.0, le=1.0)
    correlation_lookback_days: int = Field(default=60, ge=2)
    # Account-level open stop-risk ceiling in percentage points
    # (roadmap §5 P4-17; breakout-trade-planner / Minervini 6-8%帯の
    # 保守側、要検証).
    max_portfolio_heat_pct: float = Field(default=6.0, gt=0.0)
    # Weekday-only earnings proximity thresholds (roadmap §5 P4-18;
    # parabolic-short-trade-planner, 要検証).
    earnings_block_business_days: int = Field(default=2, ge=0)
    earnings_warn_business_days: int = Field(default=5, ge=0)
    # Realized-P&L circuit breaker thresholds in percentage points
    # (roadmap §5 P4-19; all initial values are 要検証).
    circuit_daily_loss_pct: float = Field(default=2.0, gt=0.0)
    circuit_weekly_loss_pct: float = Field(default=5.0, gt=0.0)
    circuit_monthly_loss_pct: float = Field(default=8.0, gt=0.0)
    circuit_consecutive_losses: int = Field(default=2, ge=1)
    circuit_cooldown_hours: int = Field(default=24, ge=1)
    # Stop distance as a % of entry price above which a WIDE_STOP sizing
    # warning is raised (roadmap §5 P1-03, 要検証).
    wide_stop_threshold_pct: float = Field(default=10.0, gt=0.0)

    @model_validator(mode="after")
    def _validate_earnings_threshold_order(self) -> RiskConfig:
        if self.earnings_warn_business_days < self.earnings_block_business_days:
            msg = "earnings_warn_business_days must be >= earnings_block_business_days"
            raise ValueError(msg)
        return self


class FundamentalFilterConfig(_StrictModel):
    """`fundamental_filters.*` in `settings.yaml`."""

    min_profitable_quarters: int = Field(default=4, ge=1)
    require_positive_fcf: bool = True
    min_equity_ratio: float = Field(default=0.30, ge=0.0, le=1.0)


class TrendSignalConfig(_StrictModel):
    """`technical_signals.trend.*` in `settings.yaml`."""

    sma_short: int = Field(default=50, ge=1)
    sma_long: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def _validate_window_order(self) -> TrendSignalConfig:
        if self.sma_short >= self.sma_long:
            msg = "sma_short must be < sma_long"
            raise ValueError(msg)
        return self


class PullbackSignalConfig(_StrictModel):
    """`technical_signals.pullback.*` in `settings.yaml`."""

    rsi_period: int = Field(default=14, ge=1)
    rsi_threshold: float = Field(default=45.0, ge=0.0, le=100.0)
    sma_band_pct: float = Field(default=0.03, ge=0.0, le=1.0)


class VolumeFilterConfig(_StrictModel):
    """`technical_signals.volume.*` in `settings.yaml`."""

    avg_volume_days: int = Field(default=20, ge=1)
    min_avg_volume: int = Field(default=1_000_000, ge=0)


class MinerviniSignalConfig(_StrictModel):
    """`technical_signals.minervini.*` (roadmap §5 P5-21)."""

    # All defaults below are roadmap §5 P5-21 values. The RS threshold and
    # weighting are explicitly 要検証 in that source, so remain configuration.
    sma200_rising_days: int = Field(default=22, ge=1)
    min_low_multiple: float = Field(default=1.25, ge=1.0)
    min_high_multiple: float = Field(default=0.75, gt=0.0, le=1.0)
    min_rs_percentile: float = Field(default=70.0, ge=0.0, le=100.0)
    rs_weight_63d: float = Field(default=0.40, ge=0.0)
    rs_weight_126d: float = Field(default=0.20, ge=0.0)
    rs_weight_189d: float = Field(default=0.20, ge=0.0)
    rs_weight_252d: float = Field(default=0.20, ge=0.0)

    @model_validator(mode="after")
    def _validate_rs_weights(self) -> MinerviniSignalConfig:
        weights = (
            self.rs_weight_63d,
            self.rs_weight_126d,
            self.rs_weight_189d,
            self.rs_weight_252d,
        )
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            msg = "minervini RS weights must sum to 1.0"
            raise ValueError(msg)
        return self


class ExecutionStateConfig(_StrictModel):
    """P5-23 ATR-normalized entry-state thresholds (roadmap §5, 要検証)."""

    damaged_max_d: float = -3.0
    fair_max_d: float = 2.0
    extended_max_d: float = 4.0

    @model_validator(mode="after")
    def _validate_order(self) -> ExecutionStateConfig:
        if not self.damaged_max_d < 0.0 < self.fair_max_d < self.extended_max_d:
            msg = "execution thresholds must satisfy damaged < 0 < fair < extended"
            raise ValueError(msg)
        return self


class VcpSignalConfig(_StrictModel):
    """P5-24 VCP thresholds (roadmap §5; every value is 要検証)."""

    zigzag_atr_multiplier: float = Field(default=2.0, gt=0.0)
    first_depth_min: float = Field(default=0.08, gt=0.0, le=1.0)
    first_depth_max: float = Field(default=0.35, gt=0.0, le=1.0)
    small_cap_first_depth_max: float = Field(default=0.50, gt=0.0, le=1.0)
    contraction_ratio_max: float = Field(default=0.75, gt=0.0, le=1.0)
    min_contractions: int = Field(default=2, ge=2)
    pattern_days_min: int = Field(default=15, ge=1)
    pattern_days_max: int = Field(default=325, ge=1)
    dry_up_ideal_max: float = Field(default=0.30, gt=0.0, le=1.0)
    dry_up_weak_min: float = Field(default=0.70, gt=0.0, le=1.0)
    chase_pivot_pct: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_ranges(self) -> VcpSignalConfig:
        if (
            not self.first_depth_min
            <= self.first_depth_max
            <= self.small_cap_first_depth_max
        ):
            msg = "VCP first-depth thresholds must be ordered"
            raise ValueError(msg)
        if self.pattern_days_max < self.pattern_days_min:
            msg = "VCP pattern_days_max must be >= pattern_days_min"
            raise ValueError(msg)
        if self.dry_up_weak_min <= self.dry_up_ideal_max:
            msg = "VCP dry-up weak threshold must exceed ideal threshold"
            raise ValueError(msg)
        return self


class TechnicalSignalConfig(_StrictModel):
    """`technical_signals.*` in `settings.yaml`."""

    trend: TrendSignalConfig = TrendSignalConfig()
    pullback: PullbackSignalConfig = PullbackSignalConfig()
    volume: VolumeFilterConfig = VolumeFilterConfig()
    minervini: MinerviniSignalConfig = MinerviniSignalConfig()
    execution: ExecutionStateConfig = ExecutionStateConfig()
    vcp: VcpSignalConfig = VcpSignalConfig()


class BacktestConfig(_StrictModel):
    """`backtest.*` in `settings.yaml`."""

    initial_cash_usd: float = Field(default=100_000, gt=0.0)
    entry: str = "next_open"
    exit_atr_multiple: float = Field(default=2.5, gt=0.0)
    exit_atr_period: int = Field(default=14, ge=1)
    max_hold_days: int = Field(default=60, ge=1)
    commission_pct: float = Field(default=0.001, ge=0.0, lt=1.0)
    slippage_pct: float = Field(default=0.001, ge=0.0, lt=1.0)
    benchmark: str = "SPY"
    # Multiplier applied to slippage_pct on both entry and exit (incl. forced
    # liquidation), roadmap §5 P2-09. 1.0 == no change from the base
    # slippage_pct; --pessimistic overrides it with pessimistic_slippage_multiplier.
    slippage_multiplier: float = Field(default=1.0, gt=0.0)
    # Pessimistic-scenario preset (roadmap §5 P2-09, 要検証: median of
    # backtest-expert's cited 1.5-2.0x range).
    pessimistic_slippage_multiplier: float = Field(default=1.75, gt=1.0)
    # P2-10 sensitivity grid: best cell's expectancy_per_trade strictly above
    # this multiple of its (non-gray) neighbors' median triggers a "spike"
    # (overfitting suspicion) verdict (roadmap §5 P2-10, 要検証).
    sensitivity_spike_multiplier: float = Field(default=1.5, gt=1.0)
    # P2-10 sensitivity grid: fraction (e.g. 0.20 == 20%) around the best
    # cell's value within which every non-gray cell must fall for a
    # "plateau" (robust) verdict (roadmap §5 P2-10, 要検証; basis point is the
    # best cell's own value -- not specified in the seed, fixed here).
    sensitivity_plateau_tolerance_pct: float = Field(default=0.20, ge=0.0)
    # trade_count below this draws a "statistically insufficient" warning;
    # below preliminary_trade_count_threshold (but >= this) draws a
    # "preliminary" warning (roadmap §5 P2-07, out: backtest-expert).
    insufficient_trade_count_threshold: int = Field(default=30, ge=1)
    preliminary_trade_count_threshold: int = Field(default=100, ge=1)
    # win_rate (fraction, e.g. 0.90 == 90%) strictly above this, or
    # max_drawdown_pct strictly below lookahead_suspicion_max_drawdown, draws
    # a look-ahead-bias suspicion warning (roadmap §5 P2-07). The win_rate
    # bound is from the roadmap; the drawdown bound has no seed value and is
    # fixed here as 要検証 per Issue #16's boundary note.
    lookahead_suspicion_win_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    lookahead_suspicion_max_drawdown: float = Field(default=0.01, ge=0.0)

    @model_validator(mode="after")
    def _validate_trade_count_thresholds(self) -> BacktestConfig:
        if (
            self.preliminary_trade_count_threshold
            < self.insufficient_trade_count_threshold
        ):
            msg = (
                "preliminary_trade_count_threshold must be >= "
                "insufficient_trade_count_threshold"
            )
            raise ValueError(msg)
        return self


class AnalysisConfig(_StrictModel):
    """`analysis.*` in `settings.yaml`.

    Bounds on the untrusted text handed to the qualitative-analysis skill via
    `analysis_input.json`. These replace the old `llm.*` section: the API
    caller is gone, but the collection and export limits it defined are still
    what keeps one run's exported text bounded (roadmap §5 P6-26).
    """

    max_news_items_per_symbol: int = Field(default=20, ge=1)
    max_news_chars_per_item: int = Field(default=4000, ge=1)
    # Total characters of one filing's text exported for analysis. Formerly
    # derived as `filing_chunk_chars * max_filing_chunks` from the previous
    # chunked-analysis bounds; consolidated into a single setting since
    # nothing chunks filings for separate calls anymore (the skill reads one
    # filing's export in a single context).
    max_filing_chars: int = Field(default=120_000, ge=1)
    # Filing *collection* recency bound and per-symbol count cap, applied in
    # `text/edgar_filings.py::fetch_recent_filings_text()` -- symmetric with
    # the news-side limit above (roadmap §5 P6-26).
    filing_lookback_days: int = Field(default=90, ge=1)
    max_filings_per_symbol: int = Field(default=3, ge=1)
    # Bounds on the run-wide macro/economic-calendar events surfaced in
    # `context.calendar_events` (not per-symbol: a calendar event isn't tied
    # to any one candidate).
    max_calendar_events: int = Field(default=20, ge=1)
    max_calendar_chars_per_item: int = Field(default=2000, ge=1)


class ScheduleConfig(_StrictModel):
    """`schedule.*` in `settings.yaml` (NFR-03)."""

    timeout_minutes: int = Field(default=35, ge=1)


class NotificationConfig(_StrictModel):
    """`notification.*` in `settings.yaml`. Discord notification is opt-in."""

    enabled: bool = False


class PostmortemConfig(_StrictModel):
    """`postmortem.*` in `settings.yaml` (P2-11, roadmap §5 P2-11)."""

    horizon_5d_weight: float = Field(default=0.6, ge=0.0)
    horizon_20d_weight: float = Field(default=0.4, ge=0.0)
    neutral_threshold_pct: float = Field(
        default=0.5, ge=0.0
    )  # |return%| <= this -> NEUTRAL (要検証)
    # return% < -this -> FALSE_POSITIVE_SEVERE (要検証)
    severe_threshold_pct: float = Field(default=2.0, ge=0.0)
    preliminary_sample_threshold: int = Field(default=20, ge=1)
    lookback_window_days: int = Field(default=90, ge=1)

    @model_validator(mode="after")
    def _validate_postmortem_thresholds(self) -> PostmortemConfig:
        if not math.isclose(
            self.horizon_5d_weight + self.horizon_20d_weight,
            1.0,
            abs_tol=1e-9,
        ):
            msg = "postmortem horizon weights must sum to 1.0"
            raise ValueError(msg)
        if self.severe_threshold_pct < self.neutral_threshold_pct:
            msg = "severe_threshold_pct must be >= neutral_threshold_pct"
            raise ValueError(msg)
        return self


class RegimeConfig(_StrictModel):
    """`regime.*` thresholds (roadmap §5 P3-13; all are 要検証)."""

    ema_period: int = Field(default=50, ge=1)
    bull_vix_max: float = Field(default=20.0, ge=0.0)
    bear_spy_ema_ratio: float = Field(default=0.97, gt=0.0)
    bear_vix_min: float = Field(default=30.0, ge=0.0)
    distribution_window_days: int = Field(default=25, ge=1)
    dd_decline_pct: float = Field(default=-0.002, le=0.0)
    stall_abs_change_pct: float = Field(default=0.001, ge=0.0)
    recovery_pct: float = Field(default=0.05, ge=0.0)
    # Exposure Ceiling's REDUCE_ONLY multiplier (roadmap §5 P3-14, 要検証).
    reduce_only_risk_multiplier: float = Field(default=0.5, gt=0.0, le=1.0)
    # roadmap §5 P3-16（要検証）: display-only Follow-Through Day thresholds.
    ftd_correction_decline_pct: float = Field(default=0.03, gt=0.0)
    ftd_correction_down_days: int = Field(default=3, ge=1)
    ftd_gain_pct: float = Field(default=0.0125, gt=0.0)

    @model_validator(mode="after")
    def _validate_vix_threshold_order(self) -> RegimeConfig:
        if self.bear_vix_min < self.bull_vix_max:
            msg = "bear_vix_min must be >= bull_vix_max"
            raise ValueError(msg)
        return self


class RetroConfig(_StrictModel):
    """`retro.*` in `settings.yaml` (P8-31, roadmap §5 P8-31).

    Deliberately tiny. The retrospective's evaluation window, horizon weights,
    noise/severity boundaries, and preliminary-sample floor all come from
    `settings.postmortem` (decision D6): a verdict and a signal are measured
    through the same window so their performance stays comparable, and one
    quantity never gets two configurable names.
    """

    # How many MISS_SEVERE symbols get a full evidence dossier in
    # `retro_input.json` (要検証: set from the reading budget of one
    # retrospective pass, not from data). Overflow is truncated by
    # |forward_return| with the dropped count reported, never silently.
    max_surprises: int = Field(default=5, ge=1)
    # Reserved for a future switch to per-proposal human approval (D10,
    # design §8.2). Nothing reads it yet: the initial implementation is
    # `auto`-only, and the name exists so the eventual `manual` mode does not
    # have to rename a shipped setting.
    approval_mode: Literal["auto", "manual"] = "auto"


class Settings(_StrictModel):
    """Parsed, validated `config/settings.yaml`."""

    universe: UniverseConfig = UniverseConfig()
    risk: RiskConfig = RiskConfig()
    fundamental_filters: FundamentalFilterConfig = FundamentalFilterConfig()
    technical_signals: TechnicalSignalConfig = TechnicalSignalConfig()
    backtest: BacktestConfig = BacktestConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    notification: NotificationConfig = NotificationConfig()
    postmortem: PostmortemConfig = PostmortemConfig()
    regime: RegimeConfig = RegimeConfig()
    retro: RetroConfig = RetroConfig()


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


class MinerviniStrategyConfig(_StrictModel):
    """Per-strategy P5-21 acceptance threshold."""

    min_criteria: int = Field(default=6, ge=1, le=7)


class StrategySpec(_StrictModel):
    """Validated composition and ranking rules for one screening strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # YAML sequences deserialize as lists. Keep scalar values strict while
    # accepting that serialization boundary for this immutable tuple API.
    filters_all: tuple[str, ...] = Field(strict=False)
    signals_all: tuple[str, ...] = Field(strict=False)
    candidate_limit: int = Field(gt=0, le=10)
    ranking: RankingConfig = RankingConfig()
    minervini: MinerviniStrategyConfig | None = None


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
            `{"finnhub", "fred"}`.

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
